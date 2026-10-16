-- ============================================================
-- finance-tracker — SQLite-Schema für die Umsatzdatenbank
-- ============================================================
-- Dieses Schema wird von transaktionen_speichern.py beim Start
-- automatisch angewendet (idempotent, kann also beliebig oft
-- ausgeführt werden). Es kann aber auch manuell über die
-- SQLite-CLI eingespielt werden:
--
--     sqlite3 finance.db < schema.sql
--
-- Datenmodell
-- -----------
-- accounts      : je ein Datensatz pro SEPA-Konto (IBAN als Schlüssel)
-- transactions  : die einzelnen Umsätze (results[].data aus fints),
--                 strukturiert in Spalten plus das komplette
--                 Original-Dict als JSON in `raw`.
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Konten
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    iban           TEXT PRIMARY KEY,   -- IBAN (eindeutiger Schlüssel)
    bank_name      TEXT,               -- Anzeigename der Bank (fuer Dashboard)
    bic            TEXT,               -- BIC
    accountnumber  TEXT,               -- Kontonummer
    subaccount     TEXT,               -- Unterkonto
    blz            TEXT,               -- Bankleitzahl
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- Kategorien (fuer Dashboard-Auswertung)
-- ------------------------------------------------------------
-- Vorschlagskategorien werden beim Start automatisch angelegt
-- (web/kategorien.py, ensure_default_categories) und Umsaetze
-- regelbasiert zugeordnet.
CREATE TABLE IF NOT EXISTS categories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    type  TEXT NOT NULL CHECK (type IN ('income', 'expense'))
);

-- ------------------------------------------------------------
-- Umsätze / Transaktionen
-- ------------------------------------------------------------
-- Die Spalten bilden die in der Regel vorhandenen Felder des
-- fints/mt940-Daten-Dicts ab. Bank-spezifische oder zusätzliche
-- Felder gehen nicht verloren: Das komplette data-Dict wird in
-- der Spalte `raw` als JSON abgelegt.
CREATE TABLE IF NOT EXISTS transactions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    account_iban         TEXT NOT NULL REFERENCES accounts(iban) ON DELETE CASCADE,

    -- strukturierte Kernfelder (aus results[].data)
    booking_date         TEXT,   -- 'date'          (ISO: YYYY-MM-DD)
    entry_date           TEXT,   -- 'entry_date'    (ISO: YYYY-MM-DD)
    amount               TEXT,   -- 'amount'        (exakt als Text, Vorzeichen: D negativ)
    currency             TEXT,   -- 'currency'      (z.B. EUR)
    status               TEXT,   -- 'status'        (C / D / RC / RD)
    customer_reference   TEXT,   -- 'customer_reference'
    bank_reference       TEXT,   -- 'bank_reference'
    extra_details        TEXT,   -- 'extra_details'

    -- ab Pflichtfelder: immer befüllt, fehlt der Wert in den Bankdaten,
    -- wird ein leerer String ('') gespeichert (siehe transaktionen_speichern.py).
    applicant_name       TEXT NOT NULL DEFAULT '',  -- 'applicant_name' (Empfänger/Auftraggeber)
    purpose              TEXT NOT NULL DEFAULT '',  -- 'purpose' (Verwendungszweck-Text)

    -- komplette Rohdaten des Umsatzes als JSON
    raw                  TEXT NOT NULL,

    -- Stabiler Hash zur Duplikaterkennung (Konto + Datum + Betrag
    -- + Referenzen). Verhindert, dass derselbe Umsatz bei einem
    -- erneuten Import doppelt angelegt wird.
    dedup_hash           TEXT UNIQUE,

    -- Kategorie (vom Dashboard gesetzt, NULL = nicht zugeordnet)
    category_id          INTEGER REFERENCES categories(id) ON DELETE SET NULL,

    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Beschleunigt Abfragen "alle Umsätze eines Kontos, chronologisch".
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
    ON transactions (account_iban, booking_date);