"""Automatische Kategorisierung von Umsätzen (regelbasiert).

Die Bank liefert bei jedem Umsatz einen Empfänger/Auftraggeber
(``applicant_name``) und einen Verwendungszweck (``purpose``). Beide Felder
zusammen enthalten in der Regel genug Hinweise, um die Ausgabe einer gängigen
deutschen Haushaltskategorie zuzuordnen, z. B.::

    ("NETTO MARKEN-DISCOUNT", "LASTSCHRIFT / BELASTUNGNETTO …")  ->  Lebensmittel
    ("SHELL 1945",            "KARTENZAHLUNG …")                 ->  Tanken
    ("BARMENIAGOTHAER AG",    "LOHN/GEHALT 00545764/202607")     ->  Gehalt & Lohn

Vorgehen
--------
1. Die Vorschlagsliste ``DEFAULT_CATEGORIES`` legt die üblichen Kategorien an
   (Einnahmen und Ausgaben, deutsch, für Deutschland relevant).
2. ``RULES`` prüft den kombinierten Text (Empfänger + Verwendungszweck) gegen
   Schlüsselwörter/Muster. Die erste passende Regel gewinnt.
3. Kann kein Muster sicher greifen, bleibt der Umsatz **ohne** Kategorie
   (``None``) – lieber ``Nicht zugeordnet`` als eine falsche Zuordnung.
   Manuell gesetzte Kategorien werden nie überschrieben.

Der Vergleich ist bewusst heuristisch und wird von Hand gepflegt: Ein neuer
Händler taucht einfach in zwei oder drei Regeln als Schlüsselwort auf.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from web.partner import clean_partner_name

ROOT = Path(__file__).resolve().parent.parent

__all__ = [
    "DEFAULT_CATEGORIES",
    "Rules",
    "RULES",
    "categorize",
    "ensure_default_categories",
    "apply_categorization_to_db",
    "ensure_learned_rules_table",
    "load_learned_rules",
    "learn_from_manual_assignment",
    "match_key",
    "suggest_category",
    "internal_transfer_category_ids",
]


def _category_key(name: str) -> str:
    """Normalisierter Kategoriename: nur Kleinbuchstaben/Ziffern."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


# Namen, unter denen eine Kategorie "interner Transfer / Umbuchung" bedeutet.
# Deckt die Vorschlagskategorie "Transfers (intern)" ebenso ab wie manuell
# angelegte Varianten ("Transferintern", "Interner Transfer", "Umbuchung").
_INTERNAL_TRANSFER_KEYS = {
    "transferintern",
    "transfersintern",
    "interntransfer",
    "internertransfer",
}


def internal_transfer_category_ids(connection: sqlite3.Connection) -> list[int]:
    """IDs der Kategorien, die interne Transfers/Umbuchungen abbilden.

    Solche Umsätze sind keine echten Einnahmen oder Ausgaben (Geld wandert nur
    zwischen eigenen Konten) und werden deshalb aus Kennzahlen und Grafiken
    herausgehalten – in der Umsatztabelle bleiben sie sichtbar.
    """
    ids: list[int] = []
    for category_id, name in connection.execute("SELECT id, name FROM categories"):
        key = _category_key(name)
        if (
            key in _INTERNAL_TRANSFER_KEYS
            or "umbuchung" in key          # "Umbuchung", "Interne Umbuchung"
            or ("transfer" in key and "intern" in key)
        ):
            ids.append(category_id)
    return ids


