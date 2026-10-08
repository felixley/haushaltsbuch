"""Inhalt der SQLite-Umsatzdatenbank ansehen (mit pandas).

Liest die von ``transaktionen_speichern.py`` befüllte Datenbank aus und
zeigt die Tabellen als pandas-DataFrame an.

Anwendung
---------
    python db_anzeigen.py                      # Konten + letzte 50 Umsätze
    python db_anzeigen.py --accounts           # nur Kontenübersicht
    python db_anzeigen.py --limit 200          # mehr Umsätze zeigen
    python db_anzeigen.py --account DE12...    # nur ein Konto
    python db_anzeigen.py --sql "SELECT ..."   # freie SQL-Abfrage
    python db_anzeigen.py --csv export.csv     # Ergebnis als CSV speichern
"""

import argparse
import os
import sqlite3

import pandas as pd

from dotenv import load_dotenv
load_dotenv()

# pandas so einstellen, dass auch größere Ergebnismengen vollständig
# dargestellt werden (kein Abschneiden mit "...").
pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 0)
pd.set_option("display.max_colwidth", 60)

# Spalten, die in der Umsatzansicht standardmäßig gezeigt werden.
TRANSACTION_COLUMNS = [
    "booking_date",
    "entry_date",
    "amount",
    "currency",
    "status",
    "applicant_name",
    "purpose",
    "customer_reference",
    "bank_reference",
    "account_iban",
    "subaccount",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inhalt der SQLite-Umsatzdatenbank ansehen (mit pandas).",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "finance.db"),
        help="Pfad zur SQLite-Datei (Default: DB_PATH aus .env oder finance.db).",
    )
    parser.add_argument(
        "--accounts",
        action="store_true",
        help="Nur die Kontenübersicht zeigen.",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Nur Umsätze dieses Kontos (IBAN) zeigen.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Anzahl der Umsätze, die gezeigt werden (Default: 50).",
    )
    parser.add_argument(
        "--sql",
        default=None,
        help="Freie (read-only) SQL-Abfrage ausführen und Ergebnis anzeigen.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Ergebnis zusätzlich als CSV in diese Datei schreiben.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Statt der Umsatzliste die kompletten Roh-JSON-Daten der letzten Umsätze zeigen.",
    )
    return parser.parse_args()


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(
            f"Datenbank '{db_path}' nicht gefunden. "
            f"Zuerst 'python transaktionen_speichern.py' ausführen "
            f"oder --db <pfad> angeben."
        )
    return sqlite3.connect(db_path)


def _print_df(df: pd.DataFrame, title: str) -> None:
    """Gibt einen Titel und den DataFrame als Tabelle aus."""
    print(f"\n=== {title} ===")
    if df.empty:
        print("(keine Einträge)")
        return
    print(df.to_string(index=False))
    print(f"({len(df)} Zeile(n))")


def read_accounts(connection: sqlite3.Connection) -> pd.DataFrame:
    """Kontenübersicht inkl. Anzahl Umsätze und Summe je Konto."""
    sql = """
        SELECT
            a.iban,
            a.bic,
            a.accountnumber,
            a.subaccount,
            a.blz,
            COUNT(t.id)         AS anzahl_umsaetze,
            COALESCE(SUM(CAST(t.amount AS REAL)), 0) AS summe
        FROM accounts a
        LEFT JOIN transactions t ON t.account_iban = a.iban
        GROUP BY a.iban
        ORDER BY a.iban
    """
    return pd.read_sql_query(sql, connection)


def read_transactions(connection: sqlite3.Connection, account_iban=None, limit=50) -> pd.DataFrame:
    """Umsätze (neueste zuerst), optional auf ein Konto gefiltert."""
    params = []
    where = ""
    if account_iban:
        where = "WHERE t.account_iban = ?"
        params.append(account_iban)

    sql = (
        "SELECT t.booking_date, t.entry_date, t.amount, t.currency, t.status, "
        "t.applicant_name, "
        "t.purpose, t.customer_reference, t.bank_reference, "
        "t.account_iban, a.subaccount "
        "FROM transactions t "
        "LEFT JOIN accounts a ON a.iban = t.account_iban "
        f"{where} "
        "ORDER BY t.booking_date DESC, t.id DESC "
        "LIMIT ?"
    )
    params.append(limit)
    df = pd.read_sql_query(sql, connection, params=params)
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df


def read_raw(connection: sqlite3.Connection, limit=10) -> pd.DataFrame:
    """Die kompletten Rohdaten (raw JSON) der letzten Umsätze."""
    sql = (
        "SELECT booking_date, amount, currency, applicant_name, "
        "account_iban, raw "
        "FROM transactions "
        "ORDER BY booking_date DESC, id DESC "
        "LIMIT ?"
    )
    return pd.read_sql_query(sql, connection, params=[limit])


def run_free_sql(connection: sqlite3.Connection, sql: str) -> pd.DataFrame:
    """Führt eine Abfrage aus. Nur lesende Statements erlaubt."""
    lowered = sql.strip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with") or lowered.startswith("pragma")):
        raise SystemExit("Aus Sicherheitsgründen sind nur SELECT/WITH/PRAGMA-Abfragen erlaubt.")
    return pd.read_sql_query(sql, connection)


def main():
    args = parse_args()
    connection = _connect(args.db)

    try:
        # Freie SQL-Abfrage
        if args.sql:
            df = run_free_sql(connection, args.sql)
            _print_df(df, "Ergebnis der Abfrage")
            if args.csv:
                df.to_csv(args.csv, index=False)
                print(f"-> als CSV gespeichert: {args.csv}")
            return

        # Kontenübersicht (immer zeigen, außer bei reinem Umsatzfilter)
        if args.accounts or not (args.account or args.raw):
            accounts_df = read_accounts(connection)
            _print_df(accounts_df, "Konten")
            if args.accounts:
                if args.csv:
                    accounts_df.to_csv(args.csv, index=False)
                    print(f"-> als CSV gespeichert: {args.csv}")
                return

        # Roh-JSON-Ansicht
        if args.raw:
            raw_df = read_raw(connection, args.limit)
            _print_df(raw_df, f"Rohdaten (letzte {args.limit})")
            if args.csv:
                raw_df.to_csv(args.csv, index=False)
                print(f"-> als CSV gespeichert: {args.csv}")
            return

        # Umsätze
        transactions_df = read_transactions(connection, args.account, args.limit)
        title = f"Umsätze (letzte {args.limit}"
        title += f" für {args.account})" if args.account else ")"
        _print_df(transactions_df, title)

        if args.csv:
            transactions_df.to_csv(args.csv, index=False)
            print(f"-> als CSV gespeichert: {args.csv}")

        # Kleine Zusammenfassung
        if not transactions_df.empty:
            einnahmen = transactions_df.loc[transactions_df["amount"] > 0, "amount"].sum()
            ausgaben = transactions_df.loc[transactions_df["amount"] < 0, "amount"].sum()
            print(f"\nSumme Einnahmen: {einnahmen:.2f} | Summe Ausgaben: {ausgaben:.2f} "
                  f"| Saldo: {einnahmen + ausgaben:.2f}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()