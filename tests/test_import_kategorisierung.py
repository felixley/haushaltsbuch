"""Regressionstest: Der CLI-Import (transaktionen_speichern.py) kategorisiert.

Hintergrund: Die automatische Kategorisierung (web/kategorien.py) war nur an die
GUI-Pfade gehängt – an den Dashboard-Button ``POST /api/categorize`` und an den
GUI-Bankabruf ``web/sync.py``. Wer die Umsätze headless über das Skript
``transaktionen_speichern.py`` importiert (typisch in einem Container, der nur
per uvicorn läuft), hatte danach ausschließlich "Nicht zugeordnet"-Umsätze.

Dieser Test fährt den kompletten ``main()``-Ablauf mit einem Fake-FinTS-Client
(kein Bankzugang) und prüft, dass die neu importierten Umsätze am Ende eine
Kategorie haben.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from decimal import Decimal

from fints.client import NeedTANResponse

import transaktionen_speichern as speichern

PIN = "geheim4711"


class FakeTxn:
    def __init__(self, data):
        self.data = data


class FakeAccount:
    iban = "DE00000000000000000001"
    bic = "TESTDEFF"
    accountnumber = "1"
    subaccount = "Girokonto"
    blz = "20041155"


class FakeClient:
    """Bildet gerade so viel Bankverhalten nach, wie main() braucht."""

    def __init__(self, blz, user, pin, server, product_id=None, **kw):
        self.init_tan_response = None  # kein direktes TAN-Verlangen

    def fetch_tan_mechanisms(self):
        pass

    def set_tan_mechanism(self, _):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_sepa_accounts(self):
        return [FakeAccount()]

    def get_transactions(self, account, start_date=None, end_date=None, **kw):
        return [FakeTxn(d) for d in TXNS]


TXNS = [
    {"date": dt.date(2026, 10, 1), "entry_date": dt.date(2026, 10, 1),
     "amount": Decimal("-7.24"), "currency": "EUR", "status": "D",
     "applicant_name": "NETTO MARKEN-DISCOUNT",
     "purpose": "LASTSCHRIFT / BELASTUNGNETTO MARKEN-DISCOUNT, KOELN"},
    {"date": dt.date(2026, 10, 2), "entry_date": dt.date(2026, 10, 2),
     "amount": Decimal("-2.14"), "currency": "EUR", "status": "D",
     "applicant_name": "SHELL 1945", "purpose": "KARTENZAHLUNG"},
    {"date": dt.date(2026, 10, 3), "entry_date": dt.date(2026, 10, 3),
     "amount": Decimal("-3.00"), "currency": "EUR", "status": "D",
     "applicant_name": "VOELLIG UNBEKANNTER LADEN XYZ", "purpose": "LASTSCHRIFT"},
]


def _category_of(db, applicant_name: str) -> str | None:
    conn = sqlite3.connect(db)
    row = conn.execute(
        """SELECT c.name FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.applicant_name = ?""",
        (applicant_name,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_cli_import_kategorisiert_automatisch(tmp_path, monkeypatch) -> None:
    db = tmp_path / "import.db"

    # Zugangsdaten kommen aus der .env – hier per Umgebung ersetzen.
    monkeypatch.setenv("BANK", "comdirect")
    monkeypatch.setenv("COMDIRECT_BLZ", "20041155")
    monkeypatch.setenv("COMDIRECT_USERNAME", "12345678900")
    monkeypatch.setenv("COMDIRECT_HBCI", "https://example.invalid/fints")
    monkeypatch.setenv("COMDIRECT_TAN_MECHANISM", "902")

    # Kein Bankzugang, keine interaktive PIN-Eingabe.
    monkeypatch.setattr(speichern, "create_client",
                        lambda blz, user, pin, url: FakeClient(blz, user, pin, url))
    monkeypatch.setattr(speichern, "minimal_interactive_cli_bootstrap", lambda client: None)
    monkeypatch.setattr(speichern.getpass, "getpass", lambda *a, **k: PIN)
    monkeypatch.setattr(sys, "argv", ["transaktionen_speichern.py", "--db", str(db)])

    speichern.main()

    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    open_rows = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL"
    ).fetchone()[0]
    conn.close()

    assert total == 3, "alle drei Fake-Umsätze müssen importiert sein"
    # Der Kern der Regression: nach dem CLI-Import darf nicht mehr alles offen sein.
    assert open_rows < total, "der CLI-Import muss automatisch kategorisieren"
    assert _category_of(db, "NETTO MARKEN-DISCOUNT") == "Lebensmittel"
    assert _category_of(db, "SHELL 1945") == "Tanken"
    # Unbekannte Händler bleiben bewusst ohne Kategorie (keine falsche Zuordnung).
    assert _category_of(db, "VOELLIG UNBEKANNTER LADEN XYZ") is None
