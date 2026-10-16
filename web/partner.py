"""Bereinigung des Partner-Namens für die Umsatzansicht.

Manche Banken hängen in das Feld ``applicant_name`` (Empfänger/Auftraggeber)
die IBAN des Gegenkontos, oft direkt ohne Trennzeichen, z. B.::

    "DE03760300800870717444FELIX LEY"
    "DE65200411550859475600Felix Ley"
    "DE96120300009005290904REWE.Koeln.Lindent/Koeln"

Auch die gedruckte, in Vierergruppen getrennte Schreibweise kommt vor::

    "Max Mustermann DE89 3704 0044 0532 0130 00"

In der GUI soll in der Spalte *Partner* nur die eigentliche Bezeichnung stehen –
ohne IBAN. Dieses Modul entfernt IBAN-artige Teilstrings per regulärem
Ausdruck: **zwei Buchstaben gefolgt von mindestens acht Ziffern**. Es bleibt
nur der darauf folgende Text übrig.

Enthält ein Name keine solche Zeichenfolge, wird er unverändert
zurückgegeben (keine Whitespace-Normalisierung), damit unbeteiligte Einträge
nicht angetastet werden.
"""

from __future__ import annotations

import re

__all__ = ["clean_partner_name", "extract_ibans"]

# IBAN-artige Zeichenkette: zwei Buchstaben (Ländercode) gefolgt von
# mindestens acht Ziffern. Zwei Schreibweisen werden erkannt:
#   - kompakt/verklebt:  DE03760300800870717444   (+ direkt folgender Name)
#   - gedruckt/gruppiert: DE89 3704 0044 0532 0130 00
# Die führende negative Lookbehind verhindert einen Treffer mitten in einem
# Wort (z. B. innerhalb von "SUMUP"): vor den zwei Buchstaben darf kein
# Buchstabe/Ziffer stehen.
_IBAN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"[A-Za-z]{2}\d{2}(?:[ -]\d{4}){3,}(?:[ -]?\d{0,2})"  # gruppiert (mit Leerzeichen/Bindestrich)
    r"|"
    r"[A-Za-z]{2}\d{8,}"                                    # kompakt/verklebt
    r")"
)


def _strip(value: str) -> str:
    """Entfernt Leerzeichen/Bindestriche aus einem Treffer (für ``extract_ibans``)."""
    return re.sub(r"[ -]", "", value)


def extract_ibans(text: str | None) -> list[str]:
    """Liefert alle erkannten IBAN-artigen Teilstrings aus ``text``.

    Die Rückgabe ist von Trennzeichen (Leerzeichen/Bindestrich) befreit,
    also z. B. ``["DE89370400440532013000"]`` für die gedruckte Schreibweise.
    """
    if not text:
        return []
    return [_strip(m.group(0)) for m in _IBAN_RE.finditer(text)]


def clean_partner_name(name: str | None) -> str:
    """Entfernt IBAN-artige Teilstrings (2 Buchstaben + mind. 8 Ziffern) aus ``name``.

    Es bleibt nur der darauf folgende Text übrig; übrig gebliebene Trenner
    (doppelte Leerzeichen, führende/abschließende Kommas/Bindestriche) werden
    aufgeräumt. Enthält ``name`` keinen Treffer, wird er **unverändert**
    zurückgegeben. Beispiele::

        >>> clean_partner_name("DE03760300800870717444FELIX LEY")
        'FELIX LEY'
        >>> clean_partner_name("Muster GmbH, DE89370400440532013000")
        'Muster GmbH'
        >>> clean_partner_name("Max Mustermann DE89 3704 0044 0532 0130 00")
        'Max Mustermann'
        >>> clean_partner_name("REWE SAG Köln 112")
        'REWE SAG Köln 112'
    """
    if not name:
        return name or ""

    # Kein Treffer -> Eintrag bleibt unberührt (keine Whitespace-Normalisierung).
    if not _IBAN_RE.search(name):
        return name

    cleaned = _IBAN_RE.sub(" ", name)
    # Doppelte Leerzeichen zusammenfassen und führende/abschließende Trenner entfernen.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t,;/-–—")
    return cleaned
