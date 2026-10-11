"""Gemeinsamer FinTS-Verbindungsaufbau für CLI (transaktionen_speichern.py) und GUI (web/sync.py).

Standard = exakt der Aufbau, mit dem die CLI bisher funktioniert hat.

Optionale comdirect-Korrektur aus dem finance-tracker (app/banks/fints_base.py),
aktiv nur mit FINTS_CUSTOMER_ID_WORKAROUND=1 (noch nicht gegen die echte Bank bestätigt):
- customer_id wird nach dem Aufbau auf "" gesetzt.
- System-ID-Sync (HKSYN -> HISYN4) probeweise mit "" und danach mit user_id.

Diese Datei darf nicht von transaktionen_speichern.py oder web/* importiert werden,
sonst entsteht ein Import-Zyklus. Sie importiert selbst nur fints.
"""

from __future__ import annotations

import os

from fints.client import FinTS3PinTanClient, SYSTEM_ID_UNASSIGNED

# FinTS-Produkt-ID (gemeinsam für CLI und GUI)
PRODUCT_ID = "6151256F3D4F9975B877BD4A2"

_ANONYMOUS_CUSTOMER_ID = "9999999999"


class FinTSSetupError(Exception):
    """Verbindungsaufbau fehlgeschlagen (Netz, Bank, System-ID)."""


def _mask(msg: str, *secrets) -> str:
    """Ersetzt Geheimnisse (PIN/Benutzerkennung) in einer Meldung durch '***'."""
    if not msg:
        return msg
    out = str(msg)
    for secret in secrets:
        if not secret:
            continue
        s = str(secret)
        if s and s != "***":
            out = out.replace(s, "***")
    return out


def sync_system_id(client) -> None:
    """Holt die System-ID, zuerst mit leerer, dann mit echter customer_id.

    Jeder Kandidat wird einzeln versucht. JEDE Exception eines Kandidaten
    (fints.exceptions.FinTSError wie ``FinTSClientPINError`` und generell
    Exception) wird abgefangen; der Fehlertext jedes Versuchs wird gemerkt.
    Erst wenn alle Kandidaten gescheitert sind, wird ``FinTSSetupError`` mit
    den gesammelten Fehlern geworfen. PIN/Benutzerkennung werden in den
    Fehlertexten durch '***' maskiert.
    """
    errors: list[str] = []
    pin_secret = getattr(getattr(client, "pin", None), "value", None)
    user_secret = getattr(client, "user_id", None)
    for cid in ("", client.user_id):
        client.customer_id = cid
        try:
            client._ensure_system_id()
            return
        except Exception as e:  # noqa: BLE001 – FinTSError und jede andere Exception
            errors.append(_mask(str(e), pin_secret, user_secret))
    detail = " | ".join(e for e in errors if e) or "(keine Fehlerdetails)"
    raise FinTSSetupError(
        "System-ID-Sync fehlgeschlagen (weder mit leerer noch mit echter "
        f"customer_id). Fehler: {detail}"
    )


def create_client(blz: str, username: str, pin: str, url: str) -> FinTS3PinTanClient:
    """Baut den FinTS-Client. Standard: wie bisher in der CLI (funktionierender Stand)."""
    client = FinTS3PinTanClient(blz, username, pin, url, product_id=PRODUCT_ID)
    if os.getenv("FINTS_CUSTOMER_ID_WORKAROUND") == "1":
        sync_system_id(client)
        client.customer_id = ""  # comdirect-Korrektur aus finance-tracker (opt-in)
    return client
