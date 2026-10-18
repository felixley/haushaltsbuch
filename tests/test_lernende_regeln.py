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
import web.kategorien as kat
from web.kategorien import (
    apply_categorization_to_db,
    apply_rule_to_all_matches,
    count_other_matches,
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


def test_count_other_matches_meldet_abweichungen(tmp_path) -> None:
    """Vor dem Überschreiben wird gemeldet, wie viele gleichartige Umsätze abweichen."""
    db = tmp_path / "t.db"
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-5.00", "h3"),
    ])
    drogerie = _cat_id(db, "Haushalt & Drogerie")
    barb = _cat_id(db, "Barbeträge")

    conn = sqlite3.connect(db)
    conn.execute("UPDATE transactions SET category_id = ? WHERE id IN (2, 3)", (barb,))
    conn.commit()

    assert count_other_matches(conn, 1, drogerie) == 2
    # schon identisch zugeordnet -> nichts zu tun
    conn.execute("UPDATE transactions SET category_id = ? WHERE id = 2", (drogerie,))
    assert count_other_matches(conn, 1, drogerie) == 1
    conn.close()


def test_apply_to_all_ueberschreibt_auch_zugeordnete(tmp_path) -> None:
    """Bestätigtes Überschreiben setzt ALLE Treffer, auch bereits kategorisierte."""
    db = tmp_path / "t.db"
    _seed(db, [
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-34.80", "h1"),
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-21.00", "h2"),
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-12.50", "h3"),
    ])
    ziel = _cat_id(db, "Restaurants & Cafés")
    alt = _cat_id(db, "Steuererstattung")

    conn = sqlite3.connect(db)
    # Alle drei sind bereits (falsch) vorkategorisiert – wie nach "Automatisch kategorisieren".
    conn.execute("UPDATE transactions SET category_id = ?", (alt,))
    conn.commit()

    updated = apply_rule_to_all_matches(conn, 1, ziel)
    conn.commit()
    conn.close()

    assert updated == 3
    assert set(_categories_by_id(db).values()) == {"Restaurants & Cafés"}


def test_apply_to_all_ist_idempotent(tmp_path) -> None:
    """Ein zweiter Aufruf mit gleicher Kategorie ändert nichts mehr."""
    db = tmp_path / "t.db"
    _seed(db, [("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1")])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    assert apply_rule_to_all_matches(conn, 1, ziel) == 1
    # Rückgabe zählt Treffer, nicht Änderungen -> bleibt 1, Daten unverändert.
    assert apply_rule_to_all_matches(conn, 1, ziel) == 1
    conn.commit()
    conn.close()

    assert set(_categories_by_id(db).values()) == {"Haushalt & Drogerie"}


def test_api_apply_to_all(tmp_path, monkeypatch) -> None:
    """PATCH überträgt die Kategorie auf alle gleichartigen Umsätze (Standard)."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-34.80", "h1"),
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-21.00", "h2"),
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-12.50", "h3"),
    ])
    ziel = _cat_id(db, "Restaurants & Cafés")
    alt = _cat_id(db, "Steuererstattung")

    conn = sqlite3.connect(db)
    # Alle drei sind bereits (falsch) vorkategorisiert – wie nach "Automatisch kategorisieren".
    conn.execute("UPDATE transactions SET category_id = ?", (alt,))
    conn.commit()
    conn.close()

    with TestClient(webapp.create_app()) as client:
        r = client.patch("/api/transactions/1/category", json={"category_id": ziel})
        assert r.status_code == 200
        body = r.json()
        assert body["updated"] == 3
        assert body["overwritten"] == 2      # die beiden anderen Vorkategorisierungen
        assert body["undo_token"]

        cats = {i["id"]: i["category"] for i in client.get("/api/transactions").json()["items"]}
        assert set(cats.values()) == {"Restaurants & Cafés"}


def test_api_undo_macht_sammelzuordnung_rueckgaengig(tmp_path, monkeypatch) -> None:
    """``undo_token`` stellt Kategorien und gelernte Regel wieder her."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-34.80", "h1"),
        ("Restaurant Vapiano", "Restaurant Vapiano Zahlung", "-21.00", "h2"),
    ])
    ziel = _cat_id(db, "Restaurants & Cafés")
    alt = _cat_id(db, "Steuererstattung")

    conn = sqlite3.connect(db)
    conn.execute("UPDATE transactions SET category_id = ? WHERE id = 2", (alt,))
    conn.commit()
    conn.close()

    with TestClient(webapp.create_app()) as client:
        token = client.patch(
            "/api/transactions/1/category", json={"category_id": ziel}
        ).json()["undo_token"]
        assert {i["category"] for i in client.get("/api/transactions").json()["items"]} == {"Restaurants & Cafés"}

        r = client.post("/api/transactions/category/undo", json={"token": token})
        assert r.status_code == 200 and r.json()["restored"] == 2

        cats = {i["id"]: i["category"] for i in client.get("/api/transactions").json()["items"]}
        assert cats[1] is None                # war vorher leer
        assert cats[2] == "Steuererstattung"  # vorherige Zuordnung wieder da
        assert _learned_count(db) == 0        # Regel mit entfernt

        # zweiter Aufruf: nichts mehr rückgängig zu machen
        assert client.post("/api/transactions/category/undo", json={"token": token}).status_code == 404


