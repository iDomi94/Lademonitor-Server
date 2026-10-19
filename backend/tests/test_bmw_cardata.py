"""BMW CarData: Anmeldung, Auswertung der Ladehistorie und Uebernahme.

Kein Test hier geht ins Netz. `FakeBmw` spielt Anmeldedienst und Kunden-API
nach und merkt sich jede Anfrage - geprueft wird damit, was den Server
verlaesst (PKCE, Fenster), was aus einem Eintrag wird (Lade-Art, Meilen,
Zeitzone, gemessene Energie) und wie sparsam der Poller mit dem
Tageskontingent umgeht.
"""

import types
import urllib.parse
from datetime import datetime, timedelta

import httpx
import pytest

from app import bmw_cardata, bmw_cardata_poller, models
from conftest import create_vehicle, register

VIN = "WBA11AA0X0AB12345"


def _epoch(when: datetime) -> int:
    """Naive LOKALZEIT -> Epoch-Sekunden (Gegenstueck zu bmw_cardata._timestamp)."""
    return int(when.timestamp())


def history_item(
    start: datetime,
    *,
    minutes: int = 120,
    soc=(30, 80),
    energy: float | None = 40.0,
    mileage: float = 12345,
    units: str = "KM",
    lat: float = 48.8,
    lon: float = 9.1,
    block_kw: float | None = 11.0,
    providers: list[str] | None = None,
) -> dict:
    item = {
        "startTime": _epoch(start),
        "endTime": _epoch(start + timedelta(minutes=minutes)),
        "displayedStartSoc": soc[0],
        "displayedSoc": soc[1],
        "mileage": mileage,
        "mileageUnits": f"MileageUnits.{units}",
        "chargingLocation": {
            "mapMatchedLatitude": lat,
            "mapMatchedLongitude": lon,
            "formattedAddress": "Musterstrasse 1, Leonberg",
        },
        "chargingBlocks": [],
        "totalChargingDurationSec": minutes * 60,
    }
    if energy is not None:
        item["energyConsumedFromPowerGridKwh"] = energy
    if block_kw is not None:
        item["chargingBlocks"] = [{"averagePowerGridKw": block_kw}]
    if providers:
        item["publicChargingPoint"] = {
            "potentialChargingPointMatches": [{"providerName": p} for p in providers]
        }
    return item


