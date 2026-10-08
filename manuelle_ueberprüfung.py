"""Minimales Setup: Daten aus der SQLite-DB mit pandas lesen.

Kleines Spielwiese-Skript zur manuellen Überprüfung. Es verbindet sich
mit der von ``transaktionen_speichern.py`` befüllten Datenbank und liest
die Tabellen ``accounts`` und ``transactions`` als pandas-DataFrame.

Anwendung
---------
    python manuelle_ueberprüfung.py

Der Pfad zur Datenbank kommt aus ``DB_PATH`` (siehe .env), sonst
``finance.db``.
"""

import os
import sqlite3

import pandas as pd

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "finance.db")

# Sperrige Spalten, die die Konsolenausgabe unlesbar machen würden.
# Bei Bedarf einfach aus der Liste entfernen, um sie mitzuzeigen.
UNTERDRUECKTE_SPALTEN = ["raw", "dedup_hash"]


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Verbindung zur SQLite-Datenbank öffnen."""
    return sqlite3.connect(db_path)


def read_accounts(connection: sqlite3.Connection) -> pd.DataFrame:
    """Alle Konten als DataFrame."""
    return pd.read_sql_query("SELECT * FROM accounts", connection)


def read_transactions(connection: sqlite3.Connection, limit: int = 20) -> pd.DataFrame:
    """Die neuesten Umsätze als DataFrame (mit Konto/Unterkonto, ohne Roh-JSON)."""
    df = pd.read_sql_query(
        "SELECT t.id, t.account_iban, a.subaccount, t.booking_date, "
        "t.entry_date, t.amount, t.currency, t.status, "
        "t.applicant_name, t.purpose, "
        "t.customer_reference, t.bank_reference, t.extra_details, "
        "t.dedup_hash, t.created_at "
        "FROM transactions t "
        "LEFT JOIN accounts a ON a.iban = t.account_iban "
        "ORDER BY t.booking_date DESC, t.id DESC LIMIT ?",
        connection,
        params=[limit],
    )
    return df.drop(columns=UNTERDRUECKTE_SPALTEN, errors="ignore")


def main() -> None:
    connection = connect()
    try:
        accounts_df = read_accounts(connection)
        transactions_df = read_transactions(connection)

        print("=== Konten ===")
        print(accounts_df.to_string(index=False))

        print("\n=== Umsätze (neueste zuerst) ===")
        print(transactions_df.to_string(index=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()