def test_gleicher_partner_und_zweck_mit_anderem_betrag(tmp_path, monkeypatch) -> None:
    """Nur der Betrag unterscheidet sich -> Umsatz wird trotzdem mitgezogen."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
    ])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    with TestClient(webapp.create_app()) as client:
        r = client.patch("/api/transactions/2/category", json={"category_id": ziel})
        assert r.json()["updated"] == 2
        cats = {i["id"]: i["category"] for i in client.get("/api/transactions").json()["items"]}
        assert cats[1] == "Haushalt & Drogerie"
        assert cats[2] == "Haushalt & Drogerie"


def test_updated_zaehlt_nur_echte_aenderungen(tmp_path, monkeypatch) -> None:
    """Die gemeldete Zahl ist die der tatsächlich geänderten Zeilen."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-11.00", "h2"),
        ("REWE", "KARTENZAHLUNG REWE SAG", "-5.00", "h3"),
    ])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    with TestClient(webapp.create_app()) as client:
        erst = client.patch("/api/transactions/1/category", json={"category_id": ziel}).json()
        assert (erst["total"], erst["updated"], erst["verified"]) == (3, 3, 3)

        # Zweiter Aufruf: alles hat die Kategorie schon -> keine Änderung,
        # aber weiterhin 3 Treffer (das war früher irreführend als "angewandt").
        nochmal = client.patch("/api/transactions/1/category", json={"category_id": ziel}).json()
        assert (nochmal["total"], nochmal["updated"], nochmal["verified"]) == (3, 0, 3)


def test_zahlen_im_zweck_verhindern_treffer_nicht(tmp_path, monkeypatch) -> None:
    """Datum/Nummern im Verwendungszweck (Bankzusatz) dürfen nicht trennen."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("CARREFOUR.ETTERB/ETTERBEEK", "VISA Debitkartenumsatz vom 10.10.2026", "-1.98", "c1"),
        ("CARREFOUR.ETTERB/ETTERBEEK", "VISA Debitkartenumsatz vom 11.10.2026", "-16.70", "c2"),
    ])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    with TestClient(webapp.create_app()) as client:
        m = client.get("/api/transactions/1/matches").json()
        assert m["count"] == 2                      # trotz abweichendem Datum
        assert m["kriterium"]["purpose_key"] == "visa debitkartenumsatz vom #.#.#"

        res = client.patch("/api/transactions/1/category", json={"category_id": ziel}).json()
        assert (res["total"], res["updated"], res["verified"]) == (2, 2, 2)
        cats = {i["id"]: i["category"] for i in client.get("/api/transactions").json()["items"]}
        assert cats[2] == "Haushalt & Drogerie"


def test_alle_zahlenformen_werden_neutralisiert() -> None:
    assert kat.match_key("Shop", "Bestellung 2026-10-12")[1] == "bestellung #-#-#"
    assert kat.match_key("Shop", "Zahlung 03.11.26")[1] == "zahlung #.#.#"
    assert kat.match_key("Shop", "Rechnung Nr. 4711")[1] == "rechnung nr. #"


def test_rechnungsnummern_trennen_nicht_mehr(tmp_path, monkeypatch) -> None:
    """Zahlen generell raus: verschiedene Rechnungsnummern gelten als gleichartig."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("Versicherung AG", "Beitrag Rechnung 4711", "-99.00", "r1"),
        ("Versicherung AG", "Beitrag Rechnung 4712", "-99.00", "r2"),
    ])

    with TestClient(webapp.create_app()) as client:
        m = client.get("/api/transactions/1/matches").json()
    assert m["count"] == 2
    assert m["kriterium"]["purpose_key"] == "beitrag rechnung #"


