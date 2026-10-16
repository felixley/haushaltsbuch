"""Tests für die IBAN-Bereinigung des Partner-Namens (web/partner.py)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web.partner import clean_partner_name, extract_ibans

# Gültige IBANs (ISO-13616-Prüfsumme == 1)
DE_IBAN = "DE89370400440532013000"
GB_IBAN = "GB82WEST12345698765432"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # unverändert, wenn keine IBAN enthalten ist
        ("REWE SAG Köln 112", "REWE SAG Köln 112"),
        ("ALDI Sued Köln", "ALDI Sued Köln"),
        ("Netflix Entertainment", "Netflix Entertainment"),
        ("", ""),
        # IBAN am Ende, mit Leerzeichen gruppiert
        (f"Max Mustermann {DE_IBAN[:4]} {DE_IBAN[4:8]} {DE_IBAN[8:12]} {DE_IBAN[12:16]} {DE_IBAN[16:20]} {DE_IBAN[20:]}", "Max Mustermann"),
        # IBAN am Ende, kompakt
        (f"Muster GmbH {DE_IBAN}", "Muster GmbH"),
        # IBAN nach Komma
        (f"Muster GmbH, {DE_IBAN}", "Muster GmbH"),
        # IBAN am Anfang
        (f"{DE_IBAN} Max Mustermann", "Max Mustermann"),
        (f"{GB_IBAN[:4]} {GB_IBAN[4:8]} {GB_IBAN[8:12]} {GB_IBAN[12:16]} {GB_IBAN[16:20]} {GB_IBAN[20:]} ACME Ltd", "ACME Ltd"),
        # IBAN in der Mitte
        (f"Firma {DE_IBAN} Zweigstelle", "Firma Zweigstelle"),
        # nur eine IBAN -> bleibt leer
        (DE_IBAN, ""),
    ],
)
def test_clean_partner_name(raw: str, expected: str) -> None:
    assert clean_partner_name(raw) == expected


def test_clean_partner_name_none_returns_empty() -> None:
    assert clean_partner_name(None) == ""


def test_invalid_checksum_is_kept() -> None:
    """Eine IBAN-artige Zeichenkette mit falscher Prüfsumme darf nicht entfernt werden."""
    wrong = "DE89370400440532013001"  # letzte Ziffer verfälscht
    assert clean_partner_name(wrong) == wrong
    assert extract_ibans(wrong) == []


def test_extract_ibans_compact_and_grouped() -> None:
    text = f"Zahlung an {DE_IBAN[:4]} {DE_IBAN[4:8]} {DE_IBAN[8:12]} {DE_IBAN[12:16]} {DE_IBAN[16:20]} {DE_IBAN[20:]}"
    assert extract_ibans(text) == [DE_IBAN]


def test_extract_ibans_multiple() -> None:
    text = f"Von {DE_IBAN} nach {GB_IBAN}"
    assert set(extract_ibans(text)) == {DE_IBAN, GB_IBAN}
    assert clean_partner_name(text) == "Von nach"


def test_plain_number_not_treated_as_iban() -> None:
    # "Köln 112" oder reine Ziffern dürfen nicht verändert werden
    assert clean_partner_name("Konto 1234567890") == "Konto 1234567890"
    assert extract_ibans("1234567890") == []


def test_clean_is_idempotent() -> None:
    once = clean_partner_name(f"Max Mustermann {DE_IBAN}")
    assert clean_partner_name(once) == once == "Max Mustermann"


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
    grouped = f"{DE_IBAN[:4]} {DE_IBAN[4:8]} {DE_IBAN[8:12]} {DE_IBAN[12:16]} {DE_IBAN[16:20]} {DE_IBAN[20:]}"

    # Erst der Lifespan (legt das Schema an), dann die Daten, dann der Abruf.
    with TestClient(webapp.create_app()) as client:
        _seed_tx(db, f"Max Mustermann {grouped}")
        data = client.get("/api/transactions").json()

    assert data["total"] == 1
    item = data["items"][0]
    assert item["partner_name"] == "Max Mustermann"
    assert DE_IBAN not in str(data)


