"""Ablauf-Tests für den GUI-Bankabruf (web/sync.py + web/app.py).

Es wird KEIN echter Bankzugang genutzt: FinTS3PinTanClient wird durch einen
Fake ersetzt, der die Antworten der Bank nachbildet.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fints.client import NeedTANResponse

import web.app as webapp
from web import sync

PIN = "geheim4711"


class FakeTxn:
    def __init__(self, data):
        self.data = data


class FakeAccount:
    iban = "DE00000000000000000001"
    bic = "TESTDEFF"
    accountnumber = "1"
    subaccount = None
    blz = "20041133"


class _TanRequest:
    def __init__(self, text):
        self.challenge = text  # die Bibliothek parst die Challenge im Konstruktor


def make_need_tan(text="Bitte TAN eingeben", decoupled=False):
    r = NeedTANResponse(None, _TanRequest(text))
    r.decoupled = decoupled
    return r


class FakeClient:
    """Bildet den Ablauf einer Bank nach. Verhalten je Test über Klassenattribute steuern."""

    scenario = "tan"           # tan | decoupled | choice | wrong_pin
    seen_pins: list = []       # zum Prüfen, mit welcher PIN eingeloggt wurde
    sent_tans: list = []

    def __init__(self, blz, user, pin, server, product_id=None, **kw):
        if self.scenario == "wrong_pin" and pin != PIN:
            raise RuntimeError("PIN ungültig (9942)")
        FakeClient.seen_pins.append(pin)
        self.selected_tan_medium = None
        self.init_tan_response = None
        self._media = None

    # Login-/Konfigurationsschritte
    def fetch_tan_mechanisms(self):
        pass

    def set_tan_mechanism(self, _):
        pass

    def is_tan_media_required(self):
        return self.scenario == "choice"

    def get_tan_media(self):
        class M:
            def __init__(self, n):
                self.tan_medium_name = n
                self.mobile_number_masked = "****123"
                self.last_use = None
        return (None, [M("Handy A"), M("Handy B")])

    def set_tan_medium(self, m):
        self.selected_tan_medium = getattr(m, "tan_medium_name", m)

    def __enter__(self):
        if self.scenario in ("tan", "decoupled"):
            self.init_tan_response = make_need_tan(decoupled=self.scenario == "decoupled")
        return self

    def __exit__(self, *a):
        return False

    def send_tan(self, challenge, tan):
        FakeClient.sent_tans.append(tan)
        return None if challenge is self.init_tan_response else [FakeTxn(d) for d in TXNS]

    def get_sepa_accounts(self):
        return [FakeAccount()]

    def get_transactions(self, account, start_date=None, end_date=None, **kw):
        return make_need_tan("Umsatzabruf: TAN bestätigen")


TXNS = [
    {"date": dt.date(2026, 10, 1), "amount": Decimal("-12.34"), "currency": "EUR",
     "purpose": "Testumsatz 1", "applicant_name": "Laden A", "status": "C"},
    {"date": dt.date(2026, 10, 2), "amount": Decimal("100.00"), "currency": "EUR",
     "purpose": "Testumsatz 2", "applicant_name": "Arbeitgeber", "status": "C"},
]


@pytest.fixture(autouse=True)
def env_and_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMDIRECT_BLZ", "20041133")
    monkeypatch.setenv("COMDIRECT_USERNAME", "testuser")
    monkeypatch.setenv("COMDIRECT_HBCI", "https://example.invalid/fints")
    monkeypatch.setenv("COMDIRECT_TAN_MECHANISM", "911")
    monkeypatch.setattr(sync, "FinTS3PinTanClient", FakeClient)
    db = tmp_path / "test.db"
    monkeypatch.setattr(webapp, "DB_PATH", db)
    monkeypatch.setattr(sync, "_jobs", {})
    FakeClient.seen_pins = []
    FakeClient.sent_tans = []
    yield db


@pytest.fixture
def client(env_and_db):
    with TestClient(webapp.create_app()) as c:
        yield c


def wait_for(client, job_id, statuses, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/sync/{job_id}").json()
        if j["status"] in statuses:
            return j
        time.sleep(0.05)
    raise AssertionError(f"Status {statuses} nicht erreicht: {j}")


def start(client, pin=PIN, days=100):
    r = client.post("/api/sync", json={"bank": "comdirect", "pin": pin, "days": days})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def test_tan_flow_end_to_end(client, env_and_db):
    FakeClient.scenario = "tan"
    job_id = start(client)
    j = wait_for(client, job_id, {"tan_required"})
    assert j["prompt"]["type"] == "tan" and j["prompt"]["decoupled"] is False
    assert client.post(f"/api/sync/{job_id}/input", json={"tan": "123456"}).status_code == 200
    # Umsatzabruf verlangt eine zweite TAN
    j = wait_for(client, job_id, {"tan_required"})
    assert "Umsatzabruf" in j["prompt"]["text"]
    client.post(f"/api/sync/{job_id}/input", json={"tan": "654321"})
    j = wait_for(client, job_id, {"done", "error"})
    assert j["status"] == "done", j
    assert j["result"]["accounts"][FakeAccount.iban] == {"new": 2, "skipped": 0, "fetched": 2}
    assert FakeClient.sent_tans == ["123456", "654321"]

    # Umsätze stehen in der DB
    con = sqlite3.connect(env_and_db)
    assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    con.close()

    # zweiter Abruf: Duplikate werden übersprungen
    FakeClient.scenario = "tan"
    job2 = start(client)
    wait_for(client, job2, {"tan_required"})
    client.post(f"/api/sync/{job2}/input", json={"tan": "1"})
    wait_for(client, job2, {"tan_required"})
    client.post(f"/api/sync/{job2}/input", json={"tan": "2"})
    j = wait_for(client, job2, {"done", "error"})
    assert j["result"]["accounts"][FakeAccount.iban]["skipped"] == 2


def test_pin_never_leaks_into_state_or_db(client, env_and_db):
    FakeClient.scenario = "tan"
    job_id = start(client)
    wait_for(client, job_id, {"tan_required"})
    assert PIN not in str(client.get(f"/api/sync/{job_id}").json())
    client.post(f"/api/sync/{job_id}/input", json={"tan": "1"})
    wait_for(client, job_id, {"tan_required"})
    client.post(f"/api/sync/{job_id}/input", json={"tan": "2"})
    wait_for(client, job_id, {"done", "error"})
    assert FakeClient.seen_pins == [PIN]
    assert PIN.encode() not in env_and_db.read_bytes()


def test_wrong_pin_reports_error_without_pin(client):
    FakeClient.scenario = "wrong_pin"
    job_id = start(client, pin="falsch")
    j = wait_for(client, job_id, {"error", "done"})
    assert j["status"] == "error"
    assert "9942" in j["error"]
    assert "falsch" not in str(j)


def test_decoupled_app_confirmation_sends_empty_tan(client):
    FakeClient.scenario = "decoupled"
    job_id = start(client)
    j = wait_for(client, job_id, {"tan_required"})
    assert j["prompt"]["decoupled"] is True
    client.post(f"/api/sync/{job_id}/input", json={})
    wait_for(client, job_id, {"tan_required", "done", "error"})
    assert FakeClient.sent_tans[0] == ""


def test_tan_medium_choice(client):
    FakeClient.scenario = "choice"
    job_id = start(client)
    j = wait_for(client, job_id, {"choice_required"})
    assert [o["label"].startswith("Handy") for o in j["prompt"]["options"]] == [True, True]
    client.post(f"/api/sync/{job_id}/input", json={"index": 1})
    # Nach Medium-Auswahl folgt der Umsatzabruf; ohne TAN-Bedarf wäre er direkt fertig
    j = wait_for(client, job_id, {"tan_required", "done", "error"})
    assert FakeClient.seen_pins == [PIN]


def test_cancel_while_waiting(client):
    FakeClient.scenario = "tan"
    job_id = start(client)
    wait_for(client, job_id, {"tan_required"})
    assert client.post(f"/api/sync/{job_id}/cancel").status_code == 200
    j = wait_for(client, job_id, {"error"})
    assert "abgebrochen" in j["error"]


def test_input_rejected_when_not_waiting(client):
    FakeClient.scenario = "tan"
    job_id = start(client)
    wait_for(client, job_id, {"tan_required"})
    client.post(f"/api/sync/{job_id}/input", json={"tan": "1"})
    wait_for(client, job_id, {"tan_required"})
    client.post(f"/api/sync/{job_id}/input", json={"tan": "2"})
    wait_for(client, job_id, {"done", "error"})
    assert client.post(f"/api/sync/{job_id}/input", json={"tan": "3"}).status_code == 409


def test_only_one_job_at_a_time(client):
    FakeClient.scenario = "tan"
    start(client)
    r = client.post("/api/sync", json={"bank": "comdirect", "pin": PIN, "days": 10})
    assert r.status_code == 409


def test_unconfigured_bank_rejected(client, monkeypatch):
    monkeypatch.delenv("DKB_BLZ", raising=False)
    r = client.post("/api/sync", json={"bank": "dkb", "pin": PIN})
    assert r.status_code == 400
    banks = {b["key"]: b["configured"] for b in client.get("/api/sync/banks").json()}
    assert banks == {"comdirect": True, "dkb": False}


def test_empty_pin_rejected(client):
    r = client.post("/api/sync", json={"bank": "comdirect", "pin": ""})
    assert r.status_code == 400