class FakeBmw(httpx.BaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_responses: list[tuple[int, dict]] = []
        self.refresh_status = 200
        self.mappings = [{"vin": VIN, "mappingType": "PRIMARY"}]
        # Liste von Seiten; jede Seite ist (data, next_token)
        self.pages: list[tuple[list[dict], str | None]] = [([], None)]
        self.history_status = 200
        self.history_body: dict | None = None

    def form(self, request: httpx.Request) -> dict:
        return dict(urllib.parse.parse_qsl(request.content.decode()))

    def history_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/chargingHistory")]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://customer.bmwgroup.com/oneid/link",
                    "verification_uri_complete": "https://customer.bmwgroup.com/oneid/link?code=ABCD-EFGH",
                    "expires_in": 300,
                    "interval": 5,
                },
            )
        if path.endswith("/oauth/token"):
            form = self.form(request)
            if form.get("grant_type") == "refresh_token":
                if self.refresh_status != 200:
                    return httpx.Response(self.refresh_status, json={"error": "invalid_grant"})
                return httpx.Response(
                    200,
                    json={"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600},
                )
            status, body = (
                self.token_responses.pop(0)
                if self.token_responses
                else (200, {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600})
            )
            return httpx.Response(status, json=body)
        if path.endswith("/mappings"):
            return httpx.Response(200, json=self.mappings)
        if path.endswith("/chargingHistory"):
            if self.history_status != 200:
                return httpx.Response(self.history_status, json=self.history_body or {})
            index = len(self.history_requests()) - 1
            data, next_token = self.pages[min(index, len(self.pages) - 1)]
            return httpx.Response(200, json={"data": data, "next_token": next_token})
        return httpx.Response(404, json={})


@pytest.fixture
def bmw(monkeypatch):
    """Ersetzt den HTTP-Client des Moduls - auch fuer die Aufrufe ueber die
    Router, die selbst keinen Client durchreichen."""
    fake = FakeBmw()
    fake_httpx = types.SimpleNamespace(
        Client=lambda **kwargs: httpx.Client(transport=fake),
        HTTPError=httpx.HTTPError,
        Response=httpx.Response,
    )
    monkeypatch.setattr(bmw_cardata, "httpx", fake_httpx)
    return fake


def _connected_config(db, client, *, vin=VIN, cursor=None) -> models.BmwCarDataConfig:
    vehicle = create_vehicle(client, external_id="ix1", name="iX1", capacity=64.7)
    owner_id = db.get(models.Vehicle, vehicle["id"]).user_id
    config = models.BmwCarDataConfig(
        user_id=owner_id,
        vehicle_id=vehicle["id"],
        enabled=True,
        client_id="client-abc",
        vin=vin,
        access_token="acc-1",
        refresh_token="ref-1",
        access_expires_at=datetime.utcnow() + timedelta(hours=1),
        refresh_expires_at=datetime.utcnow() + timedelta(days=14),
        history_cursor=cursor,
    )
    db.add(config)
    db.commit()
    return config


# ---------------------------------------------------------------------------
# Auswertung eines Eintrags
# ---------------------------------------------------------------------------


def test_parse_ac_session_keeps_local_time_and_measured_energy():
    start = datetime(2026, 10, 1, 18, 30)
    item = bmw_cardata.HistorySession.parse(history_item(start))
    assert item.start_time == start
    assert item.end_time == start + timedelta(hours=2)
    assert (item.soc_start, item.soc_end) == (30, 80)
    assert item.energy_kwh == 40.0
    assert item.charging_type == "AC"
    assert item.odometer_km == 12345
    assert item.is_charge


def test_parse_infers_dc_from_power_and_converts_miles():
    item = bmw_cardata.HistorySession.parse(
        history_item(datetime(2026, 10, 1, 12), minutes=25, energy=45.0, block_kw=None,
                     mileage=1000, units="MI")
    )
    assert item.charging_type == "DC"  # 45 kWh in 25 min = 108 kW
    assert item.odometer_km == 1609


def test_parse_rejects_implausible_energy_and_null_island():
    raw = history_item(datetime(2026, 10, 1, 12), energy=999.0, lat=0.0, lon=0.0)
    item = bmw_cardata.HistorySession.parse(raw)
    assert item.energy_kwh is None
    assert item.latitude is None and item.longitude is None


def test_entry_without_energy_and_soc_gain_is_not_a_charge():
    item = bmw_cardata.HistorySession.parse(
        history_item(datetime(2026, 10, 1, 12), energy=None, soc=(80, 80), block_kw=None)
    )
    assert not item.is_charge


# ---------------------------------------------------------------------------
# Anmeldung
# ---------------------------------------------------------------------------


def test_device_flow_sends_pkce_and_fills_vin(client, db_session, bmw):
    register(client)
    vehicle = create_vehicle(client, external_id="ix1", name="iX1")

    saved = client.put(f"/api/bmw-cardata/configs/{vehicle['id']}",
                       json={"enabled": False, "client_id": "client-abc"})
    assert saved.status_code == 200
    assert saved.json()["has_client_id"] and not saved.json()["connected"]

    started = client.post(f"/api/bmw-cardata/configs/{vehicle['id']}/auth/start")
    assert started.status_code == 200, started.text
    assert started.json()["pending_user_code"] == "ABCD-EFGH"
    assert "code=ABCD-EFGH" in started.json()["pending_verification_uri"]

    device_form = bmw.form(bmw.requests[0])
    assert device_form["code_challenge_method"] == "S256"
    assert device_form["scope"] == bmw_cardata.SCOPE

    bmw.token_responses = [(400, {"error": "authorization_pending"})]
    pending = client.post(f"/api/bmw-cardata/configs/{vehicle['id']}/auth/poll")
    assert pending.json()["status"] == "pending"

    done = client.post(f"/api/bmw-cardata/configs/{vehicle['id']}/auth/poll")
    body = done.json()
    assert body["status"] == "connected"
    assert body["config"]["connected"]
    assert body["config"]["vin"] == VIN  # aus den Fahrzeugzuordnungen uebernommen
    assert body["config"]["pending_user_code"] is None
    # Verbunden und FIN bekannt: der automatische Abruf geht von selbst an.
    assert body["config"]["enabled"] is True

    # Der Verifier aus dem ersten Schritt muss zur Challenge passen.
    token_form = bmw.form([r for r in bmw.requests if r.url.path.endswith("/token")][-1])
    assert bmw_cardata.code_challenge(token_form["code_verifier"]) == device_form["code_challenge"]

    # Client-ID und Tokens verlassen die API nie.
    assert "client_id" not in body["config"] and "refresh_token" not in body["config"]


def test_expired_device_code_clears_pending(client, bmw):
    register(client)
    vehicle = create_vehicle(client, external_id="ix1", name="iX1")
    client.put(f"/api/bmw-cardata/configs/{vehicle['id']}", json={"client_id": "client-abc"})
    client.post(f"/api/bmw-cardata/configs/{vehicle['id']}/auth/start")
    bmw.token_responses = [(400, {"error": "expired_token"})]
    result = client.post(f"/api/bmw-cardata/configs/{vehicle['id']}/auth/poll").json()
    assert result["status"] == "expired"
    assert result["config"]["pending_user_code"] is None
    assert not result["config"]["connected"]


def test_enabling_requires_connection_and_vin(client):
    register(client)
    vehicle = create_vehicle(client, external_id="ix1", name="iX1")
    res = client.put(f"/api/bmw-cardata/configs/{vehicle['id']}",
                     json={"enabled": True, "client_id": "client-abc", "vin": VIN})
    assert res.status_code == 422
    short = client.put(f"/api/bmw-cardata/configs/{vehicle['id']}", json={"vin": "WBA123"})
    assert short.status_code == 422


# ---------------------------------------------------------------------------
# Abruf und Uebernahme
# ---------------------------------------------------------------------------


def test_poll_imports_sessions_once_with_measured_energy(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=5))
    start = datetime.now().replace(microsecond=0) - timedelta(days=2)
    bmw.pages = [([history_item(start), history_item(start + timedelta(days=1), soc=(60, 60),
                                                    energy=None, block_kw=None)], None)]

    bmw_cardata_poller.poll_vehicle(db_session, config)
    sessions = db_session.query(models.ChargingSession).all()
    assert len(sessions) == 1  # der zweite Eintrag ist kein Ladevorgang
    session = sessions[0]
    assert session.start_time == start
    assert session.energy_kwh == 40.0
    assert session.energy_is_estimated is False
    assert session.charging_type == models.ChargingType.AC
    assert session.source == models.SessionSource.AUTOMATIC
    assert session.needs_review
    assert session.external_session_id == f"bmw-{config.vehicle_id}-{_epoch(start)}"
    assert config.last_status == "ok" and config.last_imported_count == 1
    assert config.requests_today == 1

    # Zweiter Abruf ueber dasselbe Fenster legt nichts doppelt an.
    config.next_poll_at = None
    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert db_session.query(models.ChargingSession).count() == 1
    assert config.last_imported_count == 0


