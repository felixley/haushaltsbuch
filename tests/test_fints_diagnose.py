"""Tests für robuste Kunden-ID-Varianten (fints_session) und fints_diagnose.

Kein echter Netzwerkzugriff: FinTS3PinTanClient wird durch einen Fake ersetzt.
Die PIN wird nicht in .env/-Datenbanken geschrieben; für Diagnose-Tests wird
die PIN nur als Literal im Test-Scope verwendet und darf in der Ausgabe nie
auftauchen.
"""

from __future__ import annotations

import pytest
from fints.exceptions import FinTSClientPINError

import fints_session
import fints_diagnose
from fints_session import sync_system_id

PIN = "geheim4711"


class Pin:
    """Minimaler Ersatz für fints.utils.Password: bietet nur ``.value``."""

    def __init__(self, value):
        self.value = value


# ---------------------------------------------------------------- sync_system_id

class SyncFake:
    """Bildet den System-ID-Sync der Bibliothek nach: leere customer_id -> Fehler."""

    fail_first: bool = True

    def __init__(self, user_id="testuser", pin=PIN):
        self.user_id = user_id
        self.pin = Pin(pin)
        self.customer_id = None
        self.system_id = "0"
        self.calls: list = []

    def _ensure_system_id(self):
        self.calls.append(self.customer_id)
        if self.customer_id == "" and self.fail_first:
            raise FinTSClientPINError(f"PIN wrong? (Eingabe: {self.pin.value})")
        self.system_id = "SYS123"


def test_sync_system_id_tries_second_candidate_after_pin_error():
    f = SyncFake()
    sync_system_id(f)
    assert f.calls == ["", "testuser"]          # "" scheitert, user_id wird versucht
    assert f.system_id == "SYS123"               # zweiter Kandidat gelingt


def test_sync_system_id_both_fail_raises_with_all_errors_and_pin_masked():
    class AlwaysFails(SyncFake):
        def _ensure_system_id(self):
            self.calls.append(self.customer_id)
            raise FinTSClientPINError(f"PIN wrong? (Eingabe: {self.pin.value})")

    f = AlwaysFails()
    with pytest.raises(fints_session.FinTSSetupError) as excinfo:
        sync_system_id(f)
    msg = str(excinfo.value)
    assert f.calls == ["", "testuser"]           # beide Kandidaten wurden versucht
    assert "PIN wrong?" in msg                    # die Fehler sind enthalten
    assert "customer_id" in msg
    assert PIN not in msg                         # PIN nirgends im Fehlertext


# ---------------------------------------------------------------- fints_diagnose

def test_extract_codes_finds_known_codes_in_order():
    text = "Erste 9942 Meldung dann 9100 und wieder 9942"
    assert fints_diagnose.extract_codes(text) == ["9942", "9100"]
    assert fints_diagnose.extract_codes("") == []
    assert fints_diagnose.extract_codes("keine codes hier") == []


class DiagnoseFake:
    """Default-Pfad (customer_id == user_id) -> FinTSClientPINError '9942'.
    Workaround-Pfad (customer_id == '') -> Erfolg."""

    def __init__(self, blz, user, pin, server, product_id=None, **kw):
        self.user_id = user
        self.customer_id = None
        self.system_id = "0"
        self.pin = Pin(pin)

    def _ensure_system_id(self):
        if self.customer_id == "":
            raise ValueError("no HISYN4 without customer_id")
        self.system_id = "SYS123"

    def fetch_tan_mechanisms(self):
        if self.customer_id == "":
            return {}                              # Workaround-Pfad: Login ok
        raise FinTSClientPINError(f"9942 – PIN ungültig (Eingabe: {self.pin.value})")


def test_diagnose_recommends_workaround_and_hides_pin(monkeypatch):
    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", DiagnoseFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", "12345678900", PIN,
        "https://fints.comdirect.de/fints", yes=True,
    )
    assert "A) Default" in out and "B) Workaround" in out
    assert "9942" in out                              # Bankcode in der Zusammenfassung
    assert "FINTS_CUSTOMER_ID_WORKAROUND=1" in out    # empfiehlt den Workaround
    assert PIN not in out                             # PIN steht nicht in der Ausgabe
    assert "123***00" in out                          # Benutzerkennung maskiert


def test_diagnose_default_ok_needs_no_workaround(monkeypatch):
    class AlwaysOk(DiagnoseFake):
        def fetch_tan_mechanisms(self):
            return {}

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", AlwaysOk)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", "12345678900", PIN, "https://x", yes=True,
    )
    assert "OK" in out
    assert "kein Workaround nötig" in out
    assert "übersprungen" in out          # kein zweiter Login-Versuch (Sperrschutz)
    assert PIN not in out