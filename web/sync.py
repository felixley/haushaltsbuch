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
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.utils import decode_phototan_image

from transaktionen_speichern import (
    PRODUCT_ID,
    apply_schema,
    insert_transactions,
    upsert_account,
)

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
def _wait_input(job: Job, status: str, prompt: dict) -> dict:
    job.update(status=status, prompt=prompt)
    try:
        value = job.inbox.get(timeout=INPUT_TIMEOUT_S)
    except queue.Empty:
        raise SyncError("Keine Eingabe innerhalb von 5 Minuten – Abruf abgebrochen.")
    if value.get("_cancel"):
        raise SyncError("Abruf abgebrochen.")
    job.update(status="running", prompt=None)
    return value


def _challenge_prompt(resp: NeedTANResponse) -> dict:
    image = None
    try:
        if resp.challenge_matrix:
            mime, data = resp.challenge_matrix[0], resp.challenge_matrix[1]
            image = (mime, data)
        elif resp.challenge_hhduc:
            decoded = decode_phototan_image(resp.challenge_hhduc)
            image = (decoded["mime_type"], decoded["image"])
    except Exception:
        image = None  # Bild nicht dekodierbar -> nur Text anzeigen
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
        value = _wait_input(job, "tan_required", _challenge_prompt(resp))
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


def _run(job: Job, cfg: dict, pin: str, days: int, db_path: Path) -> None:
    client = None
    secret = pin  # nur für die Maskierung eventueller Fehlertexte
    try:
        client = FinTS3PinTanClient(cfg["blz"], cfg["username"], pin, cfg["hbci"],
                                    product_id=PRODUCT_ID)
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
                    conn.commit()
                    per_account[account.iban] = {"new": inserted, "skipped": skipped,
                                                 "fetched": len(data)}
            finally:
                conn.close()

        job.update(status="done", result={"accounts": per_account}, prompt=None)
    except Exception as exc:  # noqa: BLE001 – Fehler gehen als Text an die GUI
        job.update(status="error", error=_safe_message(exc, secret), prompt=None)
    finally:
        pin = None
        secret = None
        job.update(finished=time.time())
        job.client = None