# ------------------------------------------------------------------
# Vorgeschlagene Kategorien (deutsch, für Deutschland üblich)
# ------------------------------------------------------------------
# (name, type) – die Reihenfolge ist nur für die Anzeige relevant.
DEFAULT_CATEGORIES: tuple[tuple[str, str], ...] = (
    # --- Ausgaben -------------------------------------------------
    ("Lebensmittel", "expense"),
    ("Restaurants & Cafés", "expense"),
    ("Tanken", "expense"),
    ("Mobilität & ÖPNV", "expense"),
    ("Miete & Wohnen", "expense"),
    ("Nebenkosten & Strom", "expense"),
    ("Internet & Telefon", "expense"),
    ("Abos & Streaming", "expense"),
    ("Versicherungen", "expense"),
    ("Gesundheit & Apotheke", "expense"),
    ("Kleidung & Schuhe", "expense"),
    ("Haushalt & Drogerie", "expense"),
    ("Elektronik & Technik", "expense"),
    ("Freizeit & Hobby", "expense"),
    ("Reisen & Urlaub", "expense"),
    ("Bildung & Kinder", "expense"),
    ("Spenden & Geschenke", "expense"),
    ("Bankgebühren", "expense"),
    ("Steuern & Gebühren", "expense"),
    ("Geldanlagen & Sparen", "expense"),
    ("Barbeträge", "expense"),
    # --- Einnahmen ------------------------------------------------
    ("Gehalt & Lohn", "income"),
    ("Kindergeld", "income"),
    ("Elterngeld", "income"),
    ("Steuererstattung", "income"),
    ("Erstattungen & Rückzahlungen", "income"),
    ("Zinsen & Kapitalerträge", "income"),
    ("Transfers (intern)", "income"),
    ("Sonstige Einnahmen", "income"),
)

DEFAULT_CATEGORY_NAMES = {name for name, _ in DEFAULT_CATEGORIES}


