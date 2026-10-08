# finance-tracker

FinTS-/HBCI-Umsatzabruf (comdirect, DKB) mit Speicherung in einer
SQLite-Datenbank und einem pandas-Viewer zum Ansehen der Daten.

## Dateien

| Datei | Zweck |
|---|---|
| `testverbindung.py` | Ursprüngliches Testskript: Bankverbindung + Umsatzabruf (Ausgabe im Terminal). |
| `schema.sql` | SQLite-Schema (`accounts`, `transactions`). |
| `transaktionen_speichern.py` | Ruft Umsätze ab und schreibt `results[].data` in die SQLite-DB. |
| `db_anzeigen.py` | Zeigt den Inhalt der Datenbank mit pandas an. |
| `manuelle_ueberprüfung.py` | Minimales Setup: DB mit pandas einlesen (Spielwiese). |

## Datenmodell (SQLite)

`schema.sql` legt zwei Tabellen an:

- **`accounts`** — je ein Datensatz pro SEPA-Konto (`iban` als Schlüssel,
  dazu `bic`, `accountnumber`, `subaccount`, `blz`).
- **`transactions`** — die einzelnen Umsätze. Die strukturierten
  Kernspalten werden aus `results[].data` befüllt:

  | Spalte | Herkunft (data-Schlüssel) |
  |---|---|
  | `booking_date` | `date` (ISO `YYYY-MM-DD`) |
  | `entry_date` | `entry_date` (ISO `YYYY-MM-DD`) |
  | `amount` | `amount` (als Text, Vorzeichen: D negativ) |
  | `currency` | `currency` |
  | `status` | `status` (C / D / RC / RD) |
  | `customer_reference` | `customer_reference` |
  | `bank_reference` | `bank_reference` |
  | `extra_details` | `extra_details` |
  | `applicant_name` | `applicant_name` (Pflicht, `NOT NULL`) |
  | `purpose` | `purpose` (Pflicht, `NOT NULL`) |
  | `raw` | komplettes `data`-Dict als JSON |
  | `dedup_hash` | Hash zur Duplikaterkennung (unique) |

  Bank-spezifische oder zusätzliche Felder gehen nicht verloren: Das
  komplette `data`-Dict liegt zusätzlich in der Spalte `raw` als JSON.

Beim Start gleicht `transaktionen_speichern.py` das Schema einer
bestehenden Datenbank automatisch an (idempotente Migration): fehlende
Pflichtspalten werden ergänzt und veraltete, nie befüllte Spalten
(`transaction_type`, `transaction_details`) entfernt.

## Setup

```bash
# Abhängigkeiten installieren
uv sync

# Zugangsdaten anlegen
cp .env.example .env
# -> .env öffnen und Werte eintragen (Bank, BLZ, Username, HBCI, TAN-Verfahren)
#    Die PIN wird NICHT in .env gespeichert, sondern beim Start abgefragt.
```

Wichtige `.env`-Variablen:

- `BANK` — `comdirect` oder `dkb`
- `DB_PATH` — Pfad zur SQLite-Datei (Default: `finance.db`)
- `<BANK>_BLZ`, `<BANK>_USERNAME`, `<BANK>_HBCI`, `<BANK>_TAN_MECHANISM`

## Umsätze abrufen und speichern

```bash
python transaktionen_speichern.py             # Standard: letzte 100 Tage
python transaktionen_speichern.py --days 365  # letzte 365 Tage
python transaktionen_speichern.py --dry-run   # nur anzeigen, nichts schreiben
python transaktionen_speichern.py --db meine.db
python transaktionen_speichern.py --bank dkb  # Bank direkt wählen (statt BANK aus .env)
```

Die Auswahl der Bank erfolgt über `--bank <name>` (möglich: `comdirect`,
`dkb`). Wird `--bank` weggelassen, gilt `BANK` aus der `.env`
(Default: `comdirect`). Angaben auf der Kommandozeile haben also Vorrang.

Für jedes Konto wird:
1. das Konto in `accounts` angelegt/aktualisiert,
2. jeder Umsatz aus `results[].data` strukturiert in `transactions` geschrieben,
3. das komplette Roh-Dict zusätzlich als JSON in der Spalte `raw` gesichert.

**Duplikate:** Jeder Umsatz bekommt einen `dedup_hash` (Konto + Datum +
Betrag + Währung + Referenzen). Ein erneuter Import legt bereits
vorhandene Umsätze daher nicht doppelt an (`ON CONFLICT DO NOTHING`).

**Pflichtfelder:** `applicant_name` (Empfänger/Auftraggeber) und `purpose`
(Verwendungszweck-Text) sind Pflichtspalten (`NOT NULL`). Liefert die Bank
eines der Felder nicht, wird ein leerer String (`''`) gespeichert.
Beim Start gleicht `transaktionen_speichern.py` das Schema einer bereits
vorhandenen Datenbank automatisch an (idempotente Migration): fehlende
Pflichtspalten werden ergänzt, veraltete Spalten (`transaction_type`,
`transaction_details`), die nie mit Daten gefüllt wurden, werden entfernt.

## Datenbank ansehen

```bash
python db_anzeigen.py                      # Konten + letzte 50 Umsätze
python db_anzeigen.py --accounts           # nur Kontenübersicht
python db_anzeigen.py --limit 200          # mehr Umsätze zeigen
python db_anzeigen.py --account DE12...    # nur ein Konto
python db_anzeigen.py --sql "SELECT ..."   # freie (read-only) SQL-Abfrage
python db_anzeigen.py --csv export.csv     # Ergebnis als CSV speichern
python db_anzeigen.py --raw                # komplette Roh-JSON-Daten zeigen
```

Die Ausgabe erfolgt als pandas-DataFrame-Tabelle im Terminal. Die
Umsatzansicht enthält zusätzlich die `account_iban` und den `subaccount`
(z. B. „Girokonto“), damit sich jeder Umsatz seinem Konto zuordnen lässt.

## Dashboard (Web-Frontend)

Das Dashboard (übernommen aus `finance-tracker`) liest direkt aus der
SQLite-DB (`DB_PATH`, Default `finance.db`).

```bash
uv run uvicorn web.app:app --host 0.0.0.0 --port 4713
# -> http://127.0.0.1:4713
```

Enthalten: Kennzahlen, Einnahmen/Ausgaben (monatlich/jährlich), Kategorie-Auswertung,
Umsatztabelle mit Filtern (Suche, Kategorie, Konto, Zeitraum, Paging),
Kategorien verwalten, Kategorie je Umsatz setzen.

Noch nicht im Dashboard (aus finance-tracker bewusst zurückgestellt):
Personen, Kategorie-Regeln (automatisches Lernen), Konto-Salden, FinTS-Verbindungs-UI, Sync-Button.

### Testdaten aus finance-tracker übernehmen

```bash
uv run python migration/import_finance_tracker.py \
    --source /opt/data/workspace/finance-tracker/data/finance.db --target finance.db
```

Achtung: Die Quelle enthält Mock-Daten (`source: mock`), keine echten Bankumsätze.
Import ist idempotent (Duplikate werden übersprungen).
