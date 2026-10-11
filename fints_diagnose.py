#!/usr/bin/env python3
"""FinTS-Diagnose für Login-Fehler (z. B. comdirect ``FinTSClientPINError``).

Führt mehrere Verbindungsvarianten nacheinander aus und zeigt die von der Bank
zurückgemeldeten FinTS-Codes (9931, 9942, 9340, 9100, 9010, …) mit deutscher
Bedeutung, damit die Ursache (Kunden-ID-Handling, Benutzerkennung, PIN, URL)
eingegrenzt werden kann.

Varianten (``--variante a|b|c|alle``, Default ``a``):
    A) Default  – customer_id = Zugangsnummer (exakt der Pfad von
                  transaktionen_speichern.py).
    B) comdirect-konform – Kunden-ID wird vor dem ersten Dialog auf "" gesetzt
                  (client.customer_id = ""), wie es das comdirect-FAQ
                  (https://www.comdirect.de/cms/sicherheit-software-banking-fints.html)
                  empfiehlt ("Das Feld Kunden-ID bitte freilassen"). Genau EIN
                  Login-Versuch, kein vorheriger System-ID-Sync.
    C) finance-tracker-Workaround – der alte Pfad (create_client mit
                  FINTS_CUSTOMER_ID_WORKAROUND=1). Nur auf ausdrückliche
                  Auswahl; wird nie automatisch gestartet.

Aufruf:
    python fints_diagnose.py                 # comdirect (aus BANK/.env)
    python fints_diagnose.py --bank dkb
    python fints_diagnose.py --yes           # keine Rückfragen (Kontosperre!)
    python fints_diagnose.py --variante alle # alle drei Varianten testen
    python fints_diagnose.py --log           # gefilterte Roh-Response (DEBUG)
    python fints_diagnose.py --log-voll      # ungefilterte Roh-Response
    python fints_diagnose.py --log-datei log.txt   # Roh-Log in Datei schreiben

Datenschutz:
    - Die PIN wird per getpass (ohne Echo) abgefragt, nie gespeichert/loggt.
    - Ein Logging-Filter und eine abschließende Prüfung maskieren die PIN
      in allen Ausgaben durch '***'; die Benutzerkennung wird nur mit
      maskierter Mitte angezeigt (z. B. 200***26).
"""

from __future__ import annotations

import argparse
import getpass
import io
import logging
import os
import re
import sys

# .env laden (Werte aus .env.example-Vorlage eintragen); die PIN kommt NIE
# aus .env, sondern immer per getpass.
from dotenv import load_dotenv

import fints_session
from fints_session import create_client

load_dotenv()

# FinTS-Rückmeldungscodes (deutsch) – Quelle: finance-tracker/app/fints_debug.py
# 9942/9340/9100/9931 sind authentische Authentication-Responses der Bank.
FINTS_CODE_DE: dict[str, str] = {
    "0030": "Neue TAN / Dialog-TAN erwünscht (normal beim Login)",
    "3910": "Synchronisierung erforderlich (HISYN)",
    "3920": "TAN-Verfahren wurden geliefert (anstatt PIN-Fehler) – eher unkritisch",
    "9000": "Allgemeiner Fehler des Teilnehmer-/Zentralrechners",
    "9010": "Unberechtigter Nutzer – Zugang zu diesem Zeitpunkt nicht erlaubt",
    "9050": "Aktion derzeit nicht möglich (vorübergehende Sperre oder Zentralrechner-Fehler)",
    "9060": "Berechtigung für die Anforderung verweigert",
    "9079": "Zugang wegen zu vieler Fehlversuche gesperrt (Kontosperre!)",
    "9100": "PIN fehlerhaft – Zugang möglicherweise gesperrt",
    "9340": "Ungültige Signatur (folgt meist auf eine falsche PIN)",
    "9800": "Allgemeine Zurückweisung – Nachricht vom Teilnehmer-/Zentralrechner abgelehnt",
    "9910": "Dialogbeginn unzulässig (Benutzerkennung/Kunden-ID?)",
    "9930": "Dialogaufbau abgelehnt – Zugangsdaten unzulässig",
    "9931": "Kunden-ID bzw. Kundenkennung unbekannt oder fehlerhaft",
    "9942": "PIN ungültig – die hinterlegte PIN ist falsch oder abgelaufen",
}

