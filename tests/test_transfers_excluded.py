"""Tests: Interne Transfers/Umbuchungen zählen nicht in Kennzahlen und Grafiken.

Umsätze der Kategorie "Transfers (intern)" (bzw. "Transferintern"/"Umbuchung")
bewegen nur Geld zwischen eigenen Konten. Sie bleiben in der Umsatztabelle
sichtbar, dürfen aber Einnahmen, Ausgaben, Timeline und Kategorien-Donut nicht
verfälschen.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web.kategorien import internal_transfer_category_ids

SCHEMA = webapp.SCHEMA_FILE.read_text(encoding="utf-8")
A = "DE00000000000000000001"
B = "DE00000000000000000002"
MONAT = dt.date.today().replace(day=1).isoformat()
VORMONAT = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()


def _seed(db) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank A')", (A,))
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank B')", (B,))
    for name, typ in (("Gehalt", "income"), ("Lebensmittel", "expense"),
                      ("Transfers (intern)", "income")):
        conn.execute("INSERT INTO categories (name, type) VALUES (?, ?)", (name, typ))
    ids = dict(conn.execute("SELECT name, id FROM categories").fetchall())
    rows = [
        (A, MONAT, "2000.00", "Arbeitgeber", "GEHALT", ids["Gehalt"]),
        (A, MONAT, "-250.00", "REWE", "EINKAUF", ids["Lebensmittel"]),
        # Umbuchung aufs eigene Kreditkartenkonto: keine echte Ausgabe
        (A, MONAT, "-500.00", "Eigenes Konto", "UEBERTRAG", ids["Transfers (intern)"]),
        (B, MONAT, "500.00", "Eigenes Konto", "UEBERTRAG", ids["Transfers (intern)"]),
        (B, MONAT, "-80.00", "ALDI", "EINKAUF", ids["Lebensmittel"]),
        # alter Transfer im Vormonat darf die Gesamtsumme nicht verfälschen
        (A, VORMONAT, "-1000.00", "Eigenes Konto", "UEBERTRAG", ids["Transfers (intern)"]),
    ]
    for iban, datum, betrag, name, zweck, cat in rows:
        conn.execute(
            """INSERT INTO transactions
                   (account_iban, booking_date, amount, currency, applicant_name,
                    purpose, raw, dedup_hash, category_id)
               VALUES (?, ?, ?, 'EUR', ?, ?, '{}', ?, ?)""",
            (iban, datum, betrag, name, zweck, f"{iban}-{datum}-{betrag}", cat),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db)
    with TestClient(webapp.create_app()) as c:
        yield c


def test_kennzahlen_ignorieren_interne_transfers(client) -> None:
    k = client.get("/api/kpis").json()
    assert k["total_balance"] == 1670.0        # 2000 - 250 - 80
    assert k["income_month"] == 2000.0
    assert k["expenses_month"] == -330.0
    assert k["net_cashflow"] == 1670.0


def test_timeline_und_breakdown_ignorieren_interne_transfers(client) -> None:
    timeline = client.get("/api/timeline").json()
    assert timeline == [{"period": MONAT[:7], "income": 2000.0, "expenses": -330.0}]

    breakdown = client.get("/api/categories/breakdown").json()
    assert breakdown == [{"category": "Lebensmittel", "total": 330.0}]


def test_tabelle_zeigt_interne_transfers_weiterhin(client) -> None:
    tabelle = client.get("/api/transactions", params={"per_page": 100}).json()
    assert tabelle["total"] == 6
    assert any(t["betrag"] == -500.0 for t in tabelle["items"])


def test_konto_filter_bleibt_korrekt(client) -> None:
    a = client.get("/api/kpis", params={"account_id": A}).json()
    assert a["total_balance"] == 1750.0        # 2000 - 250, ohne -500-Transfer
    assert a["income_month"] == 2000.0
    assert a["expenses_month"] == -250.0


def test_suche_nach_transfer_liefert_leere_auswertung(client) -> None:
    k = client.get("/api/kpis", params={"search": "UEBERTRAG"}).json()
    assert k["total_balance"] == 0.0
    assert k["income_month"] == 0.0
    assert k["expenses_month"] == 0.0
    assert client.get("/api/timeline", params={"search": "UEBERTRAG"}).json() == []
    assert client.get("/api/categories/breakdown", params={"search": "UEBERTRAG"}).json() == []
    # die Tabelle findet die Umbuchungen aber weiterhin
    assert client.get("/api/transactions", params={"search": "UEBERTRAG"}).json()["total"] == 3


def test_kategorie_varianten_werden_erkannt(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.executescript(SCHEMA)
    for name in ("Transfers (intern)", "Transferintern", "Interner Transfer",
                 "Umbuchung", "Interne Umbuchung", "Lebensmittel"):
        conn.execute("INSERT INTO categories (name, type) VALUES (?, 'expense')", (name,))
    ids = internal_transfer_category_ids(conn)
    found = {row[0] for row in conn.execute(
        f"SELECT name FROM categories WHERE id IN ({','.join('?' * len(ids))})", ids)}
    assert found == {"Transfers (intern)", "Transferintern", "Interner Transfer",
                     "Umbuchung", "Interne Umbuchung"}
    conn.close()
