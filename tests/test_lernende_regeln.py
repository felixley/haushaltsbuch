"""Tests für die lernenden Regeln (manuelle Zuordnung -> automatisch übernehmen).

Anforderung: Wird eine Kombination aus Partner (``applicant_name``) und
Verwendungszweck (``purpose``) manuell einer Kategorie zugeordnet, so sollen
alle *gleichen* Umsätze – bereits vorhandene (sofern noch ohne Kategorie) und
künftige – automatisch dieselbe Kategorie erhalten.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import web.app as webapp
from web.kategorien import (
    apply_categorization_to_db,
    ensure_default_categories,
    ensure_learned_rules_table,
    learn_from_manual_assignment,
    load_learned_rules,
    match_key,
)

SCHEMA = webapp.SCHEMA_FILE.read_text(encoding="utf-8")
IBAN = "DE00000000000000000001"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def _seed(db, rows) -> None:
    """Legt Schema, Kategorien, ein Konto und die übergebenen Umsätze an."""
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    ensure_default_categories(conn)
    conn.execute(
        "INSERT OR IGNORE INTO accounts (iban, bank_name) VALUES (?, ?)", (IBAN, "Testbank")
    )
    for name, zweck, betrag, h in rows:
        _insert(conn, name, zweck, betrag, h)
    conn.commit()
    conn.close()


def _insert(conn, name, zweck, betrag, h) -> None:
    conn.execute(
        """INSERT INTO transactions
               (account_iban, booking_date, amount, currency, applicant_name, purpose, raw, dedup_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (IBAN, "2026-10-01", betrag, "EUR", name, zweck, "{}", h),
    )


def _cat_id(db, name) -> int:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row[0]


def _categories_by_id(db) -> dict[int, str | None]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        """SELECT t.id, c.name
             FROM transactions t
             LEFT JOIN categories c ON c.id = t.category_id
            ORDER BY t.id"""
    ).fetchall()
    conn.close()
    return {tx_id: name for tx_id, name in rows}


def _learned_count(db) -> int:
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT count(*) FROM learned_rules").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Schlüsselbildung
# ---------------------------------------------------------------------------
def test_match_key_ignoriert_iban_im_partner() -> None:
    """Gleicher Partner trotz vorangestellter IBAN -> gleicher Schlüssel."""
    a = match_key("DE03760300800870717444FELIX LEY", "UEBERTRAG / UEBERWEISUNG")
    b = match_key("Felix Ley", "UEBERTRAG / UEBERWEISUNG")
    assert a == b

    c = match_key("REWE", "KARTENZAHLUNG REWE SAG")
    d = match_key("  rewe  ", "  kartenzahlung   rewe sag ")
    assert c == d  # Groß-/Kleinschreibung + Mehrfach-Leerzeichen egal


def test_match_key_trennt_unterschiedlichen_zweck() -> None:
    assert match_key("REWE", "EINKAUF A") != match_key("REWE", "EINKAUF B")


# ---------------------------------------------------------------------------
# Lernen + Anwenden
# ---------------------------------------------------------------------------
def test_manuelle_zuordnung_wird_auf_gleiche_umsetze_angewandt(tmp_path) -> None:
    """Ein manuell zugeordneter Umsatz zieht identische, offene Umsätze mit."""
    db = tmp_path / "t.db"
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-5.00", "h3"),
    ])
    cat = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    updated = learn_from_manual_assignment(conn, 1, cat)
    conn.commit()
    conn.close()

    assert updated == 3
    assert set(_categories_by_id(db).values()) == {"Haushalt & Drogerie"}
    assert _learned_count(db) == 1


def test_gelernte_regel_greift_bei_kuenftigem_import(tmp_path) -> None:
    """Ein später importierter, gleicher Umsatz wird automatisch zugeordnet."""
    db = tmp_path / "t.db"
    _seed(db, [("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1")])
    cat = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    learn_from_manual_assignment(conn, 1, cat)
    _insert(conn, "REWE", "KARTENZAHLUNG REWE SAG", "-9.99", "h2")  # neuer, offener Umsatz
    conn.commit()
    conn.close()

    scanned, changed = apply_categorization_to_db(db)
    assert scanned == 1 and changed == 1
    assert _categories_by_id(db)[2] == "Haushalt & Drogerie"