# Codes, die auf Zugangsdaten-/PIN-Probleme hindeuten -> danach NICHT ohne
# Rückfrage weitermachen (Kontosperre-Gefahr).
LOCK_CODES = ("9931", "9942", "9340", "9100")

# Zeilen des DEBUG-Logs, die für die Diagnose relevant sind (wird mit --log
# herausgefiltert): Segment-/Response-Texte, HISYN/HKSYN, Kunden-ID, Codes.
LOG_HINT_PARTS = (
    "Dialog response",
    "Response",
    "HISYN",
    "HKSYN",
    "Kunden",
    "Sending SYNC",
    "Initialisierung",
)


def mask_user(user: str | None) -> str:
    """Benutzerkennung nur mit maskierter Mitte zeigen (z. B. 200***26)."""
    u = str(user or "")
    if len(u) <= 4:
        return "***"
    if len(u) < 8:
        return u[:2] + "***" + u[-1]
    return u[:3] + "***" + u[-2:]


class _MaskFilter(logging.Filter):
    """Ersetzt PIN und Benutzerkennung in jedem Log-Record durch '***'."""

    def __init__(self, pin: str, username: str) -> None:
        super().__init__()
        self._secrets = [s for s in (pin, username) if s]

    def filter(self, record) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret and secret != "***":
                msg = msg.replace(secret, "***")
        record.msg = msg
        record.args = ()
        return True


def scrub(text: str, pin: str, username: str) -> str:
    """Finale Sicherheitsprüfung: PIN/Benutzerkennung in ``text`` durch '***' ersetzen."""
    for secret in (pin, username):
        if secret and secret != "***":
            text = text.replace(secret, "***")
    return text


