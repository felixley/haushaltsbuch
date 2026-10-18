"""Regressionstest: Zuordnung auf gleichartige Umsätze ohne native Dialoge.

Die Weitergabe an Umsätze mit abweichender Kategorie wurde früher über
``window.confirm()`` bestätigt. Chrome unterdrückt weitere native Dialoge nach
wenigen Bestätigungen („Diese Seite daran hindern, weitere Dialoge zu
erstellen") – danach liefert ``confirm()`` stillschweigend ``false`` und die
Zuordnung unterblieb ohne sichtbare Meldung. Das Frontend nutzt deshalb einen
eigenen Dialog.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "main.js").read_text(encoding="utf-8")


def test_dialog_elemente_vorhanden() -> None:
    for element_id in ("confirm-modal", "confirm-text", "confirm-ok",
                       "confirm-cancel", "confirm-backdrop"):
        assert f'id="{element_id}"' in HTML, f"{element_id} fehlt in index.html"


def test_askconfirm_wird_verwendet() -> None:
    assert "function askConfirm(" in JS
    assert "await askConfirm(" in JS


def test_kategorie_zuordnung_nutzt_keinen_nativen_dialog() -> None:
    # window.confirm darf nur noch als Fallback innerhalb von askConfirm stehen
    # (Kommentarzeilen zählen nicht mit).
    code = [line for line in JS.splitlines() if not line.strip().startswith("//")]
    code_text = "\n".join(code)
    assert code_text.count("window.confirm") == 1
    assert "const ok = confirm(" not in JS
    assert "if (!confirm(" not in JS


def test_weitergabe_ist_an_confirmation_gebunden() -> None:
    """Der Bestätigungspfad für abweichende Kategorien bleibt erhalten."""
    assert "apply_to_all: true" in JS
    assert "res.affected > 0" in JS