# ------------------------------------------------------------------
# Textnormalisierung
# ------------------------------------------------------------------
def _normalize(text: str | None) -> str:
    """Kleinschreibung, Umlaute auseinandernehmen, Satzzeichen zu Leerzeichen.

    Umlaute werden absichtlich zu ``ae``/``oe``/``ue`` (nicht zu ``a``/``o``/``u``),
    damit echte Wörter nicht kaputtgehen – ``Gothaer`` bleibt ``gothaer`` und wird
    nicht zu ``gothar``. Die Regeln enthalten die Umlaut-Schreibweise zusätzlich,
    damit beide Varianten (``müller``/``mueller``) treffen.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text)).lower()
    for src, dst in (
        ("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"),
    ):
        text = text.replace(src, dst)
    # Alles, was kein Buchstabe/Ziffer ist, wird zum Trennzeichen.
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {text.strip()} "


# Empfänger/Marker, die eindeutig auf eine Umbuchung zwischen eigenen Konten bzw.
# die eigene Bank hindeuten (typisch comdirect/DKB). Werden nur auf den
# Empfänger-/Auftraggeber-Namen angewendet, NICHT auf den Verwendungszweck –
# sonst würden normale Zahlungen fälschlich als Umbuchung gelten.
_OWN_ACCOUNT_HINTS = re.compile(
    r"(\bgemeinschaft\b|\bhaushaltsgeld\b|\bfixkosten\b|"
    r"\bfelix\s+(ley|dkb|comdirect)\b|"
    r"dkb\s+ag|comdirect|ing[- ]?diba|\binge\b)"
)


def _looks_like_own_transfer(applicant_name: str | None) -> bool:
    """True, wenn der **Empfänger** auf ein eigenes Konto / die eigene Bank deutet.

    Nur der Empfänger/Auftraggeber wird geprüft (nicht der Verwendungszweck),
    damit normale Zahlungen nicht fälschlich als Umbuchung gelten. Beispiele:
    ``Felix Ley``, ``Felix DKB``, ``Gemeinschaft DKB``, ``DKB AG``.
    """
    name = _normalize(applicant_name)
    return bool(_OWN_ACCOUNT_HINTS.search(name))


# ------------------------------------------------------------------
# Regeln: Reihenfolge = Priorität (erste passende gewinnt)
# ------------------------------------------------------------------
# Jede Regel: (Kategoriename, Muster über den normalisierten Text).
#
# Es wird per **Teilstring** gesucht (kein \b-Wortrand!), weil die Bank den
# Verwendungszweck ohne Trennzeichen aneinanderhängt, z. B.
# "uberweisungmiete" – ein \b vor "miete" würde dort nicht greifen.
# Umlaute im Muster als ae/oe/ue (siehe _normalize), kurze/mehrdeutige
# Marken werden als " wort " (mit Leerzeichen) gematcht, um Zufallstreffer
# zu vermeiden.
RULES: tuple[tuple[str, str], ...] = (
    # --- Einnahmen -------------------------------------------------
    ("Gehalt & Lohn", r"(lohngehalt|lohn gehalt|lohn/gehalt|monatsgehalt|bruttolohn|nettolohn|gehalt|bezuege|bezu(e|ge)|verguetung|vergutung|pension|ruhegehalt|beamtenbezuege)"),
    ("Kindergeld", r"(kindergeld|familienkasse)"),
    ("Elterngeld", r"(elterngeld|mutterschaftsgeld|basiselterngeld)"),
    ("Steuererstattung", r"(steuererstattung|einkommenst|einkommensteuer|lohnsteuer|solidaritaetszuschlag|steuererstatt|steuer zurueck|steuerzurueck|finanzamt|steuerverwaltung)"),
    ("Erstattungen & Rückzahlungen", r"(rueckerstattung|ruckerstattung|erstattung|rueckzahlung|ruckzahlung|reklamation|gutschrift|krankenkasse|pflegekasse)"),
    ("Zinsen & Kapitalerträge", r"(zinsgutschrift|zinsen|zinsabschluss|dividende|kapitalertrag|ausschuettung|ausschuttung|ertragsanteil|tagesgeld|festgeld)"),
    ("Transfers (intern)", r"(kaution|anzahlung|sicherheit)"),

    # --- Bank & Abgaben -------------------------------------------
    ("Bankgebühren", r"(kontofuehrung|kontofuhrung|kontofuehrungsentgelt|kontofuhrungsentgelt|kontogebuehr|kontogebuehren|kreditkartengebuehr|dispozins|ueberziehungszins|uberziehungszins|r(ue|u)cklastschrift|ruecklastschrift)"),
    ("Steuern & Gebühren", r"(kfzsteuer|kfz steuer|kraftfahrzeugsteuer|rundfunkbeitrag|/gez|\bgez\b|maut|ordnung|behoerde|behorde|buergermeister|bue(r|)germeister|amt fuer|amt fur)"),

    # --- Wohnen ---------------------------------------------------
    ("Miete & Wohnen", r"(miete|mietzahlung|mietzins|hausgeld|wohngeld|immobilien|vermieter|wohnungsbau|vonovia|deutsche wohnen)"),
    ("Nebenkosten & Strom", r"(nebenkosten|betriebskosten|stromabschlag|strom|gasabschlag|heizung|heizkosten|fernwaerme|fernwarme|stadtwerke|energieversorg|energie deutschland|e\.?on|naturstrom|lichtblick|wasserversorg|abwasser|muellabfuhr|mullabfuhr|enbw)"),

    # --- Kommunikation --------------------------------------------
    ("Internet & Telefon", r"(internet|festnetz|dsl|telefon|telekom|vodafone|mobilfunk|handytarif|telefonica|unitymedia|kabel deutschland|glasfaser|hosting|hetzner|strato|ionos|cloudflare|freenet|netcologne|net cologne|1\s?und\s?1)"),
    ("Abos & Streaming", r"(netflix|spotify|disney|amazon prime|prime video|dazn|apple music|apple one|youtube premium|paramount|joyn|audible|kindle unlimited|abonnement|streaming)"),

    # --- Versicherung & Gesundheit --------------------------------
    ("Versicherungen", r"(versicherung|versicherun|gothaer|barmenia|allianz|debeka|generali|rechtsschutz|hausrat|haftpflicht|lebensversicherung|berufsunfaehig|berufsunfahig|krankenversicherung|kfz versicherung)"),
    ("Gesundheit & Apotheke", r"(apotheke|zahnarzt|arztpraxis|arztprax|aerzte|aerztin|physiotherapie|krankenhaus|klinik|optiker|fielmann|brillen|sanitaetshaus|sanitatshaus|rezept|medikament|heilpraktiker|psychotherapie|tierarzt|barmer|techniker krankenkasse)"),

    # --- Einzelhandel & Konsum -------------------------------------
    ("Lebensmittel", r"(rewe|edeka|aldi sued|aldi nord|aldi|lidl|netto marken|netto|penny|kaufland|marktkauf|hit markt|globus|tegut|norma|alnatura|denns|denn s|biomarkt|frischemarkt|supermarkt|lebensmit|s mart|s-mart|baeckerei|backerei|backwerk|schaefer|metzgerei|feinkost|kiosk|getraenkemarkt|getrankemarkt|trinkgut|getraenke|kaffeeroesterei|kaffeerosterei|kaffee|reformhaus|torten|obst|gemuese|germuese)"),
    ("Restaurants & Cafés", r"(restaurant|ristorante|trattoria|pizzeria|pizza|bistro|imbiss|kebap|kebab|doener|doner|sushi|buerger|burger|mcdonald|starbucks|lieferando|lieferdienst|lieferservice|subway|nordsee|gasthaus|gasthof|brauhaus|kantine|catering|eiscafe|eisknabe|eis |steakhouse|estilo|vinothek|kneipe|sumup|coffee|lap coffee)"),
    ("Haushalt & Drogerie", r"(drogerie|rossmann|mueller|buenting|bunting|woolworth|tchibo|haushaltswaren|ikea|poco|segmueller|segmuller|moemax|zurbrueggen|reinigungs|waescherei|wascherei|putzmittel|bauhaus|hornbach|obi |toom|baumarkt|kleinanzeigen)"),
    ("Kleidung & Schuhe", r"(zara|primark|tk maxx|zalando|about you|asos|esprit|s oliver|tom tailor|jack jones|vero moda|mango|sportscheck|decathlon|intersport|deichmann|shoepoint|snipes|jd sports|new yorker|bershka|geox|ecco|dilling|herrenhaus|schuhhaus|bekleidung|modehaus|schuhe)"),
    ("Elektronik & Technik", r"(mediamarkt|media markt|saturn|euronics|notebooksbilliger|alternate|cyberport|galaxus|amazon|amzn|apple store|samsung|xbox|playstation|nintendo|computer|hardware|laptop|monitor|kopfhoerer|kopfhorer|elektronik)"),
    ("Freizeit & Hobby", r"(urban sports|mcfit|fitstar|fitnessstudio|fitness|sportverein|schwimmbad|freibad|cinemaxx|cinestar|kino|theater|ticketmaster|eventim|museum|tierpark|freizeitpark|globotrotter|escape room|bowling|billard|tennis|musikschule|thalia|hugendubel|mayersche|buchhandlung|buchladen|hobby|lauf)"),
    ("Reisen & Urlaub", r"(booking|airbnb|trivago|expedia|ryanair|easyjet|eurowings|condor|lufthansa|hotel|hostel|reisebuero|reiseburo|urlaub|deutsche bahn|flixbus|flix train|europcar|opodo|skyscanner|kayak|fernverkehr)"),
    ("Bildung & Kinder", r"(kindergarten|kita|krippe|schulgeld|studiengebuehr|studiengebuhr|semesterbeitrag|universitaet|universitat|hochschule|volkshochschule|nachhilfe|spielwaren|spielzeug|mytoys|smyths|babymarkt|jugendamt|tagesmutter|elternbeitrag|hort)"),
    ("Spenden & Geschenke", r"(spende|spenden|caritas|diakonie|unicef|aerzte ohne grenzen|greenpeace|rotes kreuz|tierschutz|misereor|kirchengemeinde|pfarrgemeinde|geschenk|floristik|interflora|fleurop)"),
    ("Geldanlagen & Sparen", r"(scalable capital|trade republic|union investment|dekabank|bausparkasse|bausparen|sparplan|wertpapier|kapitalanlage|aktien|depot|etf|bitcoin|krypto|flatex|smartbroker)"),
    ("Barbeträge", r"(geldautomat|bargeld|bargeldauszahlung|geldabhebung|abhebung|auszahlung)"),
    ("Tanken", r"(shell|aral|esso|totalenergies|agip|westfalen|tankstelle|tanken|tankautomat|benzin|diesel|ladestation|lade(sae|sa)ule|wallbox|ionity)"),
    ("Mobilität & ÖPNV", r"(deutschlandticket|semesterticket|verkehrsverbund|nahverkehr|bahncard|kvb|rvk|mvg|hvv|vrr|oebb|carsharing|share now|parkhaus|parkgebuehr|parkgebuhr|parken|tier scooter|bolt|miles|taxi|uber|apcoa|autozubehoer|autozubehor|werkstatt|reifen|inspektion)"),
)


class Rules:
    """Vorberechnete, vorkompilierte Kategorisierungsregeln."""

    def __init__(self, rules=RULES):
        self.compiled = tuple(
            (name, re.compile(pattern)) for name, pattern in rules
        )

    def match(self, text: str, names: set[str] | None = None) -> str | None:
        """Erster passender Kategoriename oder ``None``.

        ``names`` schränkt auf vorhandene Kategorien ein (verhindert Vorschläge
        für Kategorien, die nicht in der Datenbank existieren).
        """
        for name, pattern in self.compiled:
            if names is not None and name not in names:
                continue
            if pattern.search(text):
                return name
        return None


RULES_ENGINE = Rules()


# ------------------------------------------------------------------
# Öffentliche API
# ------------------------------------------------------------------
_INCOME_NAMES = {name for name, ctype in DEFAULT_CATEGORIES if ctype == "income"}

# Händler, deren Rückzahlungen als Erstattung gelten (Rücksendung, Rückbuchung).
_REFUND_HINTS = r"\b(amazon|amzn|paypal|payback|rewe|netto|aldi|lidl|kaufland)\b"


def _pick(name: str, names: set[str] | None) -> str | None:
    """Gibt ``name`` zurück, sofern er (optional) in ``names`` enthalten ist."""
    return name if names is None or name in names else None


def categorize(
    applicant_name: str | None,
    purpose: str | None = None,
    amount: float | None = None,
    *,
    engine: Rules | None = None,
    names: set[str] | None = None,
) -> str | None:
    """Schlägt eine Kategorie (Name) für einen Umsatz vor.

    :param applicant_name: Empfänger/Auftraggeber (``applicant_name``).
    :param purpose: Verwendungszweck (``purpose``).
    :param amount: Betrag als Zahl – Vorzeichen entscheidet über die Richtung.
    :param engine: optionale Regel-Instanz (für Tests).
    :param names: optionale Einschränkung auf existierende Kategorienamen.
    :return: Kategoriename oder ``None`` (``Nicht zugeordnet``).
    """
    engine = engine or RULES_ENGINE
    text = _normalize(applicant_name) + _normalize(purpose)

    positive = amount is not None and float(amount) > 0

    if positive:
        # Einnahmen: spezifische Muster zuerst (Ausgabenmuster überschneiden
        # sich oft, z. B. Steuererstattung vs. Steuern, Versicherung vs.
        # Erstattung, Gehalt vs. Kontoführung).
        income = engine.match(text, _INCOME_NAMES)
        if income:
            return income
        # Rückzahlungen von Händlern als Erstattung führen.
        if re.search(_REFUND_HINTS, text):
            return _pick("Erstattungen & Rückzahlungen", names)
    else:
        # Ausgaben (und unbekannte Richtung): ganz normale Regelprüfung.
        match = engine.match(text, names)
        if match:
            return match

    # Erst wenn kein konkretes Muster greift: Umbuchung zwischen eigenen Konten?
    # (z. B. -750 auf Konto A und +750 auf Konto B sind dieselbe Umbuchung).
    # Das steht bewusst HINTEN, damit z. B. "Gemeinschaft DKB" mit Zweck "MIETE"
    # der Miete zugeordnet wird und nicht als Umbuchung gilt.
    if _looks_like_own_transfer(applicant_name):
        return _pick("Transfers (intern)", names)

    if positive:
        return _pick("Sonstige Einnahmen", names)
    return None


# ------------------------------------------------------------------
# Datenbank
# ------------------------------------------------------------------
def ensure_default_categories(connection: sqlite3.Connection) -> None:
    """Legt die Vorschlagskategorien an, ohne vorhandene zu verändern (idempotent)."""
    connection.executemany(
        "INSERT OR IGNORE INTO categories (name, type) VALUES (?, ?)",
        DEFAULT_CATEGORIES,
    )


def _category_ids(connection: sqlite3.Connection) -> dict[str, int]:
    """Bildet Kategoriename -> id ab."""
    return {row[0]: row[1] for row in connection.execute("SELECT name, id FROM categories")}


# ------------------------------------------------------------------
# Gelernte Regeln (aus manueller Zuordnung)
# ------------------------------------------------------------------
# Ordnet der Nutzer eine Kombination aus Partner (Empfänger) und
# Verwendungszweck manuell einer Kategorie zu, wird sie hier festgehalten.
# Künftige Umsätze mit derselben Kombination werden dann automatisch so
# zugeordnet – noch vor den eingebauten Regeln (RULES). So „lernt“ das
# Haushaltsbuch aus einer einmaligen manuellen Zuordnung.
_LEARNED_RULES_DDL = """\
CREATE TABLE IF NOT EXISTS learned_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_key TEXT NOT NULL,               -- normalisierter Partner (IBAN-frei)
    purpose_key TEXT NOT NULL,               -- normalisierter Verwendungszweck
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    match_count INTEGER NOT NULL DEFAULT 1,  -- so oft die Kombination manuell zugeordnet wurde
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (partner_key, purpose_key)
);
"""


def ensure_learned_rules_table(connection: sqlite3.Connection) -> None:
    """Legt die Tabelle ``learned_rules`` an, falls sie fehlt (idempotent)."""
    connection.executescript(_LEARNED_RULES_DDL)


def match_key(applicant_name: str | None, purpose: str | None) -> tuple[str, str]:
    """Bildet den Vergleichsschlüssel ``(Partner, Verwendungszweck)``.

    Der Partner wird von IBAN-Resten befreit (siehe ``web.partner``) und
    zusammen mit dem Verwendungszweck auf Kleinschreibung sowie einfache
    Leerzeichen normalisiert. Dadurch treffen auch Umsätze zusammen, bei
    denen die Bank die IBAN mal voranstellt und mal nicht.
    """
    partner = " ".join(clean_partner_name(applicant_name or "").split()).casefold()
    zweck = " ".join((purpose or "").split()).casefold()
    return partner, zweck


def load_learned_rules(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Bildet ``(Partner, Verwendungszweck)`` auf den gelernten Kategorienamen ab.

    Regeln, deren Kategorie nicht mehr existiert, werden ausgelassen (die
    Fremdschlüsselbeziehung entfernt sie normalerweise ohnehin).
    """
    ensure_learned_rules_table(connection)
    return {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """SELECT lr.partner_key, lr.purpose_key, c.name
                 FROM learned_rules lr
                 JOIN categories c ON c.id = lr.category_id"""
        )
    }