def _fints_log_handler(pin: str, username: str):
    """Aktiviert DEBUG-Logging der fints-Logger in einen StringIO-Puffer (PIN-maskiert)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_MaskFilter(pin, username))
    logger = logging.getLogger("fints")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger, handler, stream


def extract_codes(text: str) -> list[str]:
    """Extrahiert bekannte 4-stellige FinTS-Codes in Erscheinungsreihenfolge."""
    if not text:
        return []
    seen: list[str] = []
    for m in re.findall(r"\b(\d{4})\b", text):
        if m in FINTS_CODE_DE and m not in seen:
            seen.append(m)
    return seen


def filter_log_lines(log_text: str) -> list[str]:
    """Filtern des DEBUG-Logs auf diagnostisch relevante Zeilen (--log)."""
    out: list[str] = []
    for line in log_text.splitlines():
        if any(p in line for p in LOG_HINT_PARTS) or re.search(r"\b\d{4}\b", line):
            out.append(line)
    return out


def _run_variant(name: str, build_client, blz, username, pin, url) -> dict:
    """Führt EINE Variante mit frischem Client aus und liefert das Ergebnis.

    Fängt Fehler aus ``build_client`` UND aus ``fetch_tan_mechanisms`` ab,
    sodass die Diagnose nie mit einem Traceback abbricht. ``stage`` verrät,
    ob der Verbindungsaufbau (Client-Bau) oder erst der Dialog scheiterte.
    Kein echter Netzwerkzugriff, wenn ``fints_session.FinTS3PinTanClient``
    für Tests durch einen Fake ersetzt wurde.
    """
    logger, handler, stream = _fints_log_handler(pin, username)
    error = None
    exc_type = None
    stage = None
    try:
        try:
            client = build_client(blz, username, pin, url)
            stage = "verbindung"  # Client-Bau ok (Verbindungsaufbau gelungen)
        except Exception as e:  # noqa: BLE001 – FinTSError und alle anderen
            exc_type = type(e).__name__
            error = scrub(str(e), pin, username)
            stage = "verbindung"
            client = None
        if client is not None:
            try:
                client.fetch_tan_mechanisms()  # Dialog-Aufbau / System-ID-Sync
                stage = None  # Verbindung + Dialog ok
            except Exception as e:  # noqa: BLE001 – FinTSError und alle anderen
                exc_type = type(e).__name__
                error = scrub(str(e), pin, username)
                stage = "dialog"
    finally:
        log_text = scrub(stream.getvalue(), pin, username)
        logger.removeHandler(handler)
        handler.close()

    codes = extract_codes(log_text)
    return {
        "variante": name,
        "exception_type": exc_type,
        "error": error,
        "codes": codes,
        "stage": stage,
        "log": log_text,
    }


def _result_codes(result: dict) -> list[str]:
    """Codes aus Log UND Fehlertext einer Variante (vereinigt, in Reihenfolge)."""
    seen: list[str] = []
    for text in (result.get("codes") or [], result.get("error") or ""):
        for c in extract_codes(text) if isinstance(text, str) else text:
            if c not in seen:
                seen.append(c)
    return seen


def _build_default(blz, username, pin, url):
    os.environ.pop("FINTS_CUSTOMER_ID_WORKAROUND", None)
    return create_client(blz, username, pin, url)


def _build_comdirect_conform(blz, username, pin, url):
    """comdirect-konform: Kunden-ID vor dem ersten Dialog leer setzen.

    Direkt über fints_session.FinTS3PinTanClient (Modulreferenz, damit Tests
    den Constructor patchen können), KEIN vorheriger sync_system_id-Aufruf und
    danach genau EIN Login-Versuch in fetch_tan_mechanisms(). Python-fints
    setzt intern ``customer_id = customer_id or user_id`` (leerer String ist
    falsy), daher muss die leere Kunden-ID NACH dem Konstruktor per Zuweisung
    gesetzt werden.
    """
    os.environ.pop("FINTS_CUSTOMER_ID_WORKAROUND", None)
    client = fints_session.FinTS3PinTanClient(
        blz, username, pin, url, product_id=fints_session.PRODUCT_ID
    )
    client.customer_id = ""  # comdirect-FAQ: "Das Feld Kunden-ID bitte freilassen"
    return client


def _build_workaround(blz, username, pin, url):
    os.environ["FINTS_CUSTOMER_ID_WORKAROUND"] = "1"
    return create_client(blz, username, pin, url)


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "j"
    except (EOFError, KeyboardInterrupt):
        return False


def recommend(results: dict) -> str:
    """Empfehlung aus den Varianten-Ergebnissen (deutsch)."""
    a = results.get("a")
    b = results.get("b")
    c = results.get("c")

    if a is not None and a["error"] is None:
        return "Standard-Pfad (Variante A) funktioniert – kein Workaround nötig."

    if b is not None and b["error"] is None:
        if a is not None and any(cd in ("9931", "9942") for cd in _result_codes(a)):
            return (
                "comdirect-konform: Kunden-ID leer setzen "
                '(client.customer_id = "" vor dem ersten Dialog), d.h. '
                "fints_session.create_client muss das für comdirect tun."
            )
        return (
            'comdirect-konform (Variante B) funktioniert -> client.customer_id = "" '
            "setzen (Kunden-ID leer lassen), wie im comdirect-FAQ beschrieben."
        )

    if c is not None and c["error"] is None:
        return (
            "Variante C funktioniert -> FINTS_CUSTOMER_ID_WORKAROUND=1 setzen "
            "(comdirect lehnt die anonymous Kunden-ID im Dialog-Init ab)."
        )

    # Keine Variante erfolgreich: fehlendes HISYN4 ist der entscheidende Befund.
    for r in (a, b, c):
        if r is not None and r["error"] and "Could not find system_id" in r["error"]:
            return (
                "Dialog mit leerer Kunden-ID akzeptiert, aber HKSYN liefert kein HISYN4 "
                "-> Rohlog mit --log schicken."
            )

    codes: list[str] = []
    for key in ("a", "b", "c"):
        if results.get(key):
            codes += _result_codes(results[key])
    if any(cd in ("9931", "9910", "9930") for cd in codes):
        return (
            "Bank meldet unbekannte/fehlerhafte Benutzerkennung oder Kunden-ID "
            "(9931/9910/9930): Benutzerkennung prüfen (Kontonummer + Unterkonto, nur "
            "Zahlen) und FinTS-URL ohne '/' am Ende prüfen. Achtung: weitere Fehlversuche "
            "können das Konto sperren."
        )
    if any(cd in ("9942", "9340", "9100") for cd in codes):
        return (
            "Bank meldet PIN-Fehler (9942/9340/9100): PIN prüfen. "
            "Achtung: weitere Fehlversuche können das Konto sperren."
        )
    return "Keine eindeutige Diagnose möglich – Codes siehe oben."


def _stage_desc(result: dict) -> str:
    if result["stage"] == "verbindung":
        return "Verbindungsaufbau fehlgeschlagen"
    if result["stage"] == "dialog":
        return "Verbindung OK, Dialog fehlgeschlagen"
    return ""


def diagnose(bank, blz, username, pin, url, yes: bool = False, variant: str = "a",
             log: bool = False, log_voll: bool = False, log_datei: str | None = None) -> str:
    """Führt die gewählten Varianten aus und liefert die deutsche Zusammenfassung.

    ``yes=True`` überspringt alle Rückfragen. ``variant`` wählt die
    Start-Variante/Reihenfolge ('a', 'b', 'c' oder 'alle'); bei 'a' wird nach
    einem Fehlschlag wie gehabt nach Variante B gefragt. Für die Tests wird
    ``fints_session.FinTS3PinTanClient`` durch einen Fake ersetzt, sodass kein
    echtes Netzwerk anläuft.
    """
    variants = {
        "a": ("A) Default (customer_id = Zugangsnummer)", _build_default),
        "b": ("B) comdirect-konform (Kunden-ID leer)", _build_comdirect_conform),
        "c": ("C) finance-tracker-Workaround", _build_workaround),
    }
    if variant == "alle":
        head, chain = "a", ["b", "c"]
    elif variant == "a":
        head, chain = "a", ["b"]
    else:
        head, chain = variant, []

    lines: list[str] = []
    add = lines.append
    add(f"Bank: {bank}, Benutzerkennung: {mask_user(username)}")
    add("Achtung: Fehlversuche beim Login können das Konto vorübergehend SPERREN!")
    add("Ohne Not nicht mehrfach auf 'Wiederholen' klicken.\n")

    results: dict = {}
    order: list[str] = []

    name, builder = variants[head]
    results[head] = _run_variant(name, builder, blz, username, pin, url)
    order.append(head)

    if results[head]["error"] is None:
        # Variante hat funktioniert -> KEINEN weiteren Login-Versuch riskieren.
        add("Weitere Varianten übersprungen (diese Variante war erfolgreich – kein "
            "weiterer Login-Versuch nötig).\n")
    else:
        for key in chain:
            prompt = (
                "\nTrotzdem Variante B versuchen? j/n: " if key == "b"
                else "\nTrotzdem Variante C (finance-tracker-Workaround) versuchen? j/n: "
            )
            if not (yes or _ask(prompt)):
                add(f"Abgebrochen – Variante {key.upper()} wird nicht ausgeführt "
                    "(Kontosperre-Risiko).\n")
                break
            name, builder = variants[key]
            results[key] = _run_variant(name, builder, blz, username, pin, url)
            order.append(key)
            if results[key]["error"] is None:
                add(f"Variante {key.upper()} war erfolgreich – keine weiteren "
                    "Login-Versuche.\n")
                break

    add("=== Zusammenfassung ===")
    for key in order:
        r = results[key]
        if r["error"] is None:
            status = "OK"
        else:
            status = f"FEHLER ({r['exception_type']}) – {_stage_desc(r)}"
        codes = ", ".join(r["codes"]) or "–"
        add(f"{r['variante']}: {status} | Codes: {codes}")
        if r["error"]:
            add(f"    Meldung: {r['error']}")

    add("\n=== Code-Bedeutungen (Fundstellen) ===")
    seen_codes: list[str] = []
    for key in order:
        for c in results[key]["codes"]:
            if c not in seen_codes:
                seen_codes.append(c)
    if seen_codes:
        for code in seen_codes:
            if code in FINTS_CODE_DE:
                add(f"  {code}: {FINTS_CODE_DE[code]}")
    else:
        add("  (keine bekannten FinTS-Codes im Log gefunden)")

    # Roh-Response der Bank (maskierter DEBUG-Log)
    if log:
        add("\n=== Roh-Response der Bank (DEBUG-Log, maskiert) ===")
        for key in order:
            r = results[key]
            add(f"--- {r['variante']} ---")
            logtext = r["log"]
            the_lines = (logtext or "").splitlines() if log_voll else filter_log_lines(logtext or "")
            add("\n".join(the_lines) if the_lines else "(keine relevanten Zeilen)")
        add("")

    if log_datei:
        try:
            with open(log_datei, "w", encoding="utf-8") as fh:
                for key in order:
                    r = results[key]
                    fh.write(f"=== {r['variante']} ===\n")
                    fh.write(r["log"] or "")
                    fh.write("\n\n")
            add(f"(Roh-Log geschrieben nach {log_datei})")
        except OSError as e:
            add(f"(Fehler beim Schreiben des Logs: {e})")

    add("\n=== Empfehlung ===")
    add("  " + recommend(results))
    return scrub("\n".join(lines), pin, username)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FinTS-Login diagnostizieren (comdirect/DKB).")
    parser.add_argument(
        "--bank", choices=["comdirect", "dkb"], default=None,
        help="Bank (überschreibt BANK aus .env; Default: 'comdirect').",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Keine Rückfragen (trotz Kontosperre-Risiko weitermachen).",
    )
    parser.add_argument(
        "--variante", choices=["a", "b", "c", "alle"], default="a",
        help="Zu testende Variante(n): a) Default, b) comdirect-konform (Kunden-ID "
             "leer), c) finance-tracker-Workaround, alle. Default: a.",
    )
    parser.add_argument(
        "--log", action="store_true",
        help="Maskierten DEBUG-Log (Roh-Response der Bank) pro Variante zeigen, "
             "gefiltert auf relevante Zeilen.",
    )
    parser.add_argument(
        "--log-voll", action="store_true",
        help="Mit --log: ungefilterten DEBUG-Log zeigen.",
    )
    parser.add_argument(
        "--log-datei", metavar="PFAD", default=None,
        help="Vollständigen maskierten DEBUG-Log in diese Datei schreiben.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    bank = (args.bank or os.getenv("BANK", "comdirect")).lower()
    prefix = bank.upper()
    credentials = {
        "blz": os.getenv(f"{prefix}_BLZ"),
        "username": os.getenv(f"{prefix}_USERNAME"),
        "hbci": os.getenv(f"{prefix}_HBCI"),
    }
    missing = [k for k, v in credentials.items() if not v]
    if missing:
        raise SystemExit(
            f"Fehlende Zugangsdaten für '{bank}' in .env: "
            f"{', '.join(prefix + '_' + m.upper() for m in missing)}. Siehe .env.example"
        )
    blz = credentials["blz"]
    username = credentials["username"]
    url = credentials["hbci"]
    pin = getpass.getpass(f"PIN für {bank} ({mask_user(username)}): ")
    print(diagnose(bank, blz, username, pin, url, yes=args.yes, variant=args.variante,
                   log=args.log, log_voll=args.log_voll, log_datei=args.log_datei))


if __name__ == "__main__":
    main()