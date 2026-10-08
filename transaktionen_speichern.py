"""Umsätze von der Bank abrufen und in eine SQLite-Datenbank schreiben.

Dieses Skript basiert auf dem bewährten ``testverbindung.py``: Die
Verbindungslogik (FinTS-Login, photoTAN-Freigabe, Konten- und
Umsatzabruf) ist identisch. Zusätzlich wird für jeden Account das
von der Bank gelieferte ``data``-Dict jeder Transaktion
(``results[].data``) strukturiert in die SQLite-Datenbank
geschrieben.

Anwendung
---------
    python transaktionen_speichern.py             # echter Import
    python transaktionen_speichern.py --dry-run   # nur anzeigen, nichts schreiben
    python transaktionen_speichern.py --days 365  # Zeitraum in Tagen (Default 100)

Den Inhalt der Datenbank kannst du mit ``db_anzeigen.py`` einsehen.
"""

import argparse
import datetime
import getpass
import hashlib
import json
import os
import sqlite3
import tempfile
import webbrowser
from decimal import Decimal

from fints.utils import minimal_interactive_cli_bootstrap, decode_phototan_image
from fints.client import FinTS3PinTanClient, NeedTANResponse

# .env laden (Werte aus .env.example-Vorlage eintragen)
from dotenv import load_dotenv
load_dotenv()

# FinTS-Produkt-ID (wird von CLI und Web-Abruf gemeinsam genutzt)
PRODUCT_ID = "6151256F3D4F9975B877BD4A2"

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Tabellenspalten, die aus dem data-Dict gefüllt werden.
COLUMNS = [
    "account_iban",
    "booking_date",
    "entry_date",
    "amount",
    "currency",
    "status",
    "customer_reference",
    "bank_reference",
    "extra_details",
    "applicant_name",
    "purpose",
    "raw",
    "dedup_hash",
]


# ------------------------------------------------------------------
# Datenbank
# ------------------------------------------------------------------
def apply_schema(connection: sqlite3.Connection) -> None:
    """Wendet schema.sql an (idempotent, kann beliebig oft laufen)."""
    with open(SCHEMA_FILE, "r", encoding="utf-8") as handle:
        connection.executescript(handle.read())
    _migrate(connection)
    connection.commit()


# Pflichtspalten, die in älteren Datenbanken noch fehlen können.
# Werden idempotent nachgerüstet (NOT NULL DEFAULT '' ist bei ALTER TABLE
# erlaubt und belegt bestehende Zeilen mit '').
_REQUIRED_MIGRATIONS = [
    ("transactions", "applicant_name", "TEXT NOT NULL DEFAULT ''"),
    ("transactions", "purpose", "TEXT NOT NULL DEFAULT ''"),
]

# Veraltete Spalten, die in früheren Versionen angelegt wurden, aber nie
# mit Daten gefüllt worden sind (die Bankdaten enthalten diese Schlüssel
# nicht). Sie werden aus bestehenden Datenbanken entfernt.
_OBSOLETE_COLUMNS = [
    ("transactions", "transaction_type"),
    ("transactions", "transaction_details"),
]


def _migrate(connection: sqlite3.Connection) -> None:
    """Ergänzt Pflichtspalten in bestehenden Datenbanken (idempotent).

    Entfernt zusätzlich veraltete Spalten, die nie mit Daten gefüllt
    wurden (siehe ``_OBSOLETE_COLUMNS``).
    """
    for table, column, definition in _REQUIRED_MIGRATIONS:
        existing = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
            print(f"-> Spalte '{column}' in '{table}' ergänzt.")

    for table, column in _OBSOLETE_COLUMNS:
        existing = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column in existing:
            try:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                print(f"-> Veraltete Spalte '{column}' aus '{table}' entfernt.")
            except sqlite3.OperationalError:
                # DROP COLUMN wird erst ab SQLite 3.35 unterstützt.
                # Ältere Datenbanken behalten die Spalte; sie bleibt dann
                # einfach leer und wird nicht mehr beschrieben.
                print(f"-> Hinweis: Spalte '{column}' in '{table}' konnte "
                      f"nicht entfernt werden (SQLite zu alt).")


