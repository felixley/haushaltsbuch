"""Bereinigung des Partner-Namens für die Umsatzansicht.

Manche Banken schreiben in das Feld ``applicant_name`` (Empfänger/Auftraggeber)
zusätzlich die IBAN des Gegenkontos, z. B.::

    "Max Mustermann DE89 3704 0044 0532 0130 00"
    "Muster GmbH, DE89370400440532013000"
    "DE89 3704 0044 0532 0130 00 Max Mustermann"

In der GUI soll in der Spalte *Partner* nur die eigentliche Bezeichnung stehen –
ohne IBAN. Dieses Modul entfernt IBAN-artige Teilstrings, prüft sie aber vorher
gegen die ISO-13616-Prüfsumme (Modulo 97), damit Namen wie "Rewe 1234" nicht
versehentlich zerstückelt werden.
"""

from __future__ import annotations

import re

__all__ = ["clean_partner_name", "extract_ibans"]

# Ein IBAN-Kandidat: zwei Buchstaben (Ländercode) + zwei Prüfziffern, danach die
# BBAN (11–30 alphanumerische Zeichen). Die gedruckte Schreibweise trennt die
# BBAN in Vierergruppen, die durch Leerzeichen (oder Bindestrich) getrennt sein
# können. Umgebende Wortgrenzen verhindern Treffer mitten in einem Wort.
_IBAN_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z]{2}\d{2}"
    r"(?:[ -]?[A-Za-z0-9]{4}){2,7}"
    r"[ -]?[A-Za-z0-9]{0,3}"
    r"(?![A-Za-z0-9])"
)


def _to_digits(value: str) -> str:
    """Wandelt einen IBAN-String in die Ziffernfolge für die Mod-97-Prüfung um.

    Buchstaben A–Z werden nach ISO 13616 durch 10–35 ersetzt, Ziffern bleiben.
    """
    return "".join(str(int(ch, 36)) for ch in value)


def _is_valid_iban(candidate: str) -> bool:
    """Prüft Länge, Aufbau und Prüfsumme (Modulo 97 == 1) eines Kandidaten."""
    s = re.sub(r"[ -]", "", candidate).upper()
    if not 15 <= len(s) <= 34:
        return False
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", s):
        return False
    # Erste vier Zeichen ans Ende, Buchstaben -> Zahlen, dann Mod 97.
    rearranged = s[4:] + s[:4]
    return int(_to_digits(rearranged)) % 97 == 1


def extract_ibans(text: str) -> list[str]:
    """Liefert alle gültigen IBANs, die in ``text`` vorkommen (ohne Leerzeichen)."""
    if not text:
        return []
    found = []
    for match in _IBAN_CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        if _is_valid_iban(candidate):
            found.append(re.sub(r"[ -]", "", candidate).upper())
    return found


def clean_partner_name(name: str | None) -> str:
    """Entfernt gültige IBANs aus ``name`` und räumt übrig gebliebene Trenner auf.

    Nicht-IBAN-Inhalte bleiben unverändert. Beispiele::

        >>> clean_partner_name("Max Mustermann DE89 3704 0044 0532 0130 00")
        'Max Mustermann'
        >>> clean_partner_name("Muster GmbH, DE89370400440532013000")
        'Muster GmbH'
        >>> clean_partner_name("REWE SAG Köln 112")
        'REWE SAG Köln 112'
    """
    if not name:
        return name or ""

    def _replace(match: re.Match[str]) -> str:
        # Nur echte (prüfsummengültige) IBANs entfernen, alles andere behalten.
        return " " if _is_valid_iban(match.group(0)) else match.group(0)

    cleaned = _IBAN_CANDIDATE_RE.sub(_replace, name)
    # Doppelte Leerzeichen zusammenfassen und führende/abschließende Trenner entfernen.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t,;/-–—")
    return cleaned
