#!/usr/bin/env python3
"""FinTS-Diagnose für Login-Fehler (z. B. comdirect ``FinTSClientPINError``).

Führt zwei Verbindungsvarianten nacheinander aus und zeigt die von der Bank
zurückgemeldeten FinTS-Codes (9931, 9942, 9340, 9100, 9010, …) mit deutscher
Bedeutung, damit die Ursache (Kunden-ID-Handling, Benutzerkennung, PIN, URL)
eingegrenzt werden kann.

Aufruf:
    python fints_diagnose.py                 # comdirect (aus BANK/.env)
    python fints_diagnose.py --bank dkb
    python fints_diagnose.py --yes           # keine Rückfrage vor Variante B

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


def _run_variant(name: str, build_client, blz, username, pin, url) -> dict:
    """Führt EINE Variante mit frischem Client aus und liefert das Ergebnis.

    Kein echter Netzwerkzugriff, wenn ``fints_session.FinTS3PinTanClient``
    für Tests durch einen Fake ersetzt wurde.
    """
    logger, handler, stream = _fints_log_handler(pin, username)
    error = None
    exc_type = None
    try:
        client = build_client(blz, username, pin, url)
        try:
            client.fetch_tan_mechanisms()  # löst den Dialog-Aufbau / System-ID-Sync aus
        except Exception as e:  # noqa: BLE001 – FinTSError und alle anderen
            exc_type = type(e).__name__
            error = scrub(str(e), pin, username)
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
    }


def _build_default(blz, username, pin, url):
    os.environ.pop("FINTS_CUSTOMER_ID_WORKAROUND", None)
    return create_client(blz, username, pin, url)


def _build_workaround(blz, username, pin, url):
    os.environ["FINTS_CUSTOMER_ID_WORKAROUND"] = "1"
    return create_client(blz, username, pin, url)


def recommend(a: dict, b: dict) -> str:
    """Empfehlung aus den beiden Varianten-Ergebnissen (deutsch)."""
    if a["error"] is None:
        return "Standard-Pfad (Variante A) funktioniert – kein Workaround nötig."
    if b["error"] is None:
        return (
            "Variante B funktioniert -> FINTS_CUSTOMER_ID_WORKAROUND=1 setzen "
            "(comdirect lehnt die anonymous Kunden-ID im Dialog-Init ab)."
        )
    codes = a["codes"] + b["codes"]
    if any(c in ("9931", "9910", "9930") for c in codes):
        return (
            "Bank meldet unbekannte/fehlerhafte Benutzerkennung oder Kunden-ID "
            "(9931/9910/9930): Benutzerkennung prüfen (Kontonummer + Unterkonto, nur "
            "Zahlen) und FinTS-URL ohne '/' am Ende prüfen. Achtung: weitere Fehlversuche "
            "können das Konto sperren."
        )
    if any(c in ("9942", "9340", "9100") for c in codes):
        return (
            "Bank meldet PIN-Fehler (9942/9340/9100): PIN prüfen. "
            "Achtung: weitere Fehlversuche können das Konto sperren."
        )
    return "Keine eindeutige Diagnose möglich – Codes siehe oben."


def diagnose(bank, blz, username, pin, url, yes: bool = False) -> str:
    """Führt beide Varianten aus und liefert die deutsche Zusammenfassung.

    ``yes=True`` überspringt die interaktive Rückfrage vor Variante B.
    Für die Tests wird ``fints_session.FinTS3PinTanClient`` durch einen Fake
    ersetzt, sodass kein echtes Netzwerk anläuft.
    """
    lines: list[str] = []
    add = lines.append
    add(f"Bank: {bank}, Benutzerkennung: {mask_user(username)}")
    add("Achtung: Fehlversuche beim Login können das Konto vorübergehend SPERREN!")
    add("Ohne Not nicht mehrfach auf 'Wiederholen' klicken.\n")

    a = _run_variant("A) Default (customer_id = user_id)", _build_default,
                     blz, username, pin, url)

    proceed = True
    if a["error"] is None:
        # Variante A hat funktioniert -> KEINEN weiteren Login-Versuch riskieren
        # (comdirect sperrt nach mehreren Fehlversuchen).
        proceed = False
        add("Variante B übersprungen (Variante A war erfolgreich – kein weiterer "
            "Login-Versuch nötig).\n")
    elif any(c in LOCK_CODES for c in a["codes"]) and not yes:
        try:
            answer = input("\nTrotzdem Variante B versuchen? j/n: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        proceed = answer == "j"
        if not proceed:
            add("Abgebrochen – Variante B wird nicht ausgeführt (Kontosperre-Risiko).\n")

    b = None
    if proceed:
        b = _run_variant("B) Workaround (customer_id leer)", _build_workaround,
                         blz, username, pin, url)

    add("=== Zusammenfassung ===")
    for result in (a, b):
        if result is None:
            continue
        status = "OK" if result["error"] is None else f"FEHLER ({result['exception_type']})"
        codes = ", ".join(result["codes"]) or "–"
        add(f"{result['variante']}: {status} | Codes: {codes}")
        if result["error"]:
            add(f"    Meldung: {result['error']}")

    add("\n=== Code-Bedeutungen (Fundstellen) ===")
    seen_codes = (a["codes"] + (b["codes"] if b else []))
    if seen_codes:
        for code in seen_codes:
            if code in FINTS_CODE_DE:
                add(f"  {code}: {FINTS_CODE_DE[code]}")
    else:
        add("  (keine bekannten FinTS-Codes im Log gefunden)")

    if b is None:
        b = {"variante": "B) Workaround (customer_id leer)", "exception_type": None,
             "error": "nicht ausgeführt (Kontosperre-Risiko)", "codes": []}

    add("\n=== Empfehlung ===")
    add("  " + recommend(a, b))
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FinTS-Login diagnostizieren (comdirect/DKB).")
    parser.add_argument(
        "--bank", choices=["comdirect", "dkb"], default=None,
        help="Bank (überschreibt BANK aus .env; Default: 'comdirect').",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Keine Rückfrage vor Variante B (trotz Kontosperre-Risiko weitermachen).",
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
    print(diagnose(bank, blz, username, pin, url, yes=args.yes))


if __name__ == "__main__":
    main()