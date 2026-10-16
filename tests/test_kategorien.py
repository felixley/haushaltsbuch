"""Tests für die automatische Kategorisierung (web/kategorien.py).

Die Fälle sind bewusst an echten comdirect/DKB-Umsätzen orientiert: Der
Verwendungszweck hängt die Felder ohne Trennzeichen aneinander
(z. B. ``...UBERWEISUNGMIETE``), sodass Muster per Teilstring greifen müssen.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web.kategorien import (
    DEFAULT_CATEGORIES,
    apply_categorization_to_db,
    categorize,
    ensure_default_categories,
)

# (Empfänger, Verwendungszweck, Betrag) -> erwartete Kategorie
FAELLE = [
    # --- Ausgaben -------------------------------------------------
    (("NETTO MARKEN-DISCOUNT", "LASTSCHRIFT / BELASTUNGNETTO MARKEN-DISCOUNT, KOELN", -7.24), "Lebensmittel"),
    (("ALDI SUED", "LASTSCHRIFT / BELASTUNGALDI SUED, DUESSELDORF", -7.95), "Lebensmittel"),
    (("REWE", "KARTENZAHLUNG REWE SAG", -23.10), "Lebensmittel"),
    (("BACKWERK KOELN", "LASTSCHRIFT / BELASTUNG", -1.50), "Lebensmittel"),
    (("SUELZER KEBAP", "LASTSCHRIFT / BELASTUNGSUELZER KEBAP, KOELN", -8.00), "Restaurants & Cafés"),
    (("SHELL 1945", "LASTSCHRIFT / BELASTUNGSHELL 1945, LOHNE", -2.14), "Tanken"),
    (("ARAL AG", "KARTENZAHLUNG TANKSTELLE", -60.00), "Tanken"),
    (("10525 GOTHAER VERSICHERUN", "LASTSCHRIFT / BELASTUNG", -20.00), "Versicherungen"),
    (("HETZNER ONLINE GMBH", "LASTSCHRIFT KUNDENNUMMER: K0735803826", -5.83), "Internet & Telefon"),
    (("KATHARINA KIEFER", "UEBERTRAG FREENET VERTRAG", -17.99), "Internet & Telefon"),
    (("PAYPAL EUROPE S.A.R.L. ET CIE S.C.A", "PP.7086.PP. SPOTIFYAB, IHR EINKAUF", -12.99), "Abos & Streaming"),
    (("AMAZON EU S.A R.L., NIEDERLASSUNG DEUTSCHLAND", "028-7593541 AMAZON.DE", -74.90), "Elektronik & Technik"),
    (("GEMEINSCHAFT DKB", "UEBERTRAG / UEBERWEISUNGMIETE", -1580.00), "Miete & Wohnen"),
    (("", "KONTOFUEHRUNGSENTGELTENTGELTVISA-KREDITKARTE", -1.90), "Bankgebühren"),
    (("MATHILDA SCALABLE CAPITAL", "UEBERTRAG VERIFIZIERUNG", -10.00), "Geldanlagen & Sparen"),
    (("URBAN SPORTS GMBH", "LASTSCHRIFT URBAN SPORTS CLUB", -66.00), "Freizeit & Hobby"),
    (("NetCologne", "LASTSCHRIFT / BELASTUNG", -34.99), "Internet & Telefon"),
    (("BAUHAUS", "KARTENZAHLUNG", -42.00), "Haushalt & Drogerie"),
    (("ROSSMANN", "KARTENZAHLUNG", -11.20), "Haushalt & Drogerie"),
    (("ZALANDO SE", "KAUF AUF RECHNUNG", -59.90), "Kleidung & Schuhe"),
    # --- Einnahmen ------------------------------------------------
    (("BARMENIAGOTHAER AG", "UEBERTRAG LOHN/GEHALT 00545764/202607", 3920.08), "Gehalt & Lohn"),
    (("BUNDESKASSE - DIENSTORT TRIER -", "UEBERTRAG ELTERNGELD", 900.00), "Elterngeld"),
    (("FAMILIENKASSE", "UEBERTRAG KINDERGELD", 255.00), "Kindergeld"),
    (("STEUERVERWALTUNG NRW", "EINKOMMENST. 2023 BESCHEID", 1020.05), "Steuererstattung"),
    (("AMAZON EU S.A R.L.", "UEBERTRAG 028-1531178 AMAZON.DE", 55.99), "Erstattungen & Rückzahlungen"),
    # --- Umbuchungen / unklar ------------------------------------
    (("FELIX LEY", "UEBERTRAG / UEBERWEISUNG", 750.00), "Transfers (intern)"),
    (("FELIX DKB", "UEBERTRAG / UEBERWEISUNG", -750.00), "Transfers (intern)"),
    (("PAYPAL EUROPE S.A.R.L. ET CIE S.C.A", "PP.7086.PP. , IHR EINKAUF BEI", -53.90), None),
    (("VOELLIG UNBEKANNTER LADEN XYZ", "LASTSCHRIFT", -12.00), None),
]


@pytest.mark.parametrize("daten, erwartet", FAELLE)
def test_categorize(daten, erwartet) -> None:
    name, zweck, betrag = daten
    assert categorize(name, zweck, betrag) == erwartet


def test_kategorie_hat_gueltigen_typ() -> None:
    """Alle Vorschlagskategorien haben den Typ income oder expense."""
    assert DEFAULT_CATEGORIES, "Es müssen Kategorien definiert sein"
    assert {t for _, t in DEFAULT_CATEGORIES} <= {"income", "expense"}


def test_kategorie_ohne_betrag() -> None:
    """Ohne Betrag (None) wird nicht als Einnahme behandelt -> reine Regelprüfung."""
    assert categorize("SHELL 1945", "LASTSCHRIFT", None) == "Tanken"
    # Unbekanntes bleibt ohne Kategorie (None), keine falsche Zuordnung.
    assert categorize("XYZ UNBEKANNT", "LASTSCHRIFT", None) is None


def test_names_schraenkt_auf_vorhandene_kategorien_ein() -> None:
    """Gibt es eine Kategorie nicht, wird sie nicht vorgeschlagen."""
    assert categorize("SHELL 1945", "", -1.0, names={"Lebensmittel"}) is None
    assert categorize("SHELL 1945", "", -1.0, names={"Tanken"}) == "Tanken"


def test_categorize_wirft_nie() -> None:
    """Robustheit: kaputte/leere Eingaben dürfen keinen Fehler auslösen."""
    for args in [(None, None, None), ("", "", 0), ("###", "", -1)]:
        categorize(*args)  # darf nicht werfen
def test_categorize_wirft_nie() -> None:
    """Robustheit: kaputte/leere Eingaben dürfen keinen Fehler auslösen."""
    for args in [(None, None, None), ("", "", 0), ("###", "", -1)]:
        categorize(*args)  # darf nicht werfen


# ---------------------------------------------------------------------------
# Datenbank: Kategorien anlegen + zuordnen
# ---------------------------------------------------------------------------
def _seed_transactions(db) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(webapp.SCHEMA_FILE.read_text(encoding="utf-8"))
    ensure_default_categories(conn)
    conn.execute(
        "INSERT OR IGNORE INTO accounts (iban, bank_name) VALUES (?, ?)",
        ("DE00000000000000000001", "Testbank"),
    )
    rows = [
        ("SHELL 1945", "LASTSCHRIFT / BELASTUNGSHELL", "-40.00", "h1"),
        ("NETTO MARKEN-DISCOUNT", "BELASTUNGNETTO MARKEN-DISCOUNT", "-7.24", "h2"),
        ("VOELLIG UNBEKANNT", "LASTSCHRIFT", "-3.00", "h3"),
    ]
    for name, zweck, betrag, h in rows:
        conn.execute(
            """INSERT INTO transactions
                   (account_iban, booking_date, amount, currency, applicant_name, purpose, raw, dedup_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("DE00000000000000000001", "2026-10-01", betrag, "EUR", name, zweck, "{}", h),
        )
    conn.commit()
    conn.close()


