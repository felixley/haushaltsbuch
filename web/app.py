"""Haushaltsbuch-Dashboard: FastAPI-Backend über der SQLite-DB aus schema.sql.

Start (vom Projektordner aus):
    uvicorn web.app:app --host 0.0.0.0 --port 4713

API-Form entspricht dem Frontend (web/static/main.js), die Daten kommen
aus den Tabellen accounts / transactions / categories.
Zugangsdaten für Banken sind NICHT Teil dieser App (siehe transaktionen_speichern.py).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from web import sync
from web.kategorien import (
    apply_categorization_to_db,
    apply_rule_to_all_matches,
    count_other_matches,
    ensure_default_categories,
    ensure_learned_rules_table,
    internal_transfer_category_ids,
    learn_from_manual_assignment,
    match_key,
    matching_transactions_snapshot,
)
from web.partner import clean_partner_name

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

_db_env = os.getenv("DB_PATH", "finance.db")
DB_PATH = Path(_db_env) if Path(_db_env).is_absolute() else ROOT / _db_env
SCHEMA_FILE = ROOT / "schema.sql"
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ------------------------------------------------------------------
# Datenbank
# ------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Wendet schema.sql an und rüstet Spalten für ältere DBs nach (idempotent)."""
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    for table, column, definition in (
        ("accounts", "bank_name", "TEXT"),
        ("transactions", "category_id", "INTEGER REFERENCES categories(id) ON DELETE SET NULL"),
    ):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Vorschlagskategorien anlegen (idempotent) – damit das Dashboard direkt
    # Auswahlmöglichkeiten hat, auch ohne manuelles Kategorisieren.
    ensure_default_categories(conn)
    conn.commit()


def _money(value: str | None) -> float:
    return float(Decimal(value)) if value not in (None, "") else 0.0


def _round(v: float) -> float:
    return round(v, 2)