def _to_iso_date(value):
    """Wandelt ein date/datetime (oder String) in 'YYYY-MM-DD' um (oder None)."""
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()[:10]
    return str(value)


def _to_decimal(value):
    """Wandelt einen Betrag in einen Decimal-String um (oder None).

    ``results[].data['amount']`` ist ein mt940 ``Amount``-Objekt mit
    ``.amount`` (Decimal) und ``.currency``. Zur Sicherheit werden auch
    reine Zahlen/Decimal-Werte akzeptiert.
    """
    if value is None:
        return None
    inner = getattr(value, "amount", value)  # Amount-Objekt -> .amount
    if inner is None:
        return None
    try:
        return str(Decimal(str(inner)))
    except Exception:
        return str(inner)


def _to_currency(data: dict):
    """Liest die Währung, egal ob direkt oder im Amount-Objekt."""
    currency = data.get("currency")
    if currency:
        return str(currency)
    amount = data.get("amount")
    inner = getattr(amount, "currency", None)
    return str(inner) if inner else None


def _to_text(value):
    """Macht aus einem Wert einen Text (oder None), None bleibt None."""
    if value is None:
        return None
    return str(value)


def _to_text_required(value):
    """Wie ``_to_text``, aber für Pflichtfelder: None wird zu einem leeren String.

    So ist gewährleistet, dass die NOT-NULL-Spalten (``applicant_name``,
    ``purpose``) auch dann befüllt werden, wenn die Bank das Feld nicht
    liefert.
    """
    if value is None:
        return ""
    return str(value)


def _make_dedup_hash(account_iban: str, data: dict) -> str:
    """Baut einen stabilen Hash zur Duplikaterkennung eines Umsatzes.

    Basis sind Konto, Buchungsdatum, Betrag, Währung, Status sowie die
    vorhandenen Referenzen. Dadurch wird derselbe Umsatz bei einem
    erneuten Import nicht doppelt angelegt.
    """
    parts = [
        account_iban or "",
        _to_iso_date(data.get("date")) or "",
        _to_decimal(data.get("amount")) or "",
        _to_currency(data) or "",
        _to_text(data.get("status")) or "",
        _to_text(data.get("customer_reference")) or "",
        _to_text(data.get("bank_reference")) or "",
        _to_text(data.get("extra_details")) or "",
    ]
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_from_data(account_iban: str, data: dict) -> dict:
    """Baut aus einem data-Dict eine DB-Zeile (alle COLUMNS gefüllt)."""
    try:
        raw = json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        raw = json.dumps({str(k): str(v) for k, v in data.items()}, ensure_ascii=False)

    return {
        "account_iban": account_iban,
        "booking_date": _to_iso_date(data.get("date")),
        "entry_date": _to_iso_date(data.get("entry_date")),
        "amount": _to_decimal(data.get("amount")),
        "currency": _to_currency(data),
        "status": _to_text(data.get("status")),
        "customer_reference": _to_text(data.get("customer_reference")),
        "bank_reference": _to_text(data.get("bank_reference")),
        "extra_details": _to_text(data.get("extra_details")),
        "applicant_name": _to_text_required(data.get("applicant_name")),
        "purpose": _to_text_required(data.get("purpose")),
        "raw": raw,
        "dedup_hash": _make_dedup_hash(account_iban, data),
    }


def upsert_account(connection: sqlite3.Connection, account) -> None:
    """Legt das Konto an bzw. aktualisiert es (idempotent)."""
    connection.execute(
        """
        INSERT INTO accounts (iban, bic, accountnumber, subaccount, blz)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(iban) DO UPDATE SET
            bic = excluded.bic,
            accountnumber = excluded.accountnumber,
            subaccount = excluded.subaccount,
            blz = excluded.blz,
            updated_at = datetime('now')
        """,
        (
            account.iban,
            getattr(account, "bic", None),
            getattr(account, "accountnumber", None),
            getattr(account, "subaccount", None),
            getattr(account, "blz", None),
        ),
    )


