"""VW-Datenportal (EU Data Act): Auswertung, Anmeldung und Ladeerkennung.

Kein Test geht ins Netz. `FakePortal` spielt Identity-Dienst und Portal nach
(Login ueber Formulare und Weiterleitungen, Fahrzeugliste, Datenanfrage,
ZIP-Liste, Download) und merkt sich jede Anfrage. Die Formate sind den
Beispielen aus HA_VAG-EU-Data-Act und carconnectivity nachgebaut - gegen einen
echten Account ist nichts davon geprueft.
"""

import io
import json
import urllib.parse
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest

from app import models, vw_data_act, vw_data_act_poller
from app.vw_data_act import DataActSnapshot, Dataset
from conftest import create_vehicle, register

VIN = "VSSZZZK1ZRP012345"
IDENTITY = "https://identity.vwgroup.io"
PORTAL = "https://eu-data-act.drivesomethinggreater.com"
EMAIL = "fahrer@example.org"
PASSWORD = "richtig"


# ---------------------------------------------------------------------------
# Datensaetze bauen
# ---------------------------------------------------------------------------

def _item(name, value, key=None, ts=None):
    item = {"key": key or f"k-{name}", "dataFieldName": name, "value": str(value)}
    if ts is not None:
        item["timestampUtc"] = ts
    return item


def dataset(
    captured: datetime,
    *,
    soc=50,
    state="CHARGE_STATE_NOT_READY_FOR_CHARGING",
    plug="PLUG_CONNECTION_STATE_DISCONNECTED",
    power=0.0,
    charge_type="CHARGE_TYPE_OFF",
    energy=None,
    mileage=10000,
    temp_dk=None,
) -> dict:
    data = [
        _item("car_captured_time", captured.strftime("%Y-%m-%dT%H:%M:%SZ"), key="k-cct"),
        _item("battery_state_report.soc", soc),
        _item("battery_state_report.charge_power", power),
        _item("mileage.value", mileage),
        _item("mileage.unit", 0),
    ]
    if state is not None:
        data.append(_item("charging_state_report.current_charge_state", state))
    if charge_type is not None:
        data.append(_item("charging_state_report.charge_type", charge_type))
    if plug is not None:
        data.append(_item("plug_states.0.plug_connection_state", plug))
    if energy is not None:
        data.append(_item("battery_state_report.charge_energy", energy))
    if temp_dk is not None:
        data.append(_item("outside_temperature", temp_dk))
    return {"vin": VIN, "user_id": "u", "Data": data}


