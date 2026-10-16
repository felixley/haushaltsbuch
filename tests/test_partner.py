"""Tests für die IBAN-Bereinigung des Partner-Namens (web/partner.py)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web.partner import clean_partner_name, extract_ibans

# IBAN-artige Ketten nach der neuen Regel: 2 Buchstaben + mind. 8 Ziffern.
DE_IBAN = "DE89370400440532013000"  # kompakt
DE_IBAN_GROUPED = "DE89 3704 0044 0532 0130 00"  # gedruckt (Vierergruppen)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # unverändert, wenn keine IBAN-artige Kette enthalten ist
        ("REWE SAG Köln 112", "REWE SAG Köln 112"),
        ("ALDI Sued Köln", "ALDI Sued Köln"),
        ("Netflix Entertainment", "Netflix Entertainment"),
        ("Konto 1234567890", "Konto 1234567890"),  # reine Ziffern
        ("", ""),
        # kompakt/verklebt: IBAN direkt vor dem Namen (typisch für Bank-Exporte)
        ("DE03760300800870717444FELIX LEY", "FELIX LEY"),
        ("DE69120300001035734530FELIX DKB", "FELIX DKB"),
        ("DE65200411550859475600Felix Ley", "Felix Ley"),
        ("DE96120300009005290904REWE.Koeln.Lindent/Koeln", "REWE.Koeln.Lindent/Koeln"),
        # kompakt, mit Leerzeichen getrennt
        (f"Muster GmbH {DE_IBAN}", "Muster GmbH"),
        (f"Muster GmbH, {DE_IBAN}", "Muster GmbH"),
        (f"{DE_IBAN} Max Mustermann", "Max Mustermann"),
        (f"Firma {DE_IBAN} Zweigstelle", "Firma Zweigstelle"),
        # gedruckt/gruppiert (mit Leerzeichen)
        (f"Max Mustermann {DE_IBAN_GROUPED}", "Max Mustermann"),
        (f"Firma {DE_IBAN_GROUPED} Zweigstelle", "Firma Zweigstelle"),
        # nur eine IBAN -> bleibt leer
        (DE_IBAN, ""),
        (DE_IBAN_GROUPED, ""),
    ],
)
def test_clean_partner_name(raw: str, expected: str) -> None:
    assert clean_partner_name(raw) == expected


def test_clean_partner_name_none_returns_empty() -> None:
    assert clean_partner_name(None) == ""


def test_no_match_leaves_value_untouched() -> None:
    """Ohne IBAN-Treffer bleibt der Wert exakt erhalten (keine Whitespace-Normalisierung)."""
    raw = "SUMUP  *DB IMBISS&CATERIN"  # doppeltes Leerzeichen, kein IBAN-Treffer
    assert clean_partner_name(raw) == raw


def test_extract_ibans_compact_and_grouped() -> None:
    assert extract_ibans(f"Zahlung an {DE_IBAN}") == [DE_IBAN]
    assert extract_ibans(f"Zahlung an {DE_IBAN_GROUPED}") == [DE_IBAN]


def test_extract_ibans_multiple() -> None:
    text = f"Von DE03760300800870717444 nach DE69120300001035734530"
    assert extract_ibans(text) == ["DE03760300800870717444", "DE69120300001035734530"]
    assert clean_partner_name(text) == "Von nach"


def test_plain_number_not_treated_as_iban() -> None:
    # reine Ziffern (ohne führende zwei Buchstaben) dürfen nicht verändert werden
    assert clean_partner_name("Konto 1234567890") == "Konto 1234567890"
    assert extract_ibans("1234567890") == []


def test_short_digit_run_not_treated_as_iban() -> None:
    """Weniger als acht Ziffern nach den zwei Buchstaben -> kein Treffer."""
    assert clean_partner_name("AB1234567") == "AB1234567"  # nur 7 Ziffern
    assert extract_ibans("AB1234567") == []
    # genau acht Ziffern -> Treffer (Untergrenze)
    assert clean_partner_name("AB12345678") == ""
    assert extract_ibans("AB12345678") == ["AB12345678"]


def test_clean_is_idempotent() -> None:
    once = clean_partner_name("DE03760300800870717444FELIX LEY")
    assert clean_partner_name(once) == once == "FELIX LEY"


# ---------------------------------------------------------------------------
# Integration: /api/transactions liefert den bereinigten Partner-Namen
# ---------------------------------------------------------------------------
def _seed_tx(db, applicant_name: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO accounts (iban, bank_name) VALUES (?, ?)",
        ("DE00000000000000000001", "Testbank"),
    )
    conn.execute(
        """INSERT INTO transactions
               (account_iban, booking_date, amount, currency, applicant_name, purpose, raw, dedup_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "DE00000000000000000001",
            "2026-10-01",
            "-12.34",
            "EUR",
            applicant_name,
            "Testumsatz",
            "{}",
            "hash-" + applicant_name,
        ),
    )
    conn.commit()
    conn.close()


def test_api_transactions_strips_iban_from_partner(tmp_path, monkeypatch) -> None:
    """Der GUI-Endpunkt darf im Partner-Feld keine IBAN mehr ausgeben."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)

    # Erst der Lifespan (legt das Schema an), dann die Daten, dann der Abruf.
    with TestClient(webapp.create_app()) as client:
        # kompakt/verklebt (typischer Bank-Export)
        _seed_tx(db, "DE03760300800870717444FELIX LEY")
        data = client.get("/api/transactions").json()

    assert data["total"] == 1
    item = data["items"][0]
    assert item["partner_name"] == "FELIX LEY"
    assert "DE03760300800870717444" not in str(data)


# ---------------------------------------------------------------------------
# Migration: bestehende DB-Einträge bereinigen (migration/clean_partner_names.py)
# ---------------------------------------------------------------------------
def test_clean_database_updates_existing_rows(tmp_path, monkeypatch) -> None:
    """Die IBAN wird auch aus bereits gespeicherten Einträgen entfernt."""
    from migration.clean_partner_names import clean_database

    db = tmp_path / "test.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    with TestClient(webapp.create_app()) as client:
        _seed_tx(db, "DE03760300800870717444FELIX LEY")
        _seed_tx(db, "REWE SAG Köln 112")  # unverändert erwartet
        client.get("/api/transactions")  # Schema anlegen

    scanned, changed = clean_database(db)
    assert scanned == 2
    assert changed == 1  # nur die IBAN-Zeile

    conn = sqlite3.connect(db)
    values = {r[0] for r in conn.execute("SELECT applicant_name FROM transactions")}
    conn.close()
    assert "FELIX LEY" in values
    assert "REWE SAG Köln 112" in values

    # Idempotent: ein zweiter Lauf ändert nichts mehr.
    scanned2, changed2 = clean_database(db)
    assert scanned2 == 2
    assert changed2 == 0