def test_gelernte_regel_hat_vorrang_vor_eingebauter_regel(tmp_path) -> None:
    """Manuell gelernt schlägt die eingebaute Regel (SHELL wäre sonst Tanken)."""
    db = tmp_path / "t.db"
    _seed(db, [("SHELL 1945", "LASTSCHRIFT / BELASTUNG", "-40.00", "h1")])
    hobby = _cat_id(db, "Freizeit & Hobby")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    learn_from_manual_assignment(conn, 1, hobby)
    conn.commit()
    conn.close()

    assert _categories_by_id(db)[1] == "Freizeit & Hobby"
    assert load_learned_rules(sqlite3.connect(db))


def test_aufheben_entfernt_gelernte_regel(tmp_path) -> None:
    """Zuordnung auf 'Nicht zugeordnet' hebt die gelernte Regel wieder auf."""
    db = tmp_path / "t.db"
    _seed(db, [("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1")])
    cat = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    learn_from_manual_assignment(conn, 1, cat)
    conn.commit()

    updated = learn_from_manual_assignment(conn, 1, None)  # Zuordnung aufheben
    conn.commit()
    conn.close()

    assert updated == 0
    assert _learned_count(db) == 0
    assert _categories_by_id(db)[1] is None


def test_andere_manuelle_zuordnung_bleibt_erhalten(tmp_path) -> None:
    """Ein gleichartiger Umsatz mit eigener Kategorie wird nicht überschrieben."""
    db = tmp_path / "t.db"
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
    ])
    barb = _cat_id(db, "Barbeträge")
    drogerie = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    conn.execute("UPDATE transactions SET category_id = ? WHERE id = 2", (barb,))
    updated = learn_from_manual_assignment(conn, 1, drogerie)
    conn.commit()
    conn.close()

    assert updated == 1  # nur Umsatz 1, Umsatz 2 bleibt Barbeträge
    cats = _categories_by_id(db)
    assert cats[1] == "Haushalt & Drogerie"
    assert cats[2] == "Barbeträge"


def test_iban_variante_wird_mitgezogen(tmp_path) -> None:
    """Umsätze mit/ohne IBAN im Partnerfeld gelten als dieselbe Kombination."""
    db = tmp_path / "t.db"
    _seed(db, [
        ("DE03760300800870717444FELIX LEY", "UEBERTRAG / UEBERWEISUNG", "-750.00", "h1"),
        ("Felix Ley", "UEBERTRAG / UEBERWEISUNG", "-750.00", "h2"),
    ])
    cat = _cat_id(db, "Transfers (intern)")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    updated = learn_from_manual_assignment(conn, 1, cat)
    conn.commit()
    conn.close()

    assert updated == 2
    assert set(_categories_by_id(db).values()) == {"Transfers (intern)"}


# ---------------------------------------------------------------------------
# Integration: API-Endpunkt + Sync-Pfad
# ---------------------------------------------------------------------------
def test_api_manual_assignment_backfill(tmp_path, monkeypatch) -> None:
    """Der PATCH-Endpunkt zieht gleiche Umsätze mit und meldet die Anzahl."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
    ])
    cat = _cat_id(db, "Haushalt & Drogerie")

    with TestClient(webapp.create_app()) as client:
        r = client.patch("/api/transactions/1/category", json={"category_id": cat})
        assert r.status_code == 200
        assert r.json()["updated"] == 2
        assert r.json()["category"] == "Haushalt & Drogerie"

        data = client.get("/api/transactions").json()
        names = {i["id"]: i["category"] for i in data["items"]}
        assert names[1] == "Haushalt & Drogerie"
        assert names[2] == "Haushalt & Drogerie"


def test_sync_pfad_nutzt_gelernte_regeln(tmp_path) -> None:
    """Auch der Bankabruf (web/sync.py) übernimmt gelernte Regeln."""
    from web import sync

    db = tmp_path / "t.db"
    _seed(db, [("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1")])
    cat = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    ensure_learned_rules_table(conn)
    learn_from_manual_assignment(conn, 1, cat)
    _insert(conn, "REWE", "KARTENZAHLUNG REWE SAG", "-9.99", "h2")
    _insert(conn, "REWE", "KARTENZAHLUNG REWE SAG", "-3.33", "h3")
    conn.commit()

    changed = sync._categorize_account(conn, IBAN)
    conn.commit()
    conn.close()

    assert changed == 2
    assert set(_categories_by_id(db).values()) == {"Haushalt & Drogerie"}
