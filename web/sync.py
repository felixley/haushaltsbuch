"""GUI-gesteuerter FinTS-Abruf (Umsätze -> SQLite).

Ablauf pro Abruf (nichts davon wird gespeichert, außer den Umsätzen):
  1. GUI sendet Bank + PIN + Zeitraum  -> Job startet (Thread)
  2. Job loggt sich mit PIN ein; die PIN lebt nur im Speicher des Jobs
  3. Bank verlangt ggf. TAN-Medium-Auswahl  -> Job wartet auf GUI-Eingabe
  4. Bank verlangt TAN / photoTAN / App-Freigabe -> Job wartet auf GUI-Eingabe
  5. Umsätze werden wie in transaktionen_speichern.py geschrieben

Bank-Konfiguration (BLZ, Benutzerkennung, FinTS-URL, TAN-Verfahren) kommt
weiterhin aus der .env. Die PIN kommt NIE aus Datei oder DB.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fints.client import NeedTANResponse
from fints.utils import decode_phototan_image

from fints_session import create_client
from transaktionen_speichern import (
    apply_schema,
    insert_transactions,
    upsert_account,
)
from web.kategorien import ensure_default_categories, load_learned_rules, suggest_category

BANKS = {
    "comdirect": "comdirect",
    "dkb": "DKB",
}

INPUT_TIMEOUT_S = 300          # wie lange der Job auf TAN/Auswahl wartet
JOB_RETENTION_S = 900          # abgeschlossene Jobs werden nach 15 min verworfen
MAX_DAYS = 365


class SyncError(Exception):
    pass


def bank_config(key: str) -> dict:
    """Liest BLZ/Benutzer/URL/TAN-Verfahren aus der .env (ohne PIN)."""
    if key not in BANKS:
        raise SyncError(f"Unbekannte Bank: {key}")
    prefix = key.upper()
    cfg = {
        "blz": os.getenv(f"{prefix}_BLZ"),
        "username": os.getenv(f"{prefix}_USERNAME"),
        "hbci": os.getenv(f"{prefix}_HBCI"),
        "tan_mechanism": os.getenv(f"{prefix}_TAN_MECHANISM"),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise SyncError(
            f"In der .env fehlen für {key}: " + ", ".join(f"{prefix}_{m.upper()}" for m in missing)
        )
    return cfg


def banks_status() -> list[dict]:
    out = []
    for key, label in BANKS.items():
        try:
            bank_config(key)
            configured = True
        except SyncError:
            configured = False
        out.append({"key": key, "label": label, "configured": configured})
    return out


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------
@dataclass
class Job:
    id: str
    bank: str
    status: str = "running"          # running | tan_required | choice_required | done | error
    prompt: dict | None = None       # Inhalt für die GUI bei Eingabebedarf
    result: dict | None = None
    error: str | None = None
    challenge: tuple[str, bytes] | None = None   # (mime, Bildbytes) des anstehenden photoTAN
    prompt_seq: int = 0                          # fortlaufend, kommt in jeden prompt
    created: float = field(default_factory=time.time)
    finished: float | None = None
    inbox: queue.Queue = field(default_factory=queue.Queue, repr=False)
    client: object | None = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def public(self) -> dict:
        """Nur GUI-taugliche Felder (kein Client, keine Eingaben)."""
        with self.lock:
            return {
                "job_id": self.id,
                "bank": self.bank,
                "status": self.status,
                "prompt": self.prompt,
                "result": self.result,
                "error": self.error,
            }


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _prune() -> None:
    now = time.time()
    with _jobs_lock:
        for jid in [j.id for j in _jobs.values()
                    if j.finished and now - j.finished > JOB_RETENTION_S]:
            _jobs.pop(jid, None)


def active_job() -> Job | None:
    with _jobs_lock:
        for j in _jobs.values():
            if j.status not in ("done", "error"):
                return j
    return None


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def start_job(bank: str, pin: str, days: int, db_path: Path) -> Job:
    """Startet einen Abruf. Die PIN wird nur an den Thread übergeben und nicht gespeichert."""
    _prune()
    if active_job() is not None:
        raise SyncError("Es läuft bereits ein Abruf. Bitte warten, bis er abgeschlossen ist.")
    cfg = bank_config(bank)
    if not pin:
        raise SyncError("PIN fehlt")
    days = min(max(int(days), 1), MAX_DAYS)
    job = Job(id=uuid.uuid4().hex, bank=bank)
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=_run, args=(job, cfg, pin, days, db_path), daemon=True).start()
    return job


def submit_input(job: Job, value: dict) -> None:
    """GUI liefert TAN / Auswahl. Nur gültig, wenn der Job darauf wartet."""
    if job.status not in ("tan_required", "choice_required"):
        raise SyncError("Der Abruf wartet gerade auf keine Eingabe.")
    job.inbox.put(value)


def cancel_job(job: Job) -> None:
    if job.status not in ("tan_required", "choice_required"):
        raise SyncError("Abbrechen ist nur bei Eingabebedarf möglich.")
    job.inbox.put({"_cancel": True})


# ------------------------------------------------------------------
# FinTS-Ablauf
# ------------------------------------------------------------------
def challenge_image(resp) -> tuple[str, bytes] | None:
    """Extrahiert aus einer NeedTANResponse ein (mime, Bildbytes)-Tupel für photoTAN.

    Reihenfolge der Quellen:
      1. challenge_matrix (falls mime mit 'image/' beginnt)
      2. challenge_hhduc – Bytes, ASCII-String oder base64-kodierter String,
         aufgelöst über decode_phototan_image
      3. base64-PNG direkt im challenge-Text (iVBORw0KGgo...)

    Wirft nie eine Exception nach außen und loggt keine Geheimnisse.
    Ergebnis wird nur akzeptiert, wenn der MIME-Typ mit 'image/' beginnt.
    """
    try:
        # 1. Matrix-Code-Challenge
        if getattr(resp, "challenge_matrix", None):
            mime, data = resp.challenge_matrix
            if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, (bytes, bytearray)):
                return (mime, bytes(data))

        # 2. HHD_UC-Challenge
        hhduc = getattr(resp, "challenge_hhduc", None)
        if hhduc:
            payload = hhduc
            if isinstance(hhduc, str):
                try:
                    payload = base64.b64decode(hhduc.encode() + b"=" * (-len(hhduc) % 4), validate=True)
                except Exception:
                    payload = hhduc.encode("iso-8859-1")
            if isinstance(payload, (bytes, bytearray)):
                decoded = decode_phototan_image(bytes(payload))
                mime = decoded.get("mime_type", "")
                if isinstance(mime, str) and mime.startswith("image/"):
                    return (mime, decoded.get("image", b""))

        # 3. base64-PNG direkt im Challenge-Text suchen
        text = getattr(resp, "challenge", "") or ""
        if isinstance(text, str) and "iVBOR" in text:
            b64 = re.search(r"iVBOR[A-Za-z0-9+/=]+", text)
            if b64:
                raw = base64.b64decode(b64.group(0).encode() + b"=" * (-(len(b64.group(0)) % 4) % 4), validate=True)
                if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    return ("image/png", raw)
    except Exception:
        pass  # Bild nicht dekodierbar -> nur Text anzeigen
    return None


def _wait_input(job: Job, status: str, prompt: dict, resp=None) -> dict:
    with job.lock:
        job.prompt_seq += 1
        seq = job.prompt_seq
    prompt = dict(prompt)
    prompt["seq"] = seq
    image = challenge_image(resp) if resp is not None else None
    if image:
        job.challenge = image
        prompt["image_url"] = f"/api/sync/{job.id}/challenge"
        prompt["mime"] = image[0]
    job.update(status=status, prompt=prompt)
    try:
        value = job.inbox.get(timeout=INPUT_TIMEOUT_S)
    except queue.Empty:
        raise SyncError("Keine Eingabe innerhalb von 5 Minuten – Abruf abgebrochen.")
    if value.get("_cancel"):
        raise SyncError("Abruf abgebrochen.")
    job.update(status="running", prompt=None, challenge=None)
    return value


def _challenge_prompt(resp: NeedTANResponse) -> dict:
    image = None
    img = challenge_image(resp)
    if img:
        mime, data = img
        image = (mime, data)
    return {
        "type": "tan",
        "text": resp.challenge or "",
        "decoupled": bool(resp.decoupled),   # True = nur in der Banking-App bestätigen
        "image": (f"data:{image[0]};base64,{base64.b64encode(image[1]).decode()}"
                  if image else None),
    }


def _resolve(job: Job, client, resp):
    """Wiederholt Eingabe + send_tan, solange die Bank eine Freigabe verlangt."""
    while isinstance(resp, NeedTANResponse):
        value = _wait_input(job, "tan_required", _challenge_prompt(resp), resp)
        if resp.decoupled:
            tan = ""  # Bei App-Freigabe wird die TAN ignoriert (wie in der Bibliothek)
        else:
            tan = str(value.get("tan", "")).strip()
            if not tan:
                raise SyncError("Keine TAN eingegeben.")
        resp = client.send_tan(resp, tan)
    return resp


def _ensure_tan_medium(job: Job, client) -> None:
    """Ersetzt minimal_interactive_cli_bootstrap (dort wird per input() gefragt)."""
    if client.selected_tan_medium is not None or not client.is_tan_media_required():
        return
    media = client.get_tan_media()[1]
    if len(media) == 1:
        client.set_tan_medium(media[0])
    elif not media:
        client.selected_tan_medium = ""  # Workaround wie in der Bibliothek (z.B. ohne Medium)
    else:
        options = [
            {"index": i, "label": f"{m.tan_medium_name} · {m.mobile_number_masked or ''} · zuletzt {m.last_use or '–'}"}
            for i, m in enumerate(media)
        ]
        value = _wait_input(job, "choice_required",
                            {"type": "choice", "text": "Bitte TAN-Medium wählen", "options": options})
        client.set_tan_medium(media[int(value.get("index", 0))])


def _safe_message(exc: Exception, pin: str | None) -> str:
    msg = str(exc) or type(exc).__name__
    if pin:
        msg = msg.replace(pin, "***")
    return msg


def _categorize_account(conn: sqlite3.Connection, account_iban: str) -> int:
    """Kategorisiert noch nicht zugeordnete Umsätze eines Kontos automatisch.

    Es werden nur Zeilen mit ``category_id IS NULL`` angefasst, manuell gesetzte
    Kategorien bleiben also erhalten. Liefert die Anzahl der Zuordnungen.
    """
    ensure_default_categories(conn)
    ids = {row[0]: row[1] for row in conn.execute("SELECT name, id FROM categories")}
    names = set(ids)
    learned = load_learned_rules(conn)
    rows = conn.execute(
        """SELECT id, applicant_name, purpose, amount FROM transactions
           WHERE account_iban = ? AND category_id IS NULL""",
        (account_iban,),
    ).fetchall()
    changed = 0
    for tx_id, applicant, purpose, amount in rows:
        proposal = suggest_category(learned, applicant, purpose, amount, names=names)
        if proposal is None:
            continue
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (ids[proposal], tx_id))
        changed += 1
    return changed


def _run(job: Job, cfg: dict, pin: str, days: int, db_path: Path) -> None:
    client = None
    secret = pin  # nur für die Maskierung eventueller Fehlertexte
    try:
        client = create_client(cfg["blz"], cfg["username"], pin, cfg["hbci"])
        pin = None  # Referenz im Thread verwerfen; der Client hält sie für den Login
        job.client = client
        client.fetch_tan_mechanisms()
        client.set_tan_mechanism(cfg["tan_mechanism"])
        _ensure_tan_medium(job, client)

        end = dt.datetime.now()
        start = end - dt.timedelta(days=days)
        per_account: dict[str, dict] = {}

        with client:
            if isinstance(client.init_tan_response, NeedTANResponse):
                client.init_tan_response = _resolve(job, client, client.init_tan_response)

            accounts = client.get_sepa_accounts()
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                apply_schema(conn)
                for account in accounts:
                    result = client.get_transactions(account, start_date=start, end_date=end)
                    if isinstance(result, NeedTANResponse):
                        result = _resolve(job, client, result)
                    data = [t.data for t in result]
                    upsert_account(conn, account)
                    inserted, skipped = insert_transactions(conn, account.iban, data)
                    # Neue Umsätze sofort automatisch kategorisieren
                    # (vorhandene/manuelle Zuordnungen bleiben unangetastet).
                    categorized = _categorize_account(conn, account.iban)
                    conn.commit()
                    per_account[account.iban] = {"new": inserted, "skipped": skipped,
                                                 "fetched": len(data),
                                                 "categorized": categorized}
            finally:
                conn.close()

        job.update(status="done", result={"accounts": per_account}, prompt=None)
    except Exception as exc:  # noqa: BLE001 – Fehler gehen als Text an die GUI
        job.update(status="error", error=_safe_message(exc, secret), prompt=None,
                   challenge=None)
    finally:
        pin = None
        secret = None
        job.update(finished=time.time(), prompt=None, challenge=None)
        job.client = None
