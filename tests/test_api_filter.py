"""Tests: Alle Filter der Leiste wirken auf Tabelle UND Auswertungen.

Die Filterleiste unten (Suche, Kategorie, Konto, Zeitraum) muss nicht nur
``/api/transactions`` einschränken, sondern auch ``/api/kpis``,
``/api/timeline`` und ``/api/categories/breakdown`` – sonst zeigen die
Kacheln oben andere Zahlen als die Tabelle darunter.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.app as webapp

SCHEMA = webapp.SCHEMA_FILE.read_text(encoding="utf-8")
KONTO_A = "DE00000000000000000001"
KONTO_B = "DE00000000000000000002"


def _monat(offset: int = 0) -> dt.date:
    heute = dt.date.today().replace(day=1)
    jahr, monat = heute.year, heute.month + offset
    while monat < 1:
        jahr, monat = jahr - 1, monat + 12
    while monat > 12:
        jahr, monat = jahr + 1, monat - 12
    return dt.date(jahr, monat, 1)


AKTUELL = _monat(0).isoformat()
VORMONAT = _monat(-1).isoformat()


def _seed(db) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank A')", (KONTO_A,))
    conn.execute("INSERT INTO accounts (iban, bank_name) VALUES (?, 'Bank B')", (KONTO_B,))
    for name, typ in (("Gehalt & Lohn", "income"), ("Lebensmittel", "expense"), ("Miete", "expense")):
        conn.execute("INSERT INTO categories (name, type) VALUES (?, ?)", (name, typ))
    ids = {
        r[0]: r[1]
        for r in conn.execute("SELECT name, id FROM categories").fetchall()
    }
    rows = [
        # aktueller Monat
        (KONTO_A, AKTUELL, "1000.00", "Arbeitgeber A", "GEHALT", ids["Gehalt & Lohn"], "a1"),
        (KONTO_A, AKTUELL, "-100.00", "REWE", "EINKAUF", ids["Lebensmittel"], "a2"),
        (KONTO_B, AKTUELL, "500.00", "Kunde B", "RECHNUNG", ids["Gehalt & Lohn"], "b1"),
        (KONTO_B, AKTUELL, "-50.00", "ALDI", "EINKAUF", ids["Lebensmittel"], "b2"),
        # Vormonat
        (KONTO_A, VORMONAT, "-700.00", "Vermieter", "MIETE", ids["Miete"], "a3"),
        (KONTO_B, VORMONAT, "-30.00", "REWE", "EINKAUF", ids["Lebensmittel"], "b3"),
    ]
    for iban, datum, betrag, name, zweck, cat, h in rows:
        conn.execute(
            """INSERT INTO transactions
                   (account_iban, booking_date, amount, currency, applicant_name,
                    purpose, raw, dedup_hash, category_id)
               VALUES (?, ?, ?, 'EUR', ?, ?, '{}', ?, ?)""",
            (iban, datum, betrag, name, zweck, h, cat),
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


def _cat(client, name: str) -> int:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def test_ohne_filter_bleiben_einnahmen_und_ausgaben_im_aktuellen_monat(client) -> None:
    k = client.get("/api/kpis").json()
    assert k["total_balance"] == 620.0          # alle Zeiträume
    assert k["income_month"] == 1500.0
    assert k["expenses_month"] == -150.0       # Vormonat zählt nicht
    assert k["period_mode"] == "month"


def test_kategorie_filter_wirkt_auf_kennzahlen(client) -> None:
    k = client.get("/api/kpis", params={"category_id": _cat(client, "Lebensmittel")}).json()
    assert k["total_balance"] == -180.0        # inkl. Vormonat
    assert k["income_month"] == 0.0
    assert k["expenses_month"] == -150.0
    t = client.get("/api/transactions", params={"category_id": _cat(client, "Lebensmittel")}).json()
    assert t["total"] == 3


def test_datumsfilter_ersetzt_den_monatsbezug(client) -> None:
    k = client.get(
        "/api/kpis",
        params={"date_from": VORMONAT, "date_to": VORMONAT},
    ).json()
    assert k["period_mode"] == "range"
    assert k["total_balance"] == -730.0
    assert k["income_month"] == 0.0
    assert k["expenses_month"] == -730.0
    assert k["net_cashflow"] == -730.0


def test_suche_filter_wirkt_auf_kennzahlen(client) -> None:
    k = client.get("/api/kpis", params={"search": "REWE"}).json()
    assert k["total_balance"] == -130.0        # -100 jetzt + -30 Vormonat
    assert k["expenses_month"] == -100.0


def test_konto_und_kategorie_kombiniert(client) -> None:
    k = client.get(
        "/api/kpis",
        params={"account_id": KONTO_B, "category_id": _cat(client, "Lebensmittel")},
    ).json()
    assert k["total_balance"] == -80.0         # -50 + -30
    assert k["expenses_month"] == -50.0


def test_kennzahlen_stimmen_mit_gefilterter_tabelle_ueberein(client) -> None:
    params = {"account_id": KONTO_A, "search": "EINKAUF"}
    k = client.get("/api/kpis", params=params).json()
    rows = client.get("/api/transactions", params={**params, "per_page": 100}).json()
    assert k["total_balance"] == round(sum(r["betrag"] for r in rows["items"]), 2)


def test_timeline_und_breakdown_nutzen_dieselben_filter(client) -> None:
    lebens = _cat(client, "Lebensmittel")
    timeline = client.get("/api/timeline", params={"category_id": lebens}).json()
    assert [p["period"] for p in timeline] == sorted([AKTUELL[:7], VORMONAT[:7]])
    assert sum(p["expenses"] for p in timeline) == -180.0

    breakdown = client.get(
        "/api/categories/breakdown",
        params={"date_from": VORMONAT, "date_to": VORMONAT},
    ).json()
    assert breakdown == [
        {"category": "Miete", "total": 700.0},
        {"category": "Lebensmittel", "total": 30.0},
    ]