def zip_bytes(payload: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", json.dumps(payload))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

def test_snapshot_reads_dotted_fields_and_converts_units():
    snap = DataActSnapshot.from_json(
        dataset(
            datetime(2026, 10, 1, 8, 0),
            soc=42,
            state="CHARGE_STATE_CHARGING_HV_BATTERY",
            plug="PLUG_CONNECTION_STATE_CONNECTED",
            power=10.8,
            charge_type="CHARGE_TYPE_AC",
            energy=3.5,
            temp_dk=2831,
        )
    )
    assert snap.charging_state == "CHARGING_HV_BATTERY"
    assert snap.is_plugged_in is True
    assert snap.soc_percent == 42
    assert snap.charge_power_kw == 10.8
    assert snap.charge_type == "AC"
    assert snap.charged_energy_kwh == 3.5
    assert snap.odometer_km == 10000
    assert snap.outside_temp_c == 10.0  # 2831 dK = 283,1 K
    assert snap.captured_at == datetime(2026, 10, 1, 8, 0)
    assert snap.coordinates is None


def test_enum_index_and_miles_are_resolved():
    payload = dataset(datetime(2026, 10, 1, 8, 0), state="2", plug="1", charge_type="3", mileage=100)
    payload["Data"] = [d for d in payload["Data"] if d["dataFieldName"] != "mileage.unit"]
    payload["Data"].append(_item("mileage.unit", 1))
    snap = DataActSnapshot.from_json(payload)
    assert snap.charging_state == "CHARGING_HV_BATTERY"
    assert snap.is_plugged_in is True
    assert snap.charge_type == "DC"
    assert snap.odometer_km == 161


def test_report_copy_never_replaces_live_soc_and_freshest_wins():
    payload = dataset(datetime(2026, 10, 1, 8, 0), soc=76)
    # SoC-Kopie eines Ladeauftrags (= SoC bei Ladebeginn) - darf nie zaehlen.
    payload["Data"].append(
        _item("battery_state_report.soc", 34, key="93b55324-6628-36df-8f76-8eba797fc59c")
    )
    # Zwei Leistungswerte: der juengere gewinnt.
    payload["Data"].append(_item("battery_state_report.charge_power", 7.0, key="a", ts="2026-10-01T07:00:00Z"))
    payload["Data"].append(_item("battery_state_report.charge_power", 9.0, key="b", ts="2026-10-01T07:30:00Z"))
    payload["Data"] = [
        d for d in payload["Data"]
        if not (d["dataFieldName"] == "battery_state_report.charge_power" and "timestampUtc" not in d)
    ]
    snap = DataActSnapshot.from_json(payload)
    assert snap.soc_percent == 76
    assert snap.charge_power_kw == 9.0


def test_flat_format_with_deci_units():
    payload = {
        "vin": VIN,
        "Data": [
            _item("state_of_charge", 61, ts="2026-10-02T10:00:00Z"),
            _item("charging_state", "charging", ts="2026-10-02T10:00:00Z"),
            _item("charging_power", 99, ts="2026-10-02T10:00:00Z"),
            _item("charged_energy", 143, ts="2026-10-02T09:55:00Z"),
            _item("mileage", 5432, ts="2026-10-02T10:00:00Z"),
            _item("charging_plug1_connectionstate", "connected", ts="2026-10-02T10:00:00Z"),
            _item("outsideTemperatureIndication", "12.5"),
        ],
    }
    snap = DataActSnapshot.from_json(payload)
    assert snap.charging_state == "CHARGING"
    assert snap.is_plugged_in is True
    assert snap.soc_percent == 61
    assert snap.charge_power_kw == 9.9
    assert snap.charged_energy_kwh == 14.3
    assert snap.odometer_km == 5432
    assert snap.outside_temp_c == 12.5
    assert snap.captured_at == datetime(2026, 10, 2, 10, 0)


def test_without_plug_or_state_nothing_is_decided():
    payload = dataset(datetime(2026, 10, 1, 8, 0), state=None, plug=None)
    snap = DataActSnapshot.from_json(payload)
    assert snap.is_plugged_in is None
    assert snap.charging_state is None


def test_state_alone_decides_when_plug_is_missing():
    plugged = DataActSnapshot.from_json(
        dataset(datetime(2026, 10, 1, 8), state="CHARGE_STATE_READY_FOR_CHARGING", plug=None)
    )
    unplugged = DataActSnapshot.from_json(dataset(datetime(2026, 10, 1, 8), plug=None))
    assert plugged.is_plugged_in is True
    assert unplugged.is_plugged_in is False


def test_sentinels_and_implausible_values_are_dropped():
    payload = dataset(datetime(2026, 10, 1, 8), soc=65535, power=65535, temp_dk=65535)
    snap = DataActSnapshot.from_json(payload)
    assert snap.soc_percent is None
    assert snap.charge_power_kw is None
    assert snap.outside_temp_c is None


def test_dataset_without_car_captured_time_uses_newest_field_timestamp():
    ds = Dataset.from_json({"Data": [
        _item("a", 1, ts="2026-10-01T07:00:00Z"),
        _item("b", 2, ts="2026-10-01T09:00:00+02:00"),
    ]})
    assert ds.captured_at == datetime(2026, 10, 1, 7, 0)


# ---------------------------------------------------------------------------
# Nachgebautes Portal
# ---------------------------------------------------------------------------

SIGNIN_PAGE = """<html><body>
<form method="post" action="/signin-service/v1/{client}/login/identifier">
<input type="hidden" name="_csrf" value="csrf-1">
<input type="hidden" name="relayState" value="relay-1">
<input type="hidden" name="hmac" value="hmac-1">
<input type="email" name="email">
</form></body></html>"""

PASSWORD_PAGE = """<html><head><script>
window._IDK = { templateModel: {"hmac":"hmac-2","relayState":"relay-2",
"emailPasswordForm":{"email":"%s"},"postAction":"login/authenticate"},
csrf_token: 'csrf-2' };
</script></head><body></body></html>"""


class FakePortal(httpx.BaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.password = PASSWORD
        self.after_login = "callback"  # callback | terms | marketing
        self.metadata_status = 200
        self.identifier = "req-1"
        self.files: list[dict] = []
        self.zips: dict[str, bytes] = {}
        self.list_status = 200
        self.expire_session_once = False
        self.logins = 0

    # Hilfen fuer die Tests
    def add(self, name: str, created: datetime, payload: dict | None):
        self.files.append({"name": name, "createdOn": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "size": 1})
        if payload is not None:
            self.zips[name] = zip_bytes(payload)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        host = url.host
        path = url.path
        if host == "identity.vwgroup.io":
            return self._identity(request, path)
        if host == "eu-data-act.drivesomethinggreater.com":
            return self._portal(request, path)
        return httpx.Response(404)

    def _identity(self, request, path):
        client_id = urllib.parse.parse_qs(request.url.query.decode()).get("client_id", ["?"])[0]
        if path == "/oidc/v1/authorize":
            return httpx.Response(
                302, headers={"Location": f"{IDENTITY}/signin-service/v1/{client_id}/login?relayState=relay-1"}
            )
        if path.endswith("/login") and request.method == "GET":
            client = path.split("/")[3]
            return httpx.Response(200, html=SIGNIN_PAGE.format(client=client))
        if path.endswith("/login/identifier"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert form["hmac"] == "hmac-1" and form["_csrf"] == "csrf-1"
            base = path.rsplit("/", 1)[0]
            return httpx.Response(303, headers={"Location": f"{base}/authenticate?relayState=relay-2"})
        if path.endswith("/login/authenticate") and request.method == "GET":
            if "error" in str(request.url):
                return httpx.Response(200, html="<html>falsch</html>")
            assert request.url.query  # Seite mit relayState im Query
            return httpx.Response(200, html=PASSWORD_PAGE % EMAIL)
        if path.endswith("/login/authenticate") and request.method == "POST":
            assert not request.url.query, "relayState darf nicht doppelt im Query stehen"
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert form["hmac"] == "hmac-2" and form["_csrf"] == "csrf-2"
            if form.get("password") != self.password:
                return httpx.Response(303, headers={"Location": f"{path}?error=login.errors.password_invalid"})
            if self.after_login == "terms":
                return httpx.Response(302, headers={
                    "Location": f"{IDENTITY}/signin-service/v1/x/terms-and-conditions?updated=dataprivacy"})
            if self.after_login == "marketing":
                callback = f"{IDENTITY}/oidc/v1/oauth/client/callback?scopes=openid cars&x=1"
                return httpx.Response(302, headers={
                    "Location": f"{IDENTITY}/signin-service/v1/consent/marketing/uid/cid/0?"
                    + urllib.parse.urlencode({"callback": callback})})
            return httpx.Response(302, headers={"Location": f"{IDENTITY}/oidc/v1/oauth/client/callback?c=1"})
        if path.startswith("/signin-service/v1/x/terms-and-conditions"):
            return httpx.Response(200, html="<html>AGB</html>")
        if path == "/oidc/v1/oauth/client/callback":
            return httpx.Response(302, headers={"Location": f"{PORTAL}/services/callbacklogin?code=abc"})
        return httpx.Response(404)

    def _portal(self, request, path):
        if path == "/services/callbacklogin":
            self.logins += 1
            return httpx.Response(
                302,
                headers={
                    "Location": f"{PORTAL}/de/de/home.html",
                    "Set-Cookie": f"access_token=tok-{self.logins}; Path=/; Secure; HttpOnly",
                },
            )
        if path == "/de/de/home.html":
            raise AssertionError("Die CMS-Seite nach dem Callback darf nicht geladen werden")
        cookie = request.headers.get("cookie", "")
        if "access_token=" not in cookie:
            return httpx.Response(401)
        if self.expire_session_once:
            self.expire_session_once = False
            return httpx.Response(401)
        if path == "/proxy_api/consent/me/vehicles":
            return httpx.Response(200, json={"vehicles": [{"vin": VIN, "vehicleNickname": "Tavascan"}]})
        if path == f"/proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial":
            if self.metadata_status != 200:
                return httpx.Response(self.metadata_status)
            return httpx.Response(200, json={"Identifier": self.identifier})
        if path == f"/proxy_api/euda-apim/datadelivery/vehicles/{VIN}/{self.identifier}/list":
            assert request.headers.get("type") == "partial"
            if self.list_status != 200:
                return httpx.Response(self.list_status, headers={"Retry-After": "120"})
            return httpx.Response(200, json=self.files)
        if path == f"/proxy_api/euda-apim/datadelivery/vehicles/{VIN}/{self.identifier}/download":
            name = request.headers.get("filename")
            if name not in self.zips:
                return httpx.Response(404)
            return httpx.Response(200, content=self.zips[name])
        return httpx.Response(404)

    def count(self, path_part: str) -> int:
        return sum(1 for r in self.requests if path_part in r.url.path)


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setattr(vw_data_act, "TRANSPORT", fake)
    monkeypatch.setattr(vw_data_act_poller, "_clients", {})
    return fake


def _client() -> vw_data_act.DataActClient:
    return vw_data_act.DataActClient(EMAIL, PASSWORD, brand="CUPRA", country="de", language="de")


# ---------------------------------------------------------------------------
# Anmeldung und Abruf
# ---------------------------------------------------------------------------

def test_login_flow_uses_brand_client_and_stops_at_callback(portal):
    client = _client()
    client.login()
    assert client.logged_in
    authorize = next(r for r in portal.requests if r.url.path == "/oidc/v1/authorize")
    params = urllib.parse.parse_qs(authorize.url.query.decode())
    assert params["client_id"] == [vw_data_act.BRANDS["CUPRA"].client_id]
    assert params["state"] == ["de__de__CUPRA"]
    assert params["prompt"] == ["login"]
    assert authorize.headers["user-agent"].startswith("lademonitor-server/")
    assert authorize.headers["accept-language"].startswith("de-DE")
    # Keine Portal-Startseite vorab.
    assert portal.requests[0].url.host == "identity.vwgroup.io"
    assert client.list_vehicles() == [{"vin": VIN, "nickname": "Tavascan"}]


def test_wrong_password_is_an_auth_error(portal):
    portal.password = "anders"
    with pytest.raises(vw_data_act.DataActAuthError, match="Passwort"):
        _client().login()


def test_terms_page_is_never_accepted(portal):
    portal.after_login = "terms"
    with pytest.raises(vw_data_act.DataActAuthError, match="Nutzungsbedingungen"):
        _client().login()
    assert not any(r.method == "POST" and "terms" in r.url.path for r in portal.requests)


def test_marketing_consent_is_skipped_without_answering(portal):
    portal.after_login = "marketing"
    client = _client()
    client.login()
    assert client.logged_in
    assert not any("consent/marketing" in r.url.path for r in portal.requests)


def test_expired_session_logs_in_again_once(portal):
    client = _client()
    client.login()
    portal.expire_session_once = True
    assert client.get_identifier(VIN) == "req-1"
    assert portal.logins == 2


def test_missing_request_is_reported_as_such(portal):
    portal.metadata_status = 404
    client = _client()
    with pytest.raises(vw_data_act.DataActNotFoundError, match="Datenanfrage"):
        client.get_identifier(VIN)


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

def _config(db, client, **kwargs) -> models.VwDataActConfig:
    vehicle = create_vehicle(client, external_id="tavascan", name="Tavascan", capacity=77.0)
    owner_id = db.get(models.Vehicle, vehicle["id"]).user_id
    config = models.VwDataActConfig(
        user_id=owner_id,
        vehicle_id=vehicle["id"],
        enabled=True,
        brand="CUPRA",
        email=EMAIL,
        password=PASSWORD,
        vin=VIN,
        **kwargs,
    )
    db.add(config)
    db.commit()
    return config


def test_first_poll_only_reads_the_newest_dataset(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    t0 = datetime(2026, 10, 1, 8, 0)
    for i in range(3):
        portal.add(f"ds-{i}.zip", t0 + timedelta(minutes=15 * i), dataset(t0 + timedelta(minutes=15 * i), soc=50 + i))

    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.last_status == "ok", config.last_error
    assert portal.count("/download") == 1
    assert config.last_dataset_name == "ds-2.zip"
    assert config.last_soc == 52
    assert config.identifier == "req-1"


def test_charging_session_with_measured_energy(client, db_session, portal):
    register(client)
    t0 = datetime(2026, 10, 1, 17, 0)
    config = _config(db_session, client, last_dataset_name="ds-0.zip", last_dataset_created=t0)
    steps = [
        # (Minuten, SoC, Stecker, Zustand, Leistung, Zaehler)
        (15, 30, "CONNECTED", "CHARGE_STATE_CHARGING_HV_BATTERY", 11.0, 0.5),
        (30, 33, "CONNECTED", "CHARGE_STATE_CHARGING_HV_BATTERY", 11.0, 3.2),
        (45, 36, "CONNECTED", "CHARGE_STATE_CHARGE_PURPOSE_REACHED_AND_CONSERVATION", 0.0, 5.9),
        (60, 36, "DISCONNECTED", "CHARGE_STATE_NOT_READY_FOR_CHARGING", 0.0, 5.9),
    ]
    portal.add("ds-0.zip", t0, dataset(t0, soc=30))
    for i, (minutes, soc, plug, state, power, energy) in enumerate(steps, start=1):
        when = t0 + timedelta(minutes=minutes)
        portal.add(f"ds-{i}.zip", when, dataset(
            when, soc=soc, plug=f"PLUG_CONNECTION_STATE_{plug}", state=state, power=power,
            charge_type="CHARGE_TYPE_AC" if power else "CHARGE_TYPE_OFF", energy=energy,
            temp_dk=2881,
        ))
    # Leerer Abschnitt zwischendurch - wird uebersprungen, nicht geladen.
    portal.add("x_no_content_found.zip", t0 + timedelta(minutes=50), None)

    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.last_status == "ok", config.last_error
    sessions = db_session.query(models.ChargingSession).all()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.source == models.SessionSource.AUTOMATIC and session.needs_review
    assert session.external_session_id.startswith(f"vwda-{config.vehicle_id}-")
    assert VIN not in session.external_session_id
    # Vor dem Einstecken kein Zaehlerstand: der Zaehler begann bei null, die
    # 0,5 kWh bis zum ersten Bericht zaehlen mit.
    assert session.energy_kwh == pytest.approx(5.9)
    assert session.energy_is_estimated is False
    assert session.energy_meter == "vehicle"  # ohne Anbieter eine Ausnahme von "Ladesaeule"
    assert session.charging_type == models.ChargingType.AC
    assert (session.soc_start, session.soc_end) == (30, 36)
    assert session.outside_temp_c == 15.0
    assert session.latitude is None
    assert portal.count("/download") == 4
    assert config.last_dataset_name == "ds-4.zip"
    assert config.open_start_time is None

    # Zweiter Abruf: nichts Neues, nichts doppelt.
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert db_session.query(models.ChargingSession).count() == 1
    assert portal.count("/download") == 4


def test_new_identifier_is_picked_up_when_list_is_empty(client, db_session, portal):
    register(client)
    config = _config(db_session, client, identifier="alt")
    portal.add("ds-1.zip", datetime(2026, 10, 1, 8), dataset(datetime(2026, 10, 1, 8)))
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.identifier == "req-1"
    assert config.last_status == "ok"


def test_auth_error_backs_off_an_hour(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    portal.password = "geaendert"
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.last_status == "auth_error"
    assert config.next_poll_at > datetime.utcnow() + timedelta(minutes=55)
    log = db_session.query(models.VwDataActLogEntry).filter_by(event="api_error").one()
    assert "Passwort" in log.message


def test_missing_request_sets_no_request(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    portal.metadata_status = 404
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.last_status == "no_request"


def test_rate_limit_respects_retry_after(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    portal.list_status = 429
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert config.last_status == "rate_limited"
    assert config.next_poll_at < datetime.utcnow() + timedelta(minutes=3)


def test_session_cookie_is_reused_between_polls(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    vw_data_act_poller.poll_vehicle(db_session, config)
    vw_data_act_poller.poll_vehicle(db_session, config)
    assert portal.logins == 1


# ---------------------------------------------------------------------------
# Schnittstelle
# ---------------------------------------------------------------------------

def test_api_never_returns_password_and_test_fills_vin(client, db_session, portal):
    register(client)
    vehicle = create_vehicle(client, external_id="tavascan", name="Tavascan")
    url = f"/api/vw-data-act/configs/{vehicle['id']}"
    res = client.put(url, json={"brand": "CUPRA", "email": EMAIL, "password": PASSWORD})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_password"] is True
    assert PASSWORD not in res.text

    portal.add("ds-1.zip", datetime(2026, 10, 1, 8), dataset(datetime(2026, 10, 1, 8), soc=44))
    res = client.post(f"{url}/test")
    assert res.status_code == 200, res.text
    result = res.json()
    assert result["ok"] is True, result
    assert result["config"]["vin"] == VIN
    assert result["summary"]["soc_percent"] == 44
    # Der Test legt keinen Zustand an.
    assert result["config"]["last_soc"] is None


def test_enabling_requires_credentials_and_rejects_second_source(client, db_session, portal):
    register(client)
    vehicle = create_vehicle(client, external_id="enyaq", name="Enyaq")
    url = f"/api/vw-data-act/configs/{vehicle['id']}"
    assert client.put(url, json={"enabled": True, "brand": "SKODA"}).status_code == 422
    assert client.put(url, json={"brand": "PORSCHE"}).status_code == 422

    owner_id = db_session.get(models.Vehicle, vehicle["id"]).user_id
    db_session.add(models.MySkodaConfig(user_id=owner_id, vehicle_id=vehicle["id"], enabled=True))
    db_session.commit()
    res = client.put(url, json={"enabled": True, "brand": "SKODA", "email": EMAIL,
                                "password": PASSWORD, "vin": VIN})
    assert res.status_code == 422
    assert "MyŠkoda" in res.text


def test_foreign_vehicle_is_404(client):
    register(client, "alice")
    vehicle = create_vehicle(client)
    register(client, "bob")
    assert client.get(f"/api/vw-data-act/configs/{vehicle['id']}").status_code == 404


def test_deleting_vehicle_and_user_remove_config(client, db_session, portal):
    register(client, "admin")
    admin_headers = dict(client.headers)
    register(client, "bob")
    config = _config(db_session, client)
    vw_data_act_poller.log_event(db_session, config, "test", "x", force=True)
    db_session.commit()
    bob_id = config.user_id

    client.headers.clear()
    client.headers.update(admin_headers)
    assert client.delete(f"/api/auth/users/{bob_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.VwDataActConfig).count() == 0
    assert db_session.query(models.VwDataActLogEntry).count() == 0


def test_deleting_vehicle_removes_config(client, db_session, portal):
    register(client)
    config = _config(db_session, client)
    vehicle_id = config.vehicle_id
    assert client.delete(f"/api/vehicles/{vehicle_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.VwDataActConfig).count() == 0
