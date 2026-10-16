"""Einmalige Bereinigung: IBAN aus ``transactions.applicant_name`` entfernen.

Manche Banken hängen die IBAN des Gegenkontos direkt an den Partner-Namen
(siehe ``web/partner.py``). Für **bereits bestehende** Einträge in der
Datenbank sorgt dieses Skript dafür, dass dieselbe Bereinigung angewendet
wird, die das Frontend bei der Anzeige vornimmt.

Das Skript ist idempotent: bereits bereinigte Namen bleiben unverändert,
ein zweiter Lauf ändert nichts mehr (es wird nur geschrieben, wenn sich der
Wert tatsächlich unterscheidet).

Aufruf (vom Projektordner aus)::

    python migration/clean_partner_names.py                # nutzt DB_PATH/.env (bzw. finance.db)
    python migration/clean_partner_names.py --db finance.db
    python migration/clean_partner_names.py --dry-run      # nur anzeigen, nichts schreiben
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from web.partner import clean_partner_name  # noqa: E402


def _resolve_db_path(arg: str | None) -> Path:
    """Ermittelt den DB-Pfad: CLI-Argument > DB_PATH aus .env > finance.db."""
    if arg:
        path = Path(arg)
        return path if path.is_absolute() else ROOT / path
    _env = os.getenv("DB_PATH", "finance.db")
    path = Path(_env)
    return path if path.is_absolute() else ROOT / path


def clean_database(db_path: Path, dry_run: bool = False) -> tuple[int, int]:
    """Bereinigt alle ``applicant_name``-Werte; liefert (geprueft, geaendert).

    Es wird nur geschrieben, wenn sich der Wert tatsächlich ändert, damit
    unbeteiligte Einträge unangetastet bleiben.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, applicant_name FROM transactions").fetchall()
        scanned = len(rows)
        changed = 0
        for row in rows:
            original = row["applicant_name"] or ""
            cleaned = clean_partner_name(original)
            if cleaned != original:
                changed += 1
                print(f"  #{row['id']}: {original!r} -> {cleaned!r}")
                if not dry_run:
                    conn.execute(
                        "UPDATE transactions SET applicant_name = ? WHERE id = ?",
                        (cleaned, row["id"]),
                    )
        if not dry_run:
            conn.commit()
        return scanned, changed
    finally:
        conn.close()


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(
        description="Entfernt IBAN-artige Teilstrings aus transactions.applicant_name.",
    )
    ap.add_argument("--db", default=None, help="Pfad zur SQLite-DB (Default: DB_PATH/.env, sonst finance.db)")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben.")
    args = ap.parse_args()

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Datenbank nicht gefunden: {db_path}")

    if args.dry_run:
        print("*** DRY-RUN: Es wird nichts in die Datenbank geschrieben. ***")
    print(f"Datenbank: {db_path}")

    scanned, changed = clean_database(db_path, dry_run=args.dry_run)
    verb = "würden bereinigt" if args.dry_run else "bereinigt"
    print(f"\nFertig. {scanned} Umsätze geprüft, {changed} {verb}.")


if __name__ == "__main__":
    main()