def test_measured_energy_counts_as_charger_even_for_vehicle_measured_provider(
    client, db_session, bmw
):
    """Der Heim-Anbieter steht oft auf "Fahrzeug" - eine netzseitige Messung
    von BMW darf dort nicht faelschlich als Fahrzeugmessung gelten."""
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=5))
    provider = client.post("/api/providers", json={
        "name": "Ionity", "energy_meter": "vehicle", "last_price_dc_per_kwh": 0.39,
    })
    assert provider.status_code == 201, provider.text
    start = datetime.now().replace(microsecond=0) - timedelta(days=1)
    bmw.pages = [([history_item(start, minutes=20, energy=40.0, block_kw=None,
                                providers=["IONITY"])], None)]

    bmw_cardata_poller.poll_vehicle(db_session, config)
    session = db_session.query(models.ChargingSession).one()
    assert session.provider_id == provider.json()["id"]  # Name ohne Gross/klein
    assert session.charging_type == models.ChargingType.DC
    assert session.price_per_kwh == 0.39
    assert session.price_total == round(0.39 * 40.0, 2)
    assert session.energy_meter == "charger"


def test_first_poll_backfills_in_windows(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client)
    config.initial_history_days = 45
    db_session.commit()

    bmw_cardata_poller.poll_vehicle(db_session, config)
    first = bmw.history_requests()[0].url.params
    since = datetime.strptime(first["from"], "%Y-%m-%dT%H:%M:%S.000Z")
    until = datetime.strptime(first["to"], "%Y-%m-%dT%H:%M:%S.000Z")
    assert until - since == timedelta(days=bmw_cardata_poller.WINDOW_DAYS)
    # Noch nicht in der Gegenwart: gleich mit dem naechsten Fenster weiter.
    assert abs(config.history_cursor - until) < timedelta(seconds=1)
    assert config.next_poll_at - datetime.utcnow() < timedelta(minutes=5)

    bmw_cardata_poller.poll_vehicle(db_session, config)
    # Gegenwart erreicht: Cursor mit Ueberlappung, naechster Abruf nach Intervall.
    assert config.next_poll_at - datetime.utcnow() > timedelta(hours=3)
    assert datetime.utcnow() - config.history_cursor > timedelta(days=2)


def test_pagination_stops_at_budget_and_keeps_cursor_on_last_session(client, db_session, bmw):
    register(client)
    cursor = datetime.utcnow() - timedelta(days=10)
    config = _connected_config(db_session, client, cursor=cursor)
    # Nur noch zwei Anfragen frei.
    config.requests_day = datetime.utcnow().strftime("%Y-%m-%d")
    config.requests_today = bmw_cardata_poller.DAILY_REQUEST_LIMIT - bmw_cardata_poller.RESERVE_REQUESTS - 2
    db_session.commit()
    base = datetime.now().replace(microsecond=0) - timedelta(days=8)
    bmw.pages = [
        ([history_item(base)], "page-2"),
        ([history_item(base + timedelta(days=1))], "page-3"),
        ([history_item(base + timedelta(days=2))], None),
    ]

    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert len(bmw.history_requests()) == 2
    assert bmw.history_requests()[1].url.params["nextToken"] == "page-2"
    assert db_session.query(models.ChargingSession).count() == 2
    assert config.history_cursor > cursor
    assert bmw_cardata_poller.requests_left(config) == 0

    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert len(bmw.history_requests()) == 2  # kein weiterer Abruf
    assert config.last_status == "rate_limited"