def test_ensure_default_categories_is_idempotent(tmp_path) -> None:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(webapp.SCHEMA_FILE.read_text(encoding="utf-8"))
    ensure_default_categories(conn)
    first = conn.execute("SELECT count(*) FROM categories").fetchone()[0]
    ensure_default_categories(conn)  # zweiter Lauf darf nichts verdoppeln
    second = conn.execute("SELECT count(*) FROM categories").fetchone()[0]
    conn.close()
    assert first == len(DEFAULT_CATEGORIES)
    assert second == first


def _category_of(db, applicant_name: str) -> str | None:
    conn = sqlite3.connect(db)
    row = conn.execute(
        """SELECT c.name FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.applicant_name = ?""",
        (applicant_name,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_apply_categorization_to_db(tmp_path) -> None:
    db = tmp_path / "t.db"
    _seed_transactions(db)

    scanned, changed = apply_categorization_to_db(db)
    assert scanned == 3
    assert changed == 2  # SHELL -> Tanken, NETTO -> Lebensmittel; Unbekannt bleibt leer

    assert _category_of(db, "SHELL 1945") == "Tanken"
    assert _category_of(db, "NETTO MARKEN-DISCOUNT") == "Lebensmittel"
    assert _category_of(db, "VOELLIG UNBEKANNT") is None


def test_apply_categorization_keeps_manual_by_default(tmp_path) -> None:
    """Manuell gesetzte Kategorien bleiben ohne only_uncategorized=False erhalten."""
    db = tmp_path / "t.db"
    _seed_transactions(db)

    conn = sqlite3.connect(db)
    manual = conn.execute("SELECT id FROM categories WHERE name = ?", ("Barbeträge",)).fetchone()[0]
    conn.execute(
        "UPDATE transactions SET category_id = ? WHERE applicant_name = ?",
        (manual, "SHELL 1945"),
    )
    conn.commit()
    conn.close()

    apply_categorization_to_db(db)  # Default: nur leere Zeilen
    assert _category_of(db, "SHELL 1945") == "Barbeträge"  # manuell -> unverändert

    apply_categorization_to_db(db, only_uncategorized=False)  # neu bewerten
    assert _category_of(db, "SHELL 1945") == "Tanken"


def test_apply_categorization_dry_run_writes_nothing(tmp_path) -> None:
    db = tmp_path / "t.db"
    _seed_transactions(db)
    scanned, changed = apply_categorization_to_db(db, dry_run=True)
    assert scanned == 3 and changed == 2

    conn = sqlite3.connect(db)
    assigned = conn.execute("SELECT count(*) FROM transactions WHERE category_id IS NOT NULL").fetchone()[0]
    conn.close()
    assert assigned == 0  # Dry-Run schreibt nichts


# ---------------------------------------------------------------------------
# Integration: FastAPI-Endpunkte
# ---------------------------------------------------------------------------
def test_api_categories_and_categorize(tmp_path, monkeypatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)

    with TestClient(webapp.create_app()) as client:
        _seed_transactions(db)

        # Start legt die Vorschlagskategorien an -> Auswahlliste ist gefüllt.
        cats = client.get("/api/categories").json()
        assert any(c["name"] == "Lebensmittel" for c in cats)

        r = client.post("/api/categorize", json={}).json()
        assert r["ok"] is True
        assert r["changed"] == 2

        data = client.get("/api/transactions").json()
        by_name = {i["partner_name"]: i["category"] for i in data["items"]}
        assert by_name["SHELL 1945"] == "Tanken"

        # Zweiter Lauf ändert nichts mehr (idempotent für leere Zeilen).
        r2 = client.post("/api/categorize", json={}).json()
        assert r2["changed"] == 0