def _filter_clause(
    *,
    search: str = "",
    category_id: int | None = None,
    account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    alias: str = "t",
) -> tuple[list[str], list[object]]:
    """Baut die WHERE-Bedingungen der Filterleiste (Tabelle und Auswertungen).

    Kennzahlen, Timeline und Kategorien-Grafik nutzen exakt dieselben Filter
    wie ``/api/transactions`` – sonst zeigen die Kacheln oben andere Zahlen als
    die darunter gefilterte Tabelle.
    """
    where: list[str] = []
    params: list[object] = []
    if search:
        where.append(f"({alias}.applicant_name LIKE ? OR {alias}.purpose LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if category_id is not None:
        where.append(f"{alias}.category_id = ?")
        params.append(category_id)
    if account_id:
        where.append(f"{alias}.account_iban = ?")
        params.append(account_id)
    if date_from:
        where.append(f"{alias}.booking_date >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{alias}.booking_date <= ?")
        params.append(date_to)
    return where, params


def _where_clause(where: list[str]) -> str:
    return (" WHERE " + " AND ".join(where)) if where else ""


def _exclude_internal_transfers(
    where: list[str], params: list[object], category_ids: list[int]
) -> None:
    """Interne Transfers/Umbuchungen aus Auswertungen heraushalten.

    Betrifft nur Kennzahlen und Grafiken – die Umsatztabelle zeigt sie
    weiterhin, damit man sie prüfen und umkategorisieren kann.
    """
    if category_ids:
        placeholders = ", ".join("?" for _ in category_ids)
        # Klammern sind Pflicht: ohne sie würde die UND-Verknüpfung mit anderen
        # Bedingungen falsch gruppiert. ``IS NULL`` muss ausdrücklich davor:
        # ``NULL NOT IN (...)`` ergibt in SQL NULL (nicht TRUE) und würde
        # Umsätze ohne Kategorie komplett aus den Auswertungen werfen.
        where.append(
            f"(t.category_id IS NULL OR t.category_id NOT IN ({placeholders}))"
        )
        params.extend(category_ids)


# Rückgängig-Puffer für Sammel-Zuordnungen: Token -> vorheriger Zustand.
_undo_store: dict[str, dict] = {}
_UNDO_LIMIT = 20


def _remember_undo(
    changed: list[tuple[int, int | None]],
    rule: tuple[str, str, int | None],
) -> str:
    """Merkt sich den Zustand vor einer Sammel-Zuordnung für ``/undo``."""
    token = uuid.uuid4().hex
    _undo_store[token] = {"changes": changed, "rule": rule}
    while len(_undo_store) > _UNDO_LIMIT:
        _undo_store.pop(next(iter(_undo_store)))
    return token


# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        ensure_schema(conn)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Haushaltsbuch", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # --- Kennzahlen -------------------------------------------------
    @app.get("/api/kpis")
    def kpis(
        search: str = "",
        category_id: int | None = None,
        account_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        # Zeitraum-Modus: sobald ein Datumsfilter gesetzt ist, beziehen sich
        # Einnahmen/Ausgaben auf diesen Zeitraum statt auf den aktuellen Monat.
        range_mode = bool(date_from or date_to)
        with connect() as conn:
            # Interne Transfers/Umbuchungen sind keine echten Einnahmen/Ausgaben.
            transfers = internal_transfer_category_ids(conn)
            where, params = _filter_clause(
                search=search, category_id=category_id, account_id=account_id,
                date_from=date_from, date_to=date_to,
            )
            _exclude_internal_transfers(where, params, transfers)
            clause = _where_clause(where)
            period_where, period_params = list(where), list(params)
            if not range_mode:
                period_where.append("t.booking_date >= ?")
                period_params.append(dt.date.today().replace(day=1).isoformat())
            period_clause = _where_clause(period_where)
            all_amounts = [r[0] for r in conn.execute(
                f"SELECT t.amount FROM transactions t{clause}", params)]
            period = [r[0] for r in conn.execute(
                f"SELECT t.amount FROM transactions t{period_clause}", period_params)]
        total = sum(_money(a) for a in all_amounts)
        income = sum(_money(a) for a in period if _money(a) > 0)
        expenses = sum(_money(a) for a in period if _money(a) < 0)
        return {
            # Hinweis: Bankkontostand wird noch nicht gespeichert -> Summe aller Umsätze
            "total_balance": _round(total),
            "income_month": _round(income),
            "expenses_month": _round(expenses),
            "net_cashflow": _round(income + expenses),
            # "month" = aktueller Monat, "range" = gesetzter Datumsfilter
            "period_mode": "range" if range_mode else "month",
        }

    @app.get("/api/timeline")
    def timeline(
        granularity: str = Query("monthly", pattern="^(monthly|yearly)$"),
        search: str = "",
        category_id: int | None = None,
        account_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        length = 7 if granularity == "monthly" else 4  # 'YYYY-MM' bzw. 'YYYY'
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expenses": 0.0})
        with connect() as conn:
            where, params = _filter_clause(
                search=search, category_id=category_id, account_id=account_id,
                date_from=date_from, date_to=date_to,
            )
            _exclude_internal_transfers(
                where, params, internal_transfer_category_ids(conn))
            clause = _where_clause(where)
            rows = conn.execute(
                f"SELECT t.booking_date, t.amount FROM transactions t{clause}", params
            ).fetchall()
        for booking_date, amount in rows:
            if not booking_date:
                continue
            period = booking_date[:length]
            b = _money(amount)
            if b > 0:
                agg[period]["income"] += b
            else:
                agg[period]["expenses"] += b
        return [
            {"period": p, "income": _round(v["income"]), "expenses": _round(v["expenses"])}
            for p, v in sorted(agg.items())
        ]

    @app.get("/api/categories/breakdown")
    def breakdown(
        search: str = "",
        category_id: int | None = None,
        account_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        totals: dict[str, float] = defaultdict(float)
        with connect() as conn:
            where, params = _filter_clause(
                search=search, category_id=category_id, account_id=account_id,
                date_from=date_from, date_to=date_to,
            )
            _exclude_internal_transfers(
                where, params, internal_transfer_category_ids(conn))
            clause = _where_clause(where)
            rows = conn.execute(
                f"""SELECT COALESCE(c.name, 'Nicht zugeordnet') AS name, t.amount
                    FROM transactions t LEFT JOIN categories c ON c.id = t.category_id{clause}""",
                params,
            ).fetchall()
        for name, amount in rows:
            b = _money(amount)
            if b < 0:
                totals[name] += -b
        return sorted(
            ({"category": k, "total": _round(v)} for k, v in totals.items()),
            key=lambda d: -d["total"],
        )

    # --- Transaktionen ----------------------------------------------
    @app.get("/api/transactions")
    def transactions(
        search: str = "",
        category_id: int | None = None,
        account_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        per_page: int = Query(25, le=100),
    ):
        where, params = _filter_clause(
            search=search, category_id=category_id, account_id=account_id,
            date_from=date_from, date_to=date_to,
        )
        clause = _where_clause(where)
        page = max(page, 1)
        with connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM transactions t {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT t.id, t.booking_date, t.entry_date, t.amount, t.currency,
                           t.applicant_name, t.purpose, t.category_id, c.name AS category,
                           t.account_iban, a.bank_name, a.subaccount
                    FROM transactions t
                    LEFT JOIN categories c ON c.id = t.category_id
                    LEFT JOIN accounts a ON a.iban = t.account_iban
                    {clause}
                    ORDER BY t.booking_date DESC, t.id DESC
                    LIMIT ? OFFSET ?""",
                [*params, per_page, (page - 1) * per_page],
            ).fetchall()
        items = [
            {
                "id": r["id"],
                "buchungsdatum": r["booking_date"],
                "valutadatum": r["entry_date"],
                "betrag": _money(r["amount"]),
                "waehrung": r["currency"] or "EUR",
                "partner_name": clean_partner_name(r["applicant_name"]),
                "verwendungszweck": r["purpose"],
                "category_id": r["category_id"],
                "category": r["category"],
                "account_id": r["account_iban"],
                "account_bank": r["bank_name"] or r["subaccount"] or r["account_iban"][-4:],
            }
            for r in rows
        ]
        return {"total": total, "page": page, "per_page": per_page, "items": items}

    @app.patch("/api/transactions/{tx_id}/category")
    def set_category(tx_id: int, body: dict):
        """Setzt die Kategorie eines Umsatzes und überträgt sie auf gleichartige.

        Standard: **alle** Umsätze mit gleichem Partner und Verwendungszweck
        erhalten diese Kategorie – auch wenn sie schon eine andere hatten
        (der Betrag ist dabei egal, gleichartige Umsätze unterscheiden sich oft
        nur darin). ``overwritten`` nennt, wie viele vorhandene Zuordnungen
        dabei ersetzt wurden; ``undo_token`` macht den Vorgang rückgängig.

        Mit ``{"only_empty": true}`` werden nur noch leere Zuordnungen gefüllt
        (bisheriges Verhalten von ``learn_from_manual_assignment``).
        ``category_id: null`` hebt die Zuordnung auf und entfernt die Regel.
        """
        raw_cat = body.get("category_id")
        only_empty = bool(body.get("only_empty"))
        with connect() as conn:
            if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (tx_id,)).fetchone() is None:
                raise HTTPException(404, "Transaktion nicht gefunden")
            name = None
            if raw_cat in (None, "", 0):
                cat_id = None
            else:
                cat = conn.execute("SELECT id, name FROM categories WHERE id = ?", (int(raw_cat),)).fetchone()
                if cat is None:
                    raise HTTPException(404, "Kategorie nicht gefunden")
                cat_id, name = cat["id"], cat["name"]

            undo_token = None
            overwritten = 0
            if cat_id is None:
                updated = learn_from_manual_assignment(conn, tx_id, None)
            elif only_empty:
                updated = learn_from_manual_assignment(conn, tx_id, cat_id)
            else:
                # Zustand VOR der Änderung festhalten (für "Rückgängig").
                vorher = matching_transactions_snapshot(conn, tx_id)
                zeile = conn.execute(
                    "SELECT applicant_name, purpose FROM transactions WHERE id = ?", (tx_id,)
                ).fetchone()
                partner_key, purpose_key = match_key(zeile[0], zeile[1])
                ensure_learned_rules_table(conn)
                regel = conn.execute(
                    "SELECT category_id FROM learned_rules WHERE partner_key = ? AND purpose_key = ?",
                    (partner_key, purpose_key),
                ).fetchone()
                alt_regel = regel[0] if regel else None

                updated = apply_rule_to_all_matches(conn, tx_id, cat_id)

                changed = [(tid, alt) for tid, alt in vorher if alt != cat_id]
                # Gezählt werden nur *andere* Umsätze – der gerade bearbeitete
                # Umsatz wurde ja bewusst umgestellt.
                overwritten = sum(
                    1 for tid, alt in changed if alt is not None and tid != tx_id
                )
                if changed or alt_regel is not None:
                    undo_token = _remember_undo(
                        changed, (partner_key, purpose_key, alt_regel)
                    )
            conn.commit()
        return {
            "ok": True, "category": name, "updated": updated,
            "overwritten": overwritten, "undo_token": undo_token,
        }

    @app.post("/api/transactions/category/undo")
    def undo_category(body: dict):
        """Macht die letzte Sammel-Zuordnung rückgängig (per ``undo_token``)."""
        changes = _undo_store.pop((body or {}).get("token"), None)
        if changes is None:
            raise HTTPException(404, "Nichts rückgängig zu machen")
        with connect() as conn:
            ensure_learned_rules_table(conn)
            for tx_id, old_category in changes["changes"]:
                conn.execute(
                    "UPDATE transactions SET category_id = ? WHERE id = ?",
                    (old_category, tx_id),
                )
            partner_key, purpose_key, old_rule = changes["rule"]
            if old_rule is None:
                conn.execute(
                    "DELETE FROM learned_rules WHERE partner_key = ? AND purpose_key = ?",
                    (partner_key, purpose_key),
                )
            else:
                conn.execute(
                    """INSERT INTO learned_rules (partner_key, purpose_key, category_id)
                           VALUES (?, ?, ?)
                       ON CONFLICT(partner_key, purpose_key) DO UPDATE SET
                           category_id = excluded.category_id,
                           updated_at = datetime('now')""",
                    (partner_key, purpose_key, old_rule),
                )
            conn.commit()
        return {"ok": True, "restored": len(changes["changes"])}

    # --- Automatische Kategorisierung -------------------------------
    @app.post("/api/categorize")
    def categorize_transactions(body: dict | None = None):
        """Kategorisiert Umsätze automatisch anhand von Empfänger und Verwendungszweck.

        Standard: nur noch nicht zugeordnete Umsätze (manuelle Zuordnungen
        bleiben erhalten). Mit ``{"overwrite": true}`` werden alle neu bewertet.
        """
        overwrite = bool((body or {}).get("overwrite"))
        with connect() as conn:
            ensure_schema(conn)
        scanned, changed = apply_categorization_to_db(
            DB_PATH, only_uncategorized=not overwrite
        )
        return {"ok": True, "scanned": scanned, "changed": changed}

    # --- Kategorien -------------------------------------------------
    @app.get("/api/categories")
    def list_categories():
        with connect() as conn:
            rows = conn.execute("SELECT id, name, type FROM categories ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/categories", status_code=201)
    def create_category(body: dict):
        name = (body.get("name") or "").strip()
        ctype = body.get("type", "expense")
        if not name:
            raise HTTPException(400, "Name fehlt")
        if ctype not in ("income", "expense"):
            raise HTTPException(400, "Typ muss income oder expense sein")
        try:
            with connect() as conn:
                cur = conn.execute("INSERT INTO categories (name, type) VALUES (?, ?)", (name, ctype))
                conn.commit()
                return {"id": cur.lastrowid, "name": name, "type": ctype}
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Kategorie '{name}' existiert bereits")

    @app.delete("/api/categories/{cat_id}")
    def delete_category(cat_id: int):
        with connect() as conn:
            cur = conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, "Kategorie nicht gefunden")
        return {"ok": True}

    # --- Konten -----------------------------------------------------
    @app.get("/api/accounts")
    def accounts():
        with connect() as conn:
            rows = conn.execute(
                "SELECT iban, bank_name, subaccount FROM accounts ORDER BY bank_name, iban"
            ).fetchall()
        return [
            {
                "id": r["iban"],
                "iban": r["iban"],
                "bank_name": r["bank_name"] or r["subaccount"] or "Konto",
            }
            for r in rows
        ]

    # --- Bankabruf (FinTS) ------------------------------------------
    # PIN kommt nur aus dem Request, lebt im Job-Speicher und wird nicht gespeichert.
    @app.get("/api/sync/banks")
    def sync_banks():
        return sync.banks_status()

    @app.post("/api/sync", status_code=202)
    def start_sync(body: dict):
        try:
            job = sync.start_job(
                bank=str(body.get("bank", "")),
                pin=str(body.get("pin", "")),
                days=int(body.get("days", 100)),
                db_path=DB_PATH,
            )
        except sync.SyncError as e:
            code = 409 if "läuft bereits" in str(e) else 400
            raise HTTPException(code, str(e))
        finally:
            body.pop("pin", None)  # Klartext-PIN nicht im Speicher halten
        return job.public()

    @app.get("/api/sync/{job_id}")
    def sync_status(job_id: str):
        job = sync.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Abruf nicht gefunden")
        return job.public()

    @app.get("/api/sync/{job_id}/challenge")
    def sync_challenge(job_id: str):
        """Liefert die anstehende photoTAN-Grafik direkt als Bild – kein Dateischreiben."""
        job = sync.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Abruf nicht gefunden")
        with job.lock:
            challenge = job.challenge
            mime = challenge[0] if challenge else None
            data = challenge[1] if challenge else None
        if data is None:
            raise HTTPException(404, "Aktuell liegt keine Challenge an")
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": "inline",
            },
        )

    @app.post("/api/sync/{job_id}/input")
    def sync_input(job_id: str, body: dict):
        job = sync.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Abruf nicht gefunden")
        value = {k: v for k, v in body.items() if k in ("tan", "index")}
        try:
            sync.submit_input(job, value)
        except sync.SyncError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/sync/{job_id}/cancel")
    def sync_cancel(job_id: str):
        job = sync.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Abruf nicht gefunden")
        try:
            sync.cancel_job(job)
        except sync.SyncError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    return app


app = create_app()
