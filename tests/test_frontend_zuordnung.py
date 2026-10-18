"""Regressionsschutz für die Umsatz-Zuordnung im Frontend.

Die Kategorie eines Umsatzes wird beim Ändern automatisch an alle Umsätze mit
gleichem Partner und Verwendungszweck weitergegeben (auch bei anderem Betrag).
Das Frontend darf dafür **keinen** nativen Dialog (``window.confirm``) nutzen:
Chrome unterdrückt weitere native Dialoge nach wenigen Bestätigungen, danach
liefe die Weitergabe stillschweigend ins Leere. Stattdessen informiert ein
Toast mit „Rückgängig“-Knopf.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "main.js").read_text(encoding="utf-8")


def _code() -> str:
    """Nur Codezeilen (ohne Kommentare) – Kommentare dürfen window.confirm nennen."""
    return "\n".join(
        line for line in JS.splitlines() if not line.strip().startswith("//")
    )


def test_kategorie_zuordnung_ohne_nativen_dialog() -> None:
    code = _code()
    assert "const ok = confirm(" not in JS
    assert "res.affected" not in code      # Rückfrage-Pfad ist entfallen


def test_zuordnung_meldet_treffer_und_aenderungen() -> None:
    # Ehrliche Meldung: Treffer, tatsächlich geänderte Zeilen und Rückleseprüfung.
    assert "Treffer" in JS
    assert "res.updated" in JS and "res.total" in JS
    assert "in der Datenbank angekommen" in JS
    assert "overwritten" in JS


def test_rueckgaengig_ist_verdrahtet() -> None:
    assert "/api/transactions/category/undo" in JS
    assert "undo_token" in JS
    assert "Rückgängig" in JS


def test_dialog_fuer_kategorie_loeschen_bleibt_verfuegbar() -> None:
    # Eigener Dialog statt window.confirm (nur noch für das Löschen nötig).
    for element_id in ("confirm-modal", "confirm-text", "confirm-ok", "confirm-cancel"):
        assert f'id="{element_id}"' in HTML
    assert "function askConfirm(" in JS
    assert _code().count("window.confirm") == 1     # nur der Fallback in askConfirm


def test_frontend_version_ist_sichtbar_und_cache_busted() -> None:
    """Sichtbare Version (aus /api/info) + Query-Parameter gegen Cache-Probleme."""
    assert 'id="app-version"' in HTML
    assert 'src="/static/main.js?v=' in HTML
    assert "/api/info" in JS