def test_expired_refresh_token_sets_auth_error(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=1))
    config.access_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()
    bmw.refresh_status = 400

    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert config.last_status == "auth_error"
    assert not bmw.history_requests()
    assert config.next_poll_at > datetime.utcnow() + timedelta(hours=1)


def test_token_is_refreshed_before_expiry(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=1))
    config.access_expires_at = datetime.utcnow() + timedelta(minutes=1)
    db_session.commit()

    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert config.access_token == "acc-2"
    assert config.refresh_token == "ref-2"
    assert bmw.history_requests()[0].headers["Authorization"] == "Bearer acc-2"
    assert bmw.history_requests()[0].headers["x-version"] == "v1"


def test_api_rate_limit_is_reported(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=1))
    bmw.history_status = 429
    bmw.history_body = {"exveErrorId": "CU-429", "exveErrorMsg": "Too many requests"}

    bmw_cardata_poller.poll_vehicle(db_session, config)
    assert config.last_status == "rate_limited"


# ---------------------------------------------------------------------------
# Trennung pro Nutzer und Aufraeumen
# ---------------------------------------------------------------------------


def test_foreign_vehicle_is_404(client):
    register(client, "alice")
    vehicle = create_vehicle(client, external_id="ix1", name="iX1")
    register(client, "bob")
    assert client.get(f"/api/bmw-cardata/configs/{vehicle['id']}").status_code == 404
    assert client.get("/api/bmw-cardata/configs").json() == []


def test_poll_endpoint_and_log(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client, cursor=datetime.utcnow() - timedelta(days=3))
    start = datetime.now().replace(microsecond=0) - timedelta(days=1)
    bmw.pages = [([history_item(start)], None)]

    res = client.post(f"/api/bmw-cardata/configs/{config.vehicle_id}/poll")
    assert res.status_code == 200, res.text
    assert res.json()["last_imported_count"] == 1
    assert res.json()["requests_today"] == 1

    logs = client.get(f"/api/bmw-cardata/logs?vehicle_id={config.vehicle_id}").json()
    created = [entry for entry in logs if entry["event"] == "session_created"]
    assert created and created[0]["has_payload"]
    payload = client.get(f"/api/bmw-cardata/logs/{created[0]['id']}/payload")
    assert payload.status_code == 200
    assert payload.json()["startTime"] == _epoch(start)


def test_deleting_vehicle_removes_cardata_config(client, db_session, bmw):
    register(client)
    config = _connected_config(db_session, client)
    bmw_cardata_poller.log_event(db_session, config, "auth", "test")
    db_session.commit()
    vehicle_id = config.vehicle_id

    assert client.delete(f"/api/vehicles/{vehicle_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.BmwCarDataConfig).count() == 0
    assert db_session.query(models.BmwCarDataLogEntry).count() == 0


def test_deleting_user_removes_cardata_config(client, db_session, bmw):
    register(client, "admin")
    admin_headers = dict(client.headers)
    register(client, "bob")
    config = _connected_config(db_session, client)
    bmw_cardata_poller.log_event(db_session, config, "auth", "test")
    db_session.commit()
    bob_id = config.user_id
    bob = db_session.get(models.User, bob_id)
    assert bob.username == "bob"

    client.headers.clear()
    client.headers.update(admin_headers)
    assert client.delete(f"/api/auth/users/{bob_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(models.BmwCarDataConfig).count() == 0


def test_connecting_without_vin_waits_for_vin_before_enabling(client, bmw):
    register(client)
    vehicle = create_vehicle(client, external_id="ix1", name="iX1")
    url = f"/api/bmw-cardata/configs/{vehicle['id']}"
    bmw.mappings = []  # keine Zuordnung, FIN bleibt leer
    client.put(url, json={"client_id": "client-abc"})
    client.post(f"{url}/auth/start")
    done = client.post(f"{url}/auth/poll").json()
    assert done["status"] == "connected" and done["config"]["enabled"] is False

    # Die FIN vervollstaendigt die Konfiguration - erst jetzt an.
    assert client.put(url, json={"vin": VIN}).json()["enabled"] is True
    # Bewusst wieder aus bleibt aus, auch beim naechsten Speichern.
    assert client.put(url, json={"enabled": False}).json()["enabled"] is False
    assert client.put(url, json={"vin": VIN}).json()["enabled"] is False