def insert_transactions(connection: sqlite3.Connection, account_iban: str, data_dicts) -> tuple:
    """Schreibt mehrere Umsätze; Duplikate werden übersprungen.

    :return: (neu_eingefuegt, uebersprungen)
    """
    placeholders = ", ".join("?" for _ in COLUMNS)
    sql = (
        f"INSERT INTO transactions ({', '.join(COLUMNS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(dedup_hash) DO NOTHING"
    )

    rows = [_row_from_data(account_iban, data) for data in data_dicts]

    inserted = 0
    for row in rows:
        cursor = connection.execute(sql, tuple(row[col] for col in COLUMNS))
        inserted += cursor.rowcount  # 1 bei Insert, 0 bei Konflikt

    skipped = len(rows) - inserted
    return inserted, skipped


# ------------------------------------------------------------------
# FinTS (aus testverbindung.py übernommen)
# ------------------------------------------------------------------
def show_image(image_bytes, mime_type="image/png"):
    """Schreibt die Bilddaten in eine Temp-Datei und öffnet sie im Standard-Bildbetrachter."""
    suffix = "." + mime_type.split("/")[-1].replace("jpeg", "jpg") if "/" in mime_type else ".png"
    path = os.path.join(tempfile.gettempdir(), "fints_phototan" + suffix)
    with open(path, "wb") as writer:
        writer.write(image_bytes)
    print(f"-> Bild gespeichert: {path}")
    webbrowser.open("file://" + path)


def handle_decoupled_tan(f, response):
    """photoTAN-Freigabe abbilden und TAN einsenden (wie in testverbindung.py)."""
    print("\n--- photoTAN-Grafik angefordert ---")

    # Fall 1: Bank liefert die Matrix direkt als challenge_matrix = [mime_type, binäre Bilddaten]
    if getattr(response, 'challenge_matrix', None):
        mime_type = response.challenge_matrix[0]
        matrix_data = response.challenge_matrix[1]  # Index 1 = die eigentlichen Bilddaten!
        print(f"Grafik gefunden (MIME: {mime_type}). Öffne Bild...")
        try:
            show_image(matrix_data, mime_type)
            print("-> Bitte scannen Sie die obige Grafik mit Ihrer photoTAN-App!")
        except Exception as e:
            print(f"Fehler beim Rendern der Grafik: {e}")
            print("Alternativer Text-Challenge:", response.challenge)
    # Fall 2: comdirect liefert die Challenge oft im challenge_hhduc (muss dekodiert werden)
    elif getattr(response, 'challenge_hhduc', None):
        print("challenge_hhduc gefunden. Dekodiere Challenge...")
        try:
            decoded = decode_phototan_image(response.challenge_hhduc)
            print(f"Dekodiert (MIME: {decoded['mime_type']}). Öffne Bild...")
            show_image(decoded['image'], decoded['mime_type'])
            print("-> Bitte scannen Sie die obige Grafik mit Ihrer photoTAN-App!")
        except Exception as e:
            print(f"Fehler beim Dekodieren der hhduc-Challenge: {e}")
            print("Alternativer Text-Challenge:", response.challenge)
    else:
        # Fallback, falls nur Text geliefert wird
        print(response.challenge)

    # TAN vom Benutzer abfragen statt leerem String
    tan_code = input("\nBitte geben Sie die in der photoTAN-App angezeigte TAN ein: ").strip()

    print("Sende Bestätigung an die Bank...")
    response = f.send_tan(response, tan_code)
    return response


