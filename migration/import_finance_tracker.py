"""Einmaliger Import: finance-tracker-DB (altes Schema) -> Haushaltsbuch-DB.

Liest die Quelle read-only und schreibt idempotent in die Ziel-DB.
Achtung: Die Quelle enthält Mock-Daten (source: mock), keine echten Bankumsätze.

Aufruf (vom Projektordner aus):
    python migration/import_finance_tracker.py \
        --source /opt/data/workspace/finance-tracker/data/finance.db \
        --target finance.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.app import connect, ensure_schema  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    target_path = Path(args.target)
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    import web.app as webapp
    webapp.DB_PATH = target_path
    tgt = connect()
    ensure_schema(tgt)

    # 1) Kategorien (per Name, Ziel-IDs mappen)
    cat_map: dict[int, int] = {}
    for r in src.execute("SELECT id, name, type FROM categories"):
        tgt.execute("INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)", (r["name"], r["type"]))
        new_id = tgt.execute("SELECT id FROM categories WHERE name = ?", (r["name"],)).fetchone()[0]
        cat_map[r["id"]] = new_id

    # 2) Konten (IBAN als Schluessel)
    acct_map: dict[int, str] = {}
    for r in src.execute("SELECT id, iban, bank_name FROM accounts"):
        tgt.execute(
            "INSERT INTO accounts (iban, bank_name) VALUES (?, ?) "
            "ON CONFLICT(iban) DO UPDATE SET bank_name = excluded.bank_name, updated_at = datetime('now')",
            (r["iban"], r["bank_name"]),
        )
        acct_map[r["id"]] = r["iban"]

    # 3) Umsaetze (Ziel-Schema: booking_date, amount als Text, dedup_hash)
    inserted = skipped = 0
    for r in src.execute("SELECT * FROM transactions ORDER BY id"):
        cur = tgt.execute(
            """INSERT INTO transactions
               (account_iban, booking_date, entry_date, amount, currency,
                applicant_name, purpose, raw, dedup_hash, category_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(dedup_hash) DO NOTHING""",
            (
                acct_map[r["account_id"]],
                r["buchungsdatum"],
                r["valutadatum"],
                str(r["betrag"]),
                r["waehrung"] or "EUR",
                r["partner_name"] or "",
                r["verwendungszweck"] or "",
                r["raw_data"] or "{}",
                f"ft:{r['external_id']}",  # eigener Praefix, kollidiert nicht mit Backend-Hashes
                cat_map.get(r["category_id"]),
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    tgt.commit()

    print(json.dumps({
        "categories": len(cat_map),
        "accounts": len(acct_map),
        "transactions_inserted": inserted,
        "transactions_skipped_existing": skipped,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