def test_unterschiedlicher_partner_bleibt_getrennt(tmp_path, monkeypatch) -> None:
    """Zahlen-Ersatz darf nicht zu weit gehen: anderer Partner bleibt getrennt."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("CARREFOUR.ETTERB/ETTERBEEK", "Kartenzahlung 10.10.2026", "-1.98", "c1"),
        ("ALDI SUED", "Kartenzahlung 11.10.2026", "-16.70", "c2"),
    ])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    with TestClient(webapp.create_app()) as client:
        m = client.get("/api/transactions/1/matches").json()
        assert m["count"] == 1
        assert m["kriterium"]["purpose_key"] == "kartenzahlung #.#.#"


def test_regel_schluessel_werden_migriert(tmp_path, monkeypatch) -> None:
    """Alte Regeln (ohne Zahlen-Ersatz) greifen nach der Migration weiter."""
    import web.kategorien as kat

    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [("CARREFOUR.ETTERB/ETTERBEEK", "VISA Debitkartenumsatz vom 12.10.2026", "-9.99", "c1")])
    ziel = _cat_id(db, "Haushalt & Drogerie")

    conn = sqlite3.connect(db)
    kat.ensure_learned_rules_table(conn)
    conn.execute(
        "INSERT INTO learned_rules (partner_key, purpose_key, category_id, match_count)"
        " VALUES (?, ?, ?, 3)",   # Zustand vor der Umstellung
        ("carrefour.etterb/etterbeek", "visa debitkartenumsatz vom 10.10.2026", ziel),
    )
    conn.commit()
    assert kat.migrate_learned_rule_keys(conn) == 1
    conn.commit()
    key = kat.match_key("CARREFOUR.ETTERB/ETTERBEEK", "VISA Debitkartenumsatz vom 12.10.2026")
    regeln = kat.load_learned_rules(conn)
    conn.close()
    assert key in regeln and regeln[key] == "Haushalt & Drogerie"
    # zweiter Lauf ändert nichts mehr (idempotent)
    conn = sqlite3.connect(db)
    assert kat.migrate_learned_rule_keys(conn) == 0
    conn.close()


def test_matches_endpunkt_zeigt_kriterien(tmp_path, monkeypatch) -> None:
    """Der Diagnose-Endpunkt nennt Kriterium und alle gleichartigen Umsätze."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [
        ("DE03760300800870717444FELIX LEY", "UEBERTRAG / UEBERWEISUNG", "-750.00", "h1"),
        ("Felix Ley", "UEBERTRAG  /  UEBERWEISUNG", "-100.00", "h2"),   # nur Betrag/Format anders
        ("ALDI", "EINKAUF", "-20.00", "h3"),                            # anderer Partner -> kein Treffer
    ])

    with TestClient(webapp.create_app()) as client:
        body = client.get("/api/transactions/1/matches").json()
        assert body["count"] == 2
        assert body["kriterium"]["partner_key"] == "felix ley"
        assert {i["id"] for i in body["items"]} == {1, 2}
        assert {i["betrag"] for i in body["items"]} == {-750.0, -100.0}


def test_info_endpunkt_meldet_version_und_datenbank(tmp_path, monkeypatch) -> None:
    db = tmp_path / "t.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    _seed(db, [("REWE", "KARTENZAHLUNG REWE SAG", "-23.10", "h1")])

    with TestClient(webapp.create_app()) as client:
        info = client.get("/api/info").json()
    assert info["app_version"] == webapp.APP_VERSION
    assert info["db_path"] == str(db)
    assert info["db_exists"] is True
    assert info["transactions"] == 1
    assert info["uncategorized"] == 1      # der Seed-Umsatz ist ohne Kategorie


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