# ------------------------------------------------------------------
# Hauptablauf
# ------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Umsätze abrufen und in die SQLite-Datenbank schreiben.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Umsätze nur abrufen und anzeigen, nichts in die DB schreiben.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=100,
        help="Zeitraum rückwärts in Tagen (Default: 100).",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "finance.db"),
        help="Pfad zur SQLite-Datei (Default: DB_PATH aus .env oder finance.db).",
    )
    parser.add_argument(
        "--bank",
        default=None,
        choices=["comdirect", "dkb"],
        help="Bank für den Umsatzabruf (überschreibt BANK aus .env). "
             "Möglich: comdirect, dkb.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = args.db

    # Bank wählen: --bank von der Kommandozeile hat Vorrang vor BANK aus .env.
    bank = (args.bank or os.getenv('BANK', 'comdirect')).lower()
    print(f"Bank: {bank}")

    # Zugangsdaten für die gewählte Bank aus der .env lesen
    # Die PIN wird bewusst NICHT aus der .env gelesen, sondern
    # sicher (ohne Echo) per getpass zur Laufzeit abgefragt.
    prefix = bank.upper()
    credentials = {
        'blz': os.getenv(f'{prefix}_BLZ'),
        'username': os.getenv(f'{prefix}_USERNAME'),
        'hbci': os.getenv(f'{prefix}_HBCI'),
        'tan_mechanism': os.getenv(f'{prefix}_TAN_MECHANISM'),
    }

    missing = [key for key, value in credentials.items() if not value]
    if missing:
        raise SystemExit(
            f"Fehlende Zugangsdaten für '{bank}' in .env: "
            f"{', '.join(prefix + '_' + m.upper() for m in missing)}. Siehe .env.example"
        )

    blz = credentials['blz']
    username = credentials['username']
    hbci_backend = credentials['hbci']
    tan_mechanism = credentials['tan_mechanism']

    # PIN verdeckt (ohne Bildschirm-Echo) abfragen
    password = getpass.getpass(f"PIN für {bank} ({username}): ")

    # Nutzen Sie die funktionierende Produkt-ID
    product_id = PRODUCT_ID

    client = FinTS3PinTanClient(blz,
                                username,
                                password,
                                hbci_backend,
                                product_id=product_id)

    # VOR dem Login-Dialog: Verfügbare TAN-Medien laden und das Verfahren setzen
    client.fetch_tan_mechanisms()
    client.set_tan_mechanism(tan_mechanism)

    minimal_interactive_cli_bootstrap(client)

    # Datenbank vorbereiten (im Dry-Run gar nicht erst verbinden)
    connection = None
    if args.dry_run:
        print("*** DRY-RUN: Es wird nichts in die Datenbank geschrieben. ***")
    else:
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        apply_schema(connection)
        print(f"Datenbank bereit: {os.path.abspath(db_path)}")

    start_date = datetime.datetime.now() - datetime.timedelta(days=args.days)
    end_date = datetime.datetime.now()

    total_new = 0
    total_skipped = 0

    try:
        with client:
            # Erster Login-Schritt (SCA / 90-Tage-Freigabe)
            if isinstance(client.init_tan_response, NeedTANResponse):
                client.init_tan_response = handle_decoupled_tan(client, client.init_tan_response)

            # Konten abrufen
            accounts = client.get_sepa_accounts()
            for account in accounts:
                print(f"\nLese Umsätze für {account.iban}...")
                result = client.get_transactions(account,
                                                 start_date=start_date,
                                                 end_date=end_date)

                # Falls für den Umsatzabruf erneut eine Freigabe nötig ist
                if isinstance(result, NeedTANResponse):
                    result = handle_decoupled_tan(client, result)

                print(f"Umsätze erfolgreich empfangen: {len(result)} Transaktionen für {account.iban}")

                data_dicts = [txn.data for txn in result]

                if args.dry_run:
                    for data in data_dicts:
                        print(f"  [dry-run] {data.get('date')} "
                              f"{data.get('amount')} {data.get('purpose')}")
                    continue

                # Konto anlegen/aktualisieren, dann Umsätze schreiben
                upsert_account(connection, account)
                inserted, skipped = insert_transactions(connection, account.iban, data_dicts)
                connection.commit()

                total_new += inserted
                total_skipped += skipped
                print(f"-> {inserted} neue Umsätze gespeichert, "
                      f"{skipped} Duplikate übersprungen.")
    finally:
        if connection is not None:
            connection.close()

    if not args.dry_run:
        print(f"\nFertig. {total_new} neue Umsätze geschrieben, "
              f"{total_skipped} bereits vorhanden (übersprungen).")
        print("Zum Ansehen: python db_anzeigen.py")


if __name__ == "__main__":
    main()