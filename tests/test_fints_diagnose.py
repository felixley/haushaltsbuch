"""Tests für robuste Kunden-ID-Varianten (fints_session) und fints_diagnose.

Kein echter Netzwerkzugriff: FinTS3PinTanClient wird durch einen Fake ersetzt.
Die PIN wird nicht in .env/-Datenbanken geschrieben; für Diagnose-Tests wird
die PIN nur als Literal im Test-Scope verwendet und darf in der Ausgabe nie
auftauchen.
"""

from __future__ import annotations

import logging

import pytest
from fints.exceptions import FinTSClientPINError

import fints_session
import fints_diagnose
from fints_session import sync_system_id

PIN = "geheim4711"
USER = "12345678900"  # comdirect-artige Benutzerkennung (Zugangsnummer)


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
    comdirect-konform (customer_id == '') -> Erfolg."""

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
            return {}                              # comdirect-konform: Login ok
        raise FinTSClientPINError(f"9942 – PIN ungültig (Eingabe: {self.pin.value})")


def test_diagnose_recommends_comdirect_conform_after_9931(monkeypatch):
    """A scheitert mit 9931 (Kunden-ID unbekannt), B (customer_id='') gelingt."""
    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", DiagnoseFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True,
    )
    assert "A) Default" in out
    assert "B) comdirect-konform" in out
    assert "9942" in out                              # Bankcode in der Zusammenfassung
    assert 'client.customer_id = ""' in out           # comdirect-Empfehlung
    assert PIN not in out                             # PIN steht nicht in der Ausgabe
    assert "123***00" in out                          # Benutzerkennung maskiert


def test_diagnose_default_ok_needs_no_workaround(monkeypatch):
    class AlwaysOk(DiagnoseFake):
        def fetch_tan_mechanisms(self):
            return {}

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", AlwaysOk)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True,
    )
    assert "OK" in out
    assert "kein Workaround nötig" in out
    assert "übersprungen" in out          # kein zweiter Login-Versuch (Sperrschutz)
    assert PIN not in out


def test_build_client_failure_is_reported_not_crash(monkeypatch):
    """Ein Fehler beim Client-Bau (build_client) bricht die Diagnose NICHT mehr ab."""
    class BuildFailFake:
        def __init__(self, *a, **k):
            raise FinTSClientPINError(f"connection refused for user {USER}")

        def fetch_tan_mechanisms(self):
            return {}

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", BuildFailFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True,
    )
    assert "FEHLER" in out
    assert "Verbindungsaufbau fehlgeschlagen" in out
    assert "Traceback" not in out
    assert PIN not in out
    assert USER not in out


def test_variant_b_sets_empty_customer_id_and_runs_single_attempt(monkeypatch):
    """Variante B setzt customer_id='' und macht genau EINEN fetch-Versuch."""
    instances = []

    class CountingFake:
        def __init__(self, *a, **k):
            self.user_id = USER
            self.customer_id = "NOTSET"
            self.fetch_calls = 0
            self.customer_at_fetch = None
            self.pin = Pin(PIN)
            instances.append(self)

        def fetch_tan_mechanisms(self):
            self.fetch_calls += 1
            self.customer_at_fetch = self.customer_id
            if self.customer_id == "":
                return {}
            raise FinTSClientPINError(f"PIN {self.pin.value} wrong")

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", CountingFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True, variant="b",
    )
    fake = instances[0]
    assert fake.customer_id == ""              # leer, NICHT None/from user_id
    assert fake.customer_at_fetch == ""        # war beim Fetch bereits leer
    assert fake.fetch_calls == 1               # genau EIN Login-Versuch
    assert "B) comdirect-konform" in out
    assert "OK" in out
    assert PIN not in out


def test_missing_hisyn4_recommends_raw_log(monkeypatch):
    """Kein HISYN4 (Could not find system_id) -> Empfehlung verweist auf --log."""
    class NoHisynFake:
        def __init__(self, *a, **k):
            self.user_id = USER
            self.customer_id = None
            self.pin = Pin(PIN)

        def fetch_tan_mechanisms(self):
            raise ValueError(f"Could not find system_id | no HISYN4 (user {USER})")

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", NoHisynFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True,
    )
    assert "--log" in out
    assert "HISYN4" in out
    assert PIN not in out
    assert USER not in out


def test_log_flag_keeps_pin_and_username_masked(monkeypatch):
    """Auch mit --log dürfen PIN und Benutzerkennung nicht im Output stehen."""
    class LogFake:
        def __init__(self, *a, **k):
            self.user_id = USER
            self.customer_id = None
            self.pin = Pin(PIN)

        def fetch_tan_mechanisms(self):
            # Die Bank "echot" die Zugangsdaten in die Rohantwort.
            logging.getLogger("fints").debug(
                f"Dialog response: Benutzer {self.user_id} PIN {self.pin.value} "
                "HISYN4 Segment 9931"
            )
            raise FinTSClientPINError(f"PIN {self.pin.value} wrong")

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", LogFake)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True, log=True,
    )
    assert "Dialog response" in out        # Log wird angezeigt (--log)
    assert "9931" in out                   # Code sichtbar
    assert PIN not in out                  # PIN maskiert, trotz --log
    assert USER not in out                 # Benutzerkennung maskiert, trotz --log


def test_variant_alle_runs_only_until_success(monkeypatch):
    """--variante alle stoppt, sobald eine Variante erfolgreich ist."""
    calls = []

    class OkFirst(DiagnoseFake):
        def fetch_tan_mechanisms(self):
            calls.append(getattr(self, "customer_id", None))
            if self.customer_id == "":
                return {}
            raise FinTSClientPINError(f"PIN {self.pin.value} wrong")

    monkeypatch.setattr(fints_session, "FinTS3PinTanClient", OkFirst)
    monkeypatch.delenv("FINTS_CUSTOMER_ID_WORKAROUND", raising=False)
    out = fints_diagnose.diagnose(
        "comdirect", "20041133", USER, PIN, "https://x", yes=True, variant="alle",
    )
    # A scheitert, B (customer_id='') gelingt -> C wird nie ausgeführt.
    assert calls == [None, ""]
    assert "C) finance-tracker-Workaround" not in out
    assert PIN not in out