def suggest_category(
    learned: dict[tuple[str, str], str] | None,
    applicant_name: str | None,
    purpose: str | None,
    amount: float | None = None,
    *,
    names: set[str] | None = None,
) -> str | None:
    """Vorschlag für einen Umsatz: erst gelernte Regeln, dann ``categorize``.

    Eine gelernte Regel (aus einer früheren manuellen Zuordnung) hat Vorrang
    vor den eingebauten Regeln – der Nutzerwille gewinnt.
    """
    if learned:
        name = learned.get(match_key(applicant_name, purpose))
        if name is not None and (names is None or name in names):
            return name
    return categorize(applicant_name, purpose, amount, names=names)


def learn_from_manual_assignment(
    connection: sqlite3.Connection,
    tx_id: int,
    category_id: int | None,
) -> int:
    """Merkt sich eine manuelle Zuordnung und wendet sie auf gleiche Umsätze an.

    Der Aufrufer ist für ``commit()`` verantwortlich.

    :param tx_id: der manuell zugeordnete Umsatz.
    :param category_id: Zielkategorie oder ``None`` (Zuordnung aufheben).
    :return: Anzahl der Umsätze, die diese Kategorie erhalten haben (inkl. tx_id).
    """
    row = connection.execute(
        "SELECT applicant_name, purpose FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()
    if row is None:
        return 0

    key = match_key(row[0], row[1])
    ensure_learned_rules_table(connection)

    if category_id is None:
        # Zuordnung aufgehoben -> gelernte Regel wieder entfernen, damit künftige
        # Umsätze nicht fälschlich zugeordnet werden.
        connection.execute(
            "DELETE FROM learned_rules WHERE partner_key = ? AND purpose_key = ?", key
        )
        connection.execute(
            "UPDATE transactions SET category_id = NULL WHERE id = ?", (tx_id,)
        )
        return 0

    # Regel merken bzw. aktualisieren (match_count zählt manuelle Zuordnungen).
    connection.execute(
        """INSERT INTO learned_rules (partner_key, purpose_key, category_id)
               VALUES (?, ?, ?)
           ON CONFLICT(partner_key, purpose_key) DO UPDATE SET
                   category_id = excluded.category_id,
                   match_count = learned_rules.match_count + 1,
                   updated_at  = datetime('now')""",
        (key[0], key[1], category_id),
    )
    return _apply_learned_rule(connection, key, category_id, target_id=tx_id)


def _apply_learned_rule(
    connection: sqlite3.Connection,
    key: tuple[str, str],
    category_id: int,
    *,
    target_id: int | None = None,
) -> int:
    """Setzt die Kategorie für ``target_id`` und alle noch offenen gleichen Umsätze.

    Bereits zugeordnete Umsätze (z. B. eine abweichende manuelle Zuordnung)
    bleiben unangetastet – nur leere Zuordnungen werden gefüllt.
    """
    changed = 0
    rows = connection.execute(
        "SELECT id, applicant_name, purpose, category_id FROM transactions"
    ).fetchall()
    for tx_id, applicant, purpose, current in rows:
        if match_key(applicant, purpose) != key:
            continue
        if tx_id != target_id and current is not None:
            continue
        connection.execute(
            "UPDATE transactions SET category_id = ? WHERE id = ?", (category_id, tx_id)
        )
        changed += 1
    return changed


def _matching_transactions(
    connection: sqlite3.Connection, key: tuple[str, str]
) -> list[tuple[int, str, str, int | None]]:
    """Alle Umsätze, deren Vergleichsschlüssel ``key`` entspricht."""
    rows = connection.execute(
        "SELECT id, applicant_name, purpose, category_id FROM transactions"
    ).fetchall()
    return [r for r in rows if match_key(r[1], r[2]) == key]


def count_other_matches(
    connection: sqlite3.Connection,
    tx_id: int,
    category_id: int | None,
) -> int:
    """Zählt weitere Umsätze mit gleichem Schlüssel, deren Kategorie sich ändern würde.

    Dient der Vorab-Info im Frontend ("betrifft N weitere Umsätze"), bevor der
    Nutzer das Überschreiben bestehender Zuordnungen bestätigt.
    """
    if category_id is None:
        return 0
    row = connection.execute(
        "SELECT applicant_name, purpose FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()
    if row is None:
        return 0
    key = match_key(row[0], row[1])
    return sum(
        1
        for tid, _applicant, _purpose, current in _matching_transactions(connection, key)
        if tid != tx_id and current != category_id
    )


def apply_rule_to_all_matches(
    connection: sqlite3.Connection,
    tx_id: int,
    category_id: int | None,
) -> int:
    """Setzt ``category_id`` für ALLE Umsätze mit gleichem Vergleichsschlüssel.

    Anders als ``learn_from_manual_assignment`` werden auch bereits zugeordnete
    Umsätze überschrieben. Der Aufrufer muss das vorher vom Nutzer bestätigen
    lassen (``apply_to_all`` im PATCH-Body). Die gelernte Regel wird dabei
    ebenfalls gespeichert bzw. aktualisiert.
    """
    row = connection.execute(
        "SELECT applicant_name, purpose FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()
    if row is None:
        return 0
    key = match_key(row[0], row[1])
    ensure_learned_rules_table(connection)

    if category_id is None:
        connection.execute(
            "DELETE FROM learned_rules WHERE partner_key = ? AND purpose_key = ?", key
        )
    else:
        connection.execute(
            """INSERT INTO learned_rules (partner_key, purpose_key, category_id)
                   VALUES (?, ?, ?)
               ON CONFLICT(partner_key, purpose_key) DO UPDATE SET
                       category_id = excluded.category_id,
                       match_count = learned_rules.match_count + 1,
                       updated_at  = datetime('now')""",
            (key[0], key[1], category_id),
        )

    matches = _matching_transactions(connection, key)
    for tid, _applicant, _purpose, current in matches:
        if current == category_id:
            continue
        connection.execute(
            "UPDATE transactions SET category_id = ? WHERE id = ?", (category_id, tid)
        )
    # Rückgabe = Anzahl der Umsätze, die diese Kategorie jetzt tragen (nicht nur
    # die geänderten) – so meldet das Frontend „auf N Umsätze angewandt".
    return len(matches)


def apply_categorization_to_db(
    db_path: Path,
    *,
    dry_run: bool = False,
    only_uncategorized: bool = True,
) -> tuple[int, int]:
    """Kategorisiert Umsätze automatisch.

    :param only_uncategorized: ``True`` lässt manuell gesetzte Kategorien in
        Ruhe (empfohlen). ``False`` überschreibt auch vorhandene Zuordnungen.
    :return: (geprueft, geaendert)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_default_categories(conn)
        ids = _category_ids(conn)
        names = set(ids)
        learned = load_learned_rules(conn)

        where = "WHERE t.category_id IS NULL" if only_uncategorized else ""
        rows = conn.execute(
            f"""SELECT t.id, t.applicant_name, t.purpose, t.amount, t.category_id
                FROM transactions t {where}"""
        ).fetchall()

        scanned = len(rows)
        changed = 0
        for row in rows:
            proposal = suggest_category(
                learned, row["applicant_name"], row["purpose"], row["amount"], names=names
            )
            if proposal is None:
                continue
            changed += 1
            if not dry_run:
                conn.execute(
                    "UPDATE transactions SET category_id = ? WHERE id = ?",
                    (ids[proposal], row["id"]),
                )
        if not dry_run:
            conn.commit()
        return scanned, changed
    finally:
        conn.close()


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def _resolve_db_path(arg: str | None) -> Path:
    """Ermittelt den DB-Pfad: CLI-Argument > DB_PATH aus .env > finance.db."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    if arg:
        path = Path(arg)
        return path if path.is_absolute() else ROOT / path
    env = os.getenv("DB_PATH", "finance.db")
    path = Path(env)
    return path if path.is_absolute() else ROOT / path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Kategorisiert Umsätze automatisch anhand von Empfänger und Verwendungszweck.",
    )
    ap.add_argument("--db", default=None, help="Pfad zur SQLite-DB (Default: DB_PATH/.env, sonst finance.db)")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben.")
    ap.add_argument("--overwrite", action="store_true",
                    help="Auch bereits zugeordnete Umsätze neu bewerten (Default: nur leere).")
    args = ap.parse_args()

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Datenbank nicht gefunden: {db_path}")

    if args.dry_run:
        print("*** DRY-RUN: Es wird nichts in die Datenbank geschrieben. ***")
    print(f"Datenbank: {db_path}")

    scanned, changed = apply_categorization_to_db(
        db_path, dry_run=args.dry_run, only_uncategorized=not args.overwrite
    )
    verb = "würden kategorisiert" if args.dry_run else "kategorisiert"
    print(f"\nFertig. {scanned} Umsätze geprüft, {changed} {verb}.")
    if scanned and not args.dry_run:
        print(f"{scanned - changed} Umsätze bleiben 'Nicht zugeordnet' (manuell zuordnen).")


if __name__ == "__main__":
    sys.exit(main())

