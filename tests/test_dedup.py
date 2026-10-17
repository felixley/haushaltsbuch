"""Tests für die Duplikaterkennung beim Umsatzimport.

Der frühere Hash enthielt Status, Bankreferenzen und ``extra_details``. Diese
Felder können sich zwischen zwei Abrufen ändern (vorgemerkt -> gebucht, neue
Referenz), wodurch derselbe Umsatz erneut eingefügt wurde. Der Identitäts-Hash
basiert jetzt nur noch auf stabilen Feldern.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal

import transaktionen_speichern as ts

IBAN = "DE00000000000000000001"


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open(ts.SCHEMA_FILE, encoding="utf-8").read())
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Testbank')", (IBAN,))
    conn.commit()
    return conn


def _txn(**overrides):
    data = {
        "date": dt.date(2026, 10, 1),
        "amount": Decimal("-12.34"),
        "currency": "EUR",
        "applicant_name": "REWE Markt",
        "purpose": "Kartenzahlung",
        "status": "PDNG",
        "bank_reference": None,
        "customer_reference": None,
    }
    data.update(overrides)
    return data


def test_identity_hash_ignoriert_status_und_referenzen() -> None:
    a = ts._make_dedup_hash(IBAN, _txn())
    b = ts._make_dedup_hash(IBAN, _txn(
        status="C", bank_reference="REF-123", customer_reference="K-9",
        extra_details="gebucht"))
    assert a == b


def test_identity_hash_normalisiert_schreibweise_und_betrag() -> None:
    a = ts._make_dedup_hash(IBAN, _txn())
    b = ts._make_dedup_hash(" de00 0000 0000 0000 0000 01 ",
                            _txn(amount=Decimal("-12.340"), applicant_name="  REWE   Markt ",
                                 purpose="kartenzahlung"))
    assert a == b


def test_zweiter_abruf_mit_neuem_status_legt_keine_zweite_zeile_an(tmp_path) -> None:
    conn = _db(tmp_path)
    first = ts.insert_transactions(conn, IBAN, [_txn()])
    assert first == (1, 0)

    # Zweiter Abruf: Umsatz ist jetzt gebucht, Bankreferenz neu vergeben
    second = ts.insert_transactions(conn, IBAN, [
        _txn(status="C", bank_reference="REF-123", extra_details="gebucht")])
    assert second == (0, 1)

    rows = conn.execute("SELECT status, bank_reference FROM transactions").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "C"
    assert rows[0]["bank_reference"] == "REF-123"


def test_update_laesst_kategorie_unangetastet(tmp_path) -> None:
    conn = _db(tmp_path)
    ts.insert_transactions(conn, IBAN, [_txn()])
    conn.execute("INSERT INTO categories (name, type) VALUES ('Lebensmittel', 'expense')")
    cat = conn.execute("SELECT id FROM categories").fetchone()[0]
    conn.execute("UPDATE transactions SET category_id = ?", (cat,))
    conn.commit()

    ts.insert_transactions(conn, IBAN, [_txn(status="C", bank_reference="REF-1")])
    assert conn.execute(
        "SELECT category_id FROM transactions").fetchone()[0] == cat


def test_repair_fuehrt_alte_duplikate_zusammen_und_behaelt_kategorie(tmp_path) -> None:
    conn = _db(tmp_path)
    # Alte Duplikate: gleicher Umsatz, unterschiedlicher alter Hash
    conn.execute("INSERT INTO categories (name, type) VALUES ('Lebensmittel', 'expense')")
    cat = conn.execute("SELECT id FROM categories").fetchone()[0]
    payload = (
        "INSERT INTO transactions (account_iban, booking_date, amount, currency,"
        " applicant_name, purpose, status, raw, dedup_hash, category_id)"
        " VALUES (?, '2026-10-01', '-12.34', 'EUR', 'REWE Markt', 'Kartenzahlung',"
        " ?, '{}', ?, ?)"
    )
    conn.execute(payload, (IBAN, "PDNG", "alter-hash-1", None))
    conn.execute(payload, (IBAN, "C", "alter-hash-2", cat))
    conn.commit()

    report = ts.repair_duplicates(conn)
    assert report == {"rows": 2, "unique": 1, "merged": 1}
    row = conn.execute(
        "SELECT category_id, dedup_hash FROM transactions").fetchone()
    assert row["category_id"] == cat
    assert row["dedup_hash"] == ts._make_dedup_hash(IBAN, _txn())

    # erneuter Lauf ist idempotent
    assert ts.repair_duplicates(conn)["merged"] == 0


def test_apply_schema_bereinigt_bestehende_db_einmalig(tmp_path) -> None:
    db = tmp_path / "alt.db"
    schema = open(ts.SCHEMA_FILE, encoding="utf-8").read().replace(
        "dedup_hash           TEXT UNIQUE,", "dedup_hash           TEXT,")
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.execute("INSERT INTO accounts (iban) VALUES (?)", (IBAN,))
    for h in ("alt-1", "alt-2"):
        conn.execute(
            "INSERT INTO transactions (account_iban, booking_date, amount, currency,"
            " applicant_name, purpose, raw, dedup_hash)"
            " VALUES (?, '2026-10-01', '-12.34', 'EUR', 'REWE Markt',"
            " 'Kartenzahlung', '{}', ?)", (IBAN, h))
    conn.commit()

    ts.apply_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_transactions_dedup_hash'").fetchone()
    assert idx is not None

    # Nach dem Umbau greift ON CONFLICT wieder: kein Fehler, keine neue Zeile
    assert ts.insert_transactions(conn, IBAN, [_txn(status="C")]) == (0, 1)


def test_unterschiedliche_umsaetze_bleiben_erhalten(tmp_path) -> None:
    conn = _db(tmp_path)
    ts.insert_transactions(conn, IBAN, [
        _txn(),
        _txn(amount=Decimal("-12.35")),
        _txn(purpose="Anderer Zweck"),
        _txn(date=dt.date(2026, 10, 2)),
    ])
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
