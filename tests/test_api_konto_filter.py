"""Tests für den Konto-Filter der Dashboards.

Der Konto-Filter (``?account_id=<iban>``) muss nicht nur die Umsatztabelle,
sondern auch die Auswertungen oben auf der Seite einschränken:
``/api/kpis``, ``/api/timeline`` und ``/api/categories/breakdown``.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from fastapi.testclient import TestClient

import web.app as webapp

SCHEMA = webapp.SCHEMA_FILE.read_text(encoding="utf-8")
KONTO_A = "DE00000000000000000001"
KONTO_B = "DE00000000000000000002"


def _seed(db) -> None:
    """Zwei Konten mit je einer Einnahme und einer Ausgabe im aktuellen Monat."""
    monat = dt.date.today().replace(day=1).isoformat()
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank A')", (KONTO_A,))
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank B')", (KONTO_B,))
    conn.execute("INSERT INTO categories (name, type) VALUES ('Gehalt & Lohn', 'income')")
    conn.execute("INSERT INTO categories (name, type) VALUES ('Lebensmittel', 'expense')")
    gehalt = conn.execute("SELECT id FROM categories WHERE name='Gehalt & Lohn'").fetchone()[0]
    lebens = conn.execute("SELECT id FROM categories WHERE name='Lebensmittel'").fetchone()[0]
    rows = [
        (KONTO_A, "1000.00", "Arbeitgeber A", "GEHALT", gehalt, "a1"),
        (KONTO_A, "-100.00", "REWE", "EINKAUF", lebens, "a2"),
        (KONTO_B, "500.00", "Kunde B", "RECHNUNG", gehalt, "b1"),
        (KONTO_B, "-50.00", "ALDI", "EINKAUF", lebens, "b2"),
    ]
    for iban, betrag, name, zweck, cat, h in rows:
        conn.execute(
            """INSERT INTO transactions
                   (account_iban, booking_date, amount, currency, applicant_name,
                    purpose, raw, dedup_hash, category_id)
               VALUES (?, ?, ?, 'EUR', ?, ?, '{}', ?, ?)""",
            (iban, monat, betrag, name, zweck, h, cat),
        )
    conn.commit()
    conn.close()


def _client(tmp_path, monkeypatch) -> TestClient:
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db)
    return TestClient(webapp.create_app())


def test_kpis_ohne_filter_summiert_alle_konten(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        k = client.get("/api/kpis").json()
    assert k["total_balance"] == 1350.0
    assert k["income_month"] == 1500.0
    assert k["expenses_month"] == -150.0
    assert k["net_cashflow"] == 1350.0


def test_kpis_mit_konto_filter(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        a = client.get("/api/kpis", params={"account_id": KONTO_A}).json()
        b = client.get("/api/kpis", params={"account_id": KONTO_B}).json()
    assert a["total_balance"] == 900.0
    assert a["income_month"] == 1000.0
    assert a["expenses_month"] == -100.0
    assert b["total_balance"] == 450.0
    assert b["income_month"] == 500.0
    assert b["expenses_month"] == -50.0


def test_timeline_mit_konto_filter(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        alle = client.get("/api/timeline").json()
        a = client.get("/api/timeline", params={"account_id": KONTO_A}).json()
    assert sum(p["income"] for p in alle) == 1500.0
    assert len(a) == 1
    assert a[0]["income"] == 1000.0
    assert a[0]["expenses"] == -100.0


def test_breakdown_mit_konto_filter(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        alle = client.get("/api/categories/breakdown").json()
        a = client.get("/api/categories/breakdown", params={"account_id": KONTO_A}).json()
    gesamt = {d["category"]: d["total"] for d in alle}
    gefiltert = {d["category"]: d["total"] for d in a}
    assert gesamt == {"Lebensmittel": 150.0}
    assert gefiltert == {"Lebensmittel": 100.0}


def test_unbekanntes_konto_liefert_leere_auswertung(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        k = client.get("/api/kpis", params={"account_id": "DE99999999999999999999"}).json()
        t = client.get("/api/timeline", params={"account_id": "DE99999999999999999999"}).json()
        b = client.get("/api/categories/breakdown", params={"account_id": "DE99999999999999999999"}).json()
    assert k == {"total_balance": 0.0, "income_month": 0.0,
                 "expenses_month": 0.0, "net_cashflow": 0.0}
    assert t == []
    assert b == []
