"""Renault (Renault, Dacia, Alpine): Auswertung, Anmeldung und Ladeerkennung.

Kein Test geht ins Netz. `FakeRenault` spielt Gigya (Anmeldung, JWT,
Bestaetigung per E-Mail-Code) und Kamereon (Person, Fahrzeuge,
battery-status, cockpit, location, hvac-status) nach und merkt sich jede
Anfrage. Die Antworten sind den Fixtures von renault-api nachgebaut
(tests/fixtures/gigya, tests/fixtures/kamereon) - gegen ein echtes Konto ist
nichts davon geprueft.
"""

import urllib.parse
from datetime import datetime, timedelta

import httpx
import pytest

from app import models, renault, renault_poller
from app.renault import RenaultSnapshot
from conftest import create_vehicle, register

VIN = "VF1AG000000000001"
EMAIL = "fahrer@example.org"
PASSWORD = "richtig"
GIGYA = "accounts.eu1.gigya.com"
KAMEREON = "api-wired-prod-1-euw1.wrd-aws.com"


def _ts(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeRenault(httpx.BaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.password = PASSWORD
        self.require_tfa = False
        self.code = "123456"
        self.token_expired = False
        self.logins = 0
        self.quota = False
        self.unsupported: set[str] = set()
        self.battery: dict = {"timestamp": "2026-10-01T08:00:00Z", "batteryLevel": 50,
                              "plugStatus": 0, "chargingStatus": 0.0}
        self.mileage = 12345.6
        self.position = (48.12, 11.56)
        self.temperature = 9.0
        self.device_trusted = False

    # -- Hilfen fuer die Tests -----------------------------------------------
    def set_battery(self, when, soc, plugged, status=0.0, power=None):
        self.battery = {"timestamp": _ts(when), "batteryLevel": soc,
                        "plugStatus": 1 if plugged else 0, "chargingStatus": status}
        if power is not None:
            self.battery["chargingInstantaneousPower"] = power

    def count(self, part: str) -> int:
        return sum(1 for r in self.requests if part in r.url.path)

    # -- Transport -------------------------------------------------------------
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == GIGYA:
            return self._gigya(request)
        if request.url.host == KAMEREON:
            return self._kamereon(request)
        return httpx.Response(404)

    def _params(self, request) -> dict:
        if request.method == "POST":
            return dict(urllib.parse.parse_qsl(request.content.decode()))
        return dict(urllib.parse.parse_qsl(request.url.query.decode()))

    def _gigya(self, request):
        path = request.url.path.lstrip("/")
        params = self._params(request)
        cookie = request.headers.get("cookie", "")
        if path == "accounts.login":
            assert params["ApiKey"] == renault.GIGYA_KEY_EU
            if params.get("password") != self.password:
                return httpx.Response(200, json={"errorCode": 403042, "errorDetails": "invalid loginID or password"})
            if self.require_tfa and not (self.device_trusted and "gmid=gmid-1" in cookie):
                return httpx.Response(200, json={"errorCode": 403101, "errorDetails": "Pending Two-Factor Authentication",
                                                 "regToken": "reg-1"})
            self.logins += 1
            self.token_expired = False
            return httpx.Response(200, json={"errorCode": 0, "sessionInfo": {"cookieValue": f"login-{self.logins}"}})
        if path == "accounts.getJWT":
            if self.token_expired:
                return httpx.Response(200, json={"errorCode": 403005, "errorDetails": "Unauthorized user"})
            return httpx.Response(200, json={"errorCode": 0, "id_token": f"jwt-{params['login_token']}"})
        if path == "accounts.getAccountInfo":
            return httpx.Response(200, json={"errorCode": 0, "data": {"personId": "person-1"}})
        if path == "accounts.webSdkBootstrap":
            return httpx.Response(200, json={"errorCode": 0},
                                  headers=[("Set-Cookie", "gmid=gmid-1; Path=/"), ("Set-Cookie", "ucid=ucid-1; Path=/")])
        if path == "accounts.tfa.initTFA":
            assert params["regToken"] == "reg-1" and params["gmid"] == "gmid-1"
            return httpx.Response(200, json={"errorCode": 0, "gigyaAssertion": "assert-1"})
        if path == "accounts.tfa.email.getEmails":
            return httpx.Response(200, json={"errorCode": 0, "emails": [{"id": "mail-1", "obfuscated": "f***@example.org"}]})
        if path == "accounts.tfa.email.sendVerificationCode":
            assert params["emailID"] == "mail-1"
            return httpx.Response(200, json={"errorCode": 0, "phvToken": "phv-1"})
        if path == "accounts.tfa.email.completeVerification":
            if params["code"] != self.code:
                return httpx.Response(200, json={"errorCode": 400006, "errorDetails": "Wrong verification code"})
            return httpx.Response(200, json={"errorCode": 0, "providerAssertion": "prov-1"})
        if path == "accounts.tfa.finalizeTFA":
            assert params["tempDevice"] == "false" and params["providerAssertion"] == "prov-1"
            self.device_trusted = True
            return httpx.Response(200, json={"errorCode": 0})
        return httpx.Response(404)

    def _kamereon(self, request):
        assert request.headers["apikey"] == renault.KAMEREON_APIKEY
        assert request.headers["x-gigya-id_token"].startswith("jwt-")
        assert request.url.params["country"] == "DE"
        path = request.url.path.removeprefix("/commerce/v1")
        if path == "/persons/person-1":
            return httpx.Response(200, json={"accounts": [
                {"accountId": "sfdc-1", "accountType": "SFDC"},
                {"accountId": "acc-1", "accountType": "MYRENAULT"},
            ]})
        if path == "/accounts/acc-1/vehicles":
            return httpx.Response(200, json={"vehicleLinks": [{"vin": VIN, "vehicleDetails": {
                "brand": {"label": "RENAULT"}, "model": {"code": "X102VE", "label": "ZOE"}}}]})
        prefix = "/accounts/acc-1/kamereon/kca/car-adapter/"
        if not path.startswith(prefix):
            return httpx.Response(404)
        endpoint = path.rsplit("/", 1)[-1]
        if self.quota:
            return httpx.Response(429, json={"errors": [{"errorCode": "err.func.wired.overloaded",
                                                         "errorMessage": "You have reached your quota limit"}]})
        if endpoint in self.unsupported:
            return httpx.Response(501, json={"errors": [{"errorCode": "err.tech.501", "errorMessage": "not supported"}]})
        attributes = {
            "battery-status": self.battery,
            "cockpit": {"totalMileage": self.mileage},
            "location": {"gpsLatitude": self.position[0], "gpsLongitude": self.position[1]},
            "hvac-status": {"externalTemperature": self.temperature},
        }.get(endpoint)
        if attributes is None:
            return httpx.Response(404)
        if endpoint == "battery-status":
            assert "/v2/" in path
        return httpx.Response(200, json={"data": {"type": "Car", "id": VIN, "attributes": attributes}})


@pytest.fixture
def fake(monkeypatch):
    service = FakeRenault()
    monkeypatch.setattr(renault, "TRANSPORT", service)
    monkeypatch.setattr(renault_poller, "_clients", {})
    monkeypatch.setattr(renault_poller, "_pending", {})
    return service


# ---------------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------------

def test_snapshot_states_and_units():
    snap = RenaultSnapshot({"timestamp": "2020-11-17T09:06:48+01:00", "batteryLevel": 50,
                            "plugStatus": 1, "chargingStatus": 1.0, "chargingInstantaneousPower": 11.0})
    assert snap.is_plugged_in is True
    assert snap.charging_state == "CHARGE_IN_PROGRESS"
    assert snap.charge_type == "AC"
    assert snap.captured_at == datetime(2020, 11, 17, 8, 6, 48)  # naives UTC
    assert RenaultSnapshot({"chargingInstantaneousPower": 50}).charge_type == "DC"
    # ZOE Phase 1 meldet Watt
    zoe1 = RenaultSnapshot({"chargingInstantaneousPower": 3100}, model_code="X101VE")
    assert zoe1.charge_power_kw == pytest.approx(3.1)
    assert RenaultSnapshot({"chargingInstantaneousPower": 7400}).charge_power_kw == pytest.approx(7.4)


def test_snapshot_without_plug_decides_only_on_clear_states():
    # Stecker unbekannt, Zustand "laedt" -> steckt
    assert RenaultSnapshot({"plugStatus": -2147483648, "chargingStatus": 1.0}).is_plugged_in is True
    # Stecker unbekannt, "laedt nicht" -> nichts entscheiden
    unclear = RenaultSnapshot({"chargingStatus": 0.0, "batteryLevel": 40})
    assert unclear.is_plugged_in is None and unclear.charging_state is None
    # Rueckspeisung zaehlt als "nicht verbunden", auch mit Kabel
    assert RenaultSnapshot({"plugStatus": 1, "chargingStatus": -1.4}).is_plugged_in is False
    # Nur Stecker, kein Zustand
    assert RenaultSnapshot({"plugStatus": 0}).charging_state == "UNPLUGGED"
    # Unplausibles faellt weg
    odd = RenaultSnapshot({"batteryLevel": 140}, cockpit={"totalMileage": 0},
                          location={"gpsLatitude": 0, "gpsLongitude": 0}, hvac={"externalTemperature": 99})
    assert (odd.soc_percent, odd.odometer_km, odd.coordinates, odd.outside_temp_c) == (None, None, None, None)


# ---------------------------------------------------------------------------
# Anmeldung
# ---------------------------------------------------------------------------

def _client(**kwargs) -> renault.RenaultClient:
    return renault.RenaultClient(EMAIL, PASSWORD, locale="de_DE", **kwargs)


def test_login_finds_myrenault_account_and_vehicles(fake):
    client = _client()
    vehicles = client.list_vehicles()
    assert vehicles == [{"vin": VIN, "brand": "RENAULT", "model": "ZOE", "model_code": "X102VE"}]
    assert client.account_id == "acc-1"
    assert client.login_token == "login-1"
    # JWT wird wiederverwendet
    client.battery_status(VIN)
    assert fake.count("accounts.getJWT") == 1


def test_wrong_password_is_an_auth_error(fake):
    fake.password = "anders"
    with pytest.raises(renault.RenaultAuthError, match="Passwort"):
        _client().login()


def test_expired_login_token_logs_in_again_with_password(fake):
    client = _client(login_token="alt", account_id="acc-1")
    fake.token_expired = True
    client.battery_status(VIN)
    assert fake.logins == 1
    assert client.login_token == "login-1"


def test_two_factor_flow(fake):
    fake.require_tfa = True
    client = _client()
    with pytest.raises(renault.RenaultTwoFactorRequired) as info:
        client.login()
    assert "reg-1" not in str(info.value)
    pending = client.start_two_factor(info.value.reg_token)
    assert pending.email_hint == "f***@example.org"
    assert client.device_ids == {"gmid": "gmid-1", "ucid": "ucid-1"}
    with pytest.raises(renault.RenaultInvalidCode):
        client.complete_two_factor(pending, "000000")
    client.complete_two_factor(pending, "123456")
    assert client.login_token == "login-1"

    # Mit gemerkter Geraetekennung keine neue Abfrage
    again = _client(device_ids=client.device_ids)
    again.login()
    assert again.login_token == "login-2"


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

def _config(db, client, **kwargs) -> models.RenaultConfig:
    vehicle = create_vehicle(client, external_id="zoe", name="ZOE", capacity=52.0)
    owner_id = db.get(models.Vehicle, vehicle["id"]).user_id
    config = models.RenaultConfig(
        user_id=owner_id, vehicle_id=vehicle["id"], enabled=True,
        email=EMAIL, password=PASSWORD, vin=VIN, **kwargs,
    )
    db.add(config)
    db.commit()
    return config


def test_charging_session_is_detected_with_few_requests(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    t0 = datetime(2026, 10, 1, 17, 0)
    steps = [
        # (Minuten, SoC, steckt, Zustand, Leistung)
        (0, 30, False, 0.0, None),
        (15, 31, True, 1.0, 11.0),
        (20, 34, True, 1.0, 11.0),
        (25, 37, True, 1.0, 11.0),
        (30, 37, True, 0.2, 0.0),
        (35, 37, False, 0.0, None),
        (50, 37, False, 0.0, None),
    ]
    for minutes, soc, plugged, status, power in steps:
        fake.set_battery(t0 + timedelta(minutes=minutes), soc, plugged, status, power)
        renault_poller.poll_vehicle(db_session, config)
        assert config.last_status == "ok", config.last_error
        if plugged:
            assert config.next_poll_at < datetime.utcnow() + timedelta(minutes=6)

    sessions = db_session.query(models.ChargingSession).all()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.external_session_id.startswith(f"renault-{config.vehicle_id}-")
    assert VIN not in session.external_session_id
    assert session.source == models.SessionSource.AUTOMATIC and session.needs_review
    assert (session.soc_start, session.soc_end) == (30, 37)  # zurueckdatiert auf den Abruf davor
    assert session.charging_type == models.ChargingType.AC
    assert session.odometer_km == 12346
    assert session.outside_temp_c == 9.0
    assert (session.latitude, session.longitude) == (48.12, 11.56)
    assert session.energy_is_estimated is True
    assert "Renault" in session.notes
    # Zusatzabrufe nur beim ersten Abruf, beim Einstecken und beim Ausstecken
    assert fake.count("/battery-status") == len(steps)
    assert fake.count("/cockpit") == 3
    assert fake.count("/location") == 3
    # Login-Token und Kontokennung liegen in der Zeile
    assert config.login_token == "login-1" and config.account_id == "acc-1"
    assert fake.logins == 1


def test_ac_start_is_upgraded_to_dc(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    t0 = datetime(2026, 10, 1, 12, 0)
    for minutes, soc, plugged, status, power in [
        (0, 20, False, 0.0, None), (5, 22, True, 1.0, 8.0), (10, 40, True, 1.0, 95.0), (15, 60, False, 0.0, None),
    ]:
        fake.set_battery(t0 + timedelta(minutes=minutes), soc, plugged, status, power)
        renault_poller.poll_vehicle(db_session, config)
    session = db_session.query(models.ChargingSession).one()
    assert session.charging_type == models.ChargingType.DC


def test_missing_endpoints_only_drop_their_values(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    fake.unsupported = {"location", "hvac-status"}
    t0 = datetime(2026, 10, 1, 8, 0)
    for minutes, soc, plugged, status, power in [(0, 50, False, 0.0, None), (10, 52, True, 1.0, 7.0),
                                                 (20, 60, False, 0.0, None)]:
        fake.set_battery(t0 + timedelta(minutes=minutes), soc, plugged, status, power)
        renault_poller.poll_vehicle(db_session, config)
        assert config.last_status == "ok", config.last_error
    session = db_session.query(models.ChargingSession).one()
    assert session.latitude is None and session.outside_temp_c is None
    assert session.odometer_km == 12346


def test_quota_limit_pauses_fifteen_minutes(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    fake.quota = True
    renault_poller.poll_vehicle(db_session, config)
    assert config.last_status == "rate_limited"
    assert config.next_poll_at > datetime.utcnow() + timedelta(minutes=14)


def test_tfa_needed_in_background_sets_status_and_backs_off(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    fake.require_tfa = True
    renault_poller.poll_vehicle(db_session, config)
    assert config.last_status == "tfa_required"
    assert config.next_poll_at > datetime.utcnow() + timedelta(minutes=55)
    # Im Hintergrund wird nie ein Code verschickt
    assert fake.count("sendVerificationCode") == 0


def test_wrong_password_in_background_is_auth_error(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    fake.password = "geaendert"
    renault_poller.poll_vehicle(db_session, config)
    assert config.last_status == "auth_error"
    log = db_session.query(models.RenaultLogEntry).filter_by(event="api_error").one()
    assert "Passwort" in log.message


# ---------------------------------------------------------------------------
# Schnittstelle
# ---------------------------------------------------------------------------

def test_api_login_with_code_then_test_fills_vin(client, db_session, fake):
    register(client)
    vehicle = create_vehicle(client, external_id="zoe", name="ZOE")
    url = f"/api/renault/configs/{vehicle['id']}"
    res = client.put(url, json={"email": EMAIL, "password": PASSWORD})
    assert res.status_code == 200, res.text
    assert res.json()["has_password"] is True and PASSWORD not in res.text

    fake.require_tfa = True
    res = client.post(f"{url}/login")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "code_sent"
    assert res.json()["email_hint"] == "f***@example.org"

    res = client.post(f"{url}/login/code", json={"code": "999999"})
    assert res.json()["status"] == "code_sent" and "falsch" in res.json()["error"]
    res = client.post(f"{url}/login/code", json={"code": "123456"})
    body = res.json()
    assert body["status"] == "connected", body
    assert body["config"]["connected"] is True
    assert "login-1" not in res.text

    res = client.post(f"{url}/test")
    result = res.json()
    assert result["ok"] is True, result
    assert result["config"]["vin"] == VIN
    assert result["config"]["vehicle_model"] == "RENAULT ZOE"
    assert result["summary"]["soc_percent"] == 50
    assert result["config"]["last_soc"] is None  # Test legt keinen Zustand an

    # Die gemerkte Geraetekennung erspart nach einem Neustart den Code
    config = db_session.query(models.RenaultConfig).one()
    assert "gmid-1" in config.device_ids
    renault_poller._clients.clear()
    config.login_token = None
    db_session.commit()
    res = client.post(f"{url}/login")
    assert res.json()["status"] == "connected"


def test_enabling_requires_credentials_and_rejects_second_source(client, db_session, fake):
    register(client)
    vehicle = create_vehicle(client, external_id="zoe", name="ZOE")
    url = f"/api/renault/configs/{vehicle['id']}"
    assert client.put(url, json={"enabled": True}).status_code == 422
    assert client.put(url, json={"locale": "xx_XX"}).status_code == 422
    assert client.put(url, json={"vin": "ZU-KURZ"}).status_code == 422

    owner_id = db_session.get(models.Vehicle, vehicle["id"]).user_id
    db_session.add(models.BmwCarDataConfig(user_id=owner_id, vehicle_id=vehicle["id"], enabled=True))
    db_session.commit()
    res = client.put(url, json={"enabled": True, "email": EMAIL, "password": PASSWORD, "vin": VIN})
    assert res.status_code == 422
    assert "BMW CarData" in res.text


def test_changing_the_account_drops_the_login_token(client, db_session, fake):
    register(client)
    config = _config(db_session, client, login_token="login-x", account_id="acc-1")
    url = f"/api/renault/configs/{config.vehicle_id}"
    assert client.put(url, json={"email": EMAIL}).json()["connected"] is True
    assert client.put(url, json={"email": "neu@example.org"}).json()["connected"] is False


def test_foreign_vehicle_is_404(client):
    register(client, "alice")
    vehicle = create_vehicle(client)
    register(client, "bob")
    assert client.get(f"/api/renault/configs/{vehicle['id']}").status_code == 404


def test_deleting_vehicle_and_user_remove_config(client, db_session, fake):
    register(client, "admin")
    admin_headers = dict(client.headers)
    register(client, "bob")
    config = _config(db_session, client)
    renault_poller.log_event(db_session, config, "test", "x", force=True)
    db_session.commit()
    bob_id = config.user_id

    client.headers.clear()
    client.headers.update(admin_headers)
    assert client.delete(f"/api/auth/users/{bob_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.RenaultConfig).count() == 0
    assert db_session.query(models.RenaultLogEntry).count() == 0


def test_deleting_vehicle_removes_config(client, db_session, fake):
    register(client)
    config = _config(db_session, client)
    assert client.delete(f"/api/vehicles/{config.vehicle_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.RenaultConfig).count() == 0
