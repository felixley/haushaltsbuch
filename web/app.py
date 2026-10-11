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
from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from web import sync

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
    conn.commit()


def _money(value: str | None) -> float:
    return float(Decimal(value)) if value not in (None, "") else 0.0


def _round(v: float) -> float:
    return round(v, 2)


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
    def kpis():
        month_start = dt.date.today().replace(day=1).isoformat()
        with connect() as conn:
            all_amounts = [r[0] for r in conn.execute("SELECT amount FROM transactions")]
            month = [r[0] for r in conn.execute(
                "SELECT amount FROM transactions WHERE booking_date >= ?", (month_start,))]
        total = sum(_money(a) for a in all_amounts)
        income = sum(_money(a) for a in month if _money(a) > 0)
        expenses = sum(_money(a) for a in month if _money(a) < 0)
        return {
            # Hinweis: Bankkontostand wird noch nicht gespeichert -> Summe aller Umsätze
            "total_balance": _round(total),
            "income_month": _round(income),
            "expenses_month": _round(expenses),
            "net_cashflow": _round(income + expenses),
        }

    @app.get("/api/timeline")
    def timeline(granularity: str = Query("monthly", pattern="^(monthly|yearly)$")):
        length = 7 if granularity == "monthly" else 4  # 'YYYY-MM' bzw. 'YYYY'
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expenses": 0.0})
        with connect() as conn:
            rows = conn.execute("SELECT booking_date, amount FROM transactions").fetchall()
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
    def breakdown():
        totals: dict[str, float] = defaultdict(float)
        with connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(c.name, 'Nicht zugeordnet') AS name, t.amount
                   FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"""
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
        where, params = [], []
        if search:
            where.append("(t.applicant_name LIKE ? OR t.purpose LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if category_id is not None:
            where.append("t.category_id = ?")
            params.append(category_id)
        if account_id:
            where.append("t.account_iban = ?")
            params.append(account_id)
        if date_from:
            where.append("t.booking_date >= ?")
            params.append(date_from)
        if date_to:
            where.append("t.booking_date <= ?")
            params.append(date_to)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
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
                "partner_name": r["applicant_name"],
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
        raw_cat = body.get("category_id")
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
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (cat_id, tx_id))
            conn.commit()
        return {"ok": True, "category": name}

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
