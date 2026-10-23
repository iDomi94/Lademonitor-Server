"""Laufende Ladevorgaenge ("laedt gerade", siehe active_sessions.py).

Zwei Quellen: die Startmeldung aus Home Assistant und der offene Zustand der
Poller. Geprueft wird der Vertrag, gegen den Web, Apps und HA gebaut sind.
"""

from datetime import datetime, timedelta

from app import models
from conftest import create_vehicle, register


def _active(client):
    response = client.get("/api/sessions/active")
    assert response.status_code == 200, response.text
    return response.json()


def test_empty_without_any_source(client):
    register(client)
    create_vehicle(client)
    assert _active(client) == []


def test_home_assistant_start_until_push(client):
    register(client)
    create_vehicle(client, capacity=77.0)

    r = client.post("/api/sessions/active", json={
        "vehicle_external_id": "enyaq",
        "start_time": "2026-10-23T14:05:00+02:00",
        "charging_type": "ac", "soc_start": 23, "outside_temp_c": "unknown",
    })
    assert r.status_code == 204, r.text

    [entry] = _active(client)
    assert entry["source"] == "home_assistant"
    assert entry["vehicle_name"] == "Enyaq"
    # Wanduhrzeit, wie beim Push am Ladeende.
    assert entry["start_time"].startswith("2026-10-23T14:05")
    assert entry["charging_type"] == "AC"
    assert entry["soc_start"] == 23 and entry["soc_current"] == 23
    assert entry["energy_kwh"] is None
    assert entry["outside_temp_c"] is None

    # Zwischenstand: nur der SoC kommt, der Rest bleibt.
    client.post("/api/sessions/active", json={"vehicle_external_id": "enyaq", "soc_current": "61"})
    [entry] = _active(client)
    assert entry["soc_start"] == 23 and entry["soc_current"] == 61
    assert entry["energy_kwh"] == round(77.0 * 0.38, 2)
    assert entry["energy_is_estimated"] is True

    # Push am Ladeende raeumt den Eintrag weg.
    r = client.post("/api/sessions/auto", json={
        "vehicle_external_id": "enyaq", "external_session_id": "ha-1",
        "start_time": "2026-10-23T14:05:00+02:00", "soc_start": 23, "soc_end": 80,
    })
    assert r.status_code == 201, r.text
    assert _active(client) == []


def test_new_start_replaces_old_report(client):
    register(client)
    create_vehicle(client)
    client.post("/api/sessions/active", json={
        "vehicle_external_id": "enyaq", "start_time": "2026-10-20T08:00:00", "soc_start": 10,
        "soc_current": 50,
    })
    client.post("/api/sessions/active", json={
        "vehicle_external_id": "enyaq", "start_time": "2026-10-23T08:00:00", "soc_start": 30,
    })
    [entry] = _active(client)
    assert entry["soc_start"] == 30 and entry["soc_current"] == 30


def test_delete_and_unknown_vehicle(client):
    register(client)
    create_vehicle(client)
    client.post("/api/sessions/active", json={"vehicle_external_id": "enyaq", "soc_start": 30})
    assert len(_active(client)) == 1
    assert client.delete("/api/sessions/active?vehicle_external_id=enyaq").status_code == 204
    assert _active(client) == []
    r = client.post("/api/sessions/active", json={"vehicle_external_id": "nope"})
    assert r.status_code == 404


def test_stale_home_assistant_report_is_hidden(client, db_session):
    register(client)
    create_vehicle(client)
    client.post("/api/sessions/active", json={"vehicle_external_id": "enyaq", "soc_start": 30})
    report = db_session.query(models.ActiveChargeReport).one()
    report.updated_at = datetime.utcnow() - timedelta(days=4)
    db_session.commit()
    assert _active(client) == []


def test_poller_open_session(client, db_session):
    register(client)
    vehicle = create_vehicle(client, capacity=52.0)
    user_id = db_session.query(models.User).one().id
    config = models.VwDataActConfig(
        user_id=user_id, vehicle_id=vehicle["id"], enabled=True,
        open_start_time=datetime(2026, 10, 23, 12, 0), open_soc_start=40,
        open_soc_last=55, last_soc=55, open_energy_kwh=8.2,
        open_charging_type="AC", open_odometer_km=12345,
        last_captured_at=datetime(2026, 10, 23, 13, 0), last_charging_state="CHARGING",
    )
    db_session.add(config)
    db_session.commit()

    [entry] = _active(client)
    assert entry["source"] == "vw_data_act"
    assert entry["soc_start"] == 40 and entry["soc_current"] == 55
    assert entry["energy_kwh"] == 8.2 and entry["energy_is_estimated"] is False
    assert entry["odometer_km"] == 12345
    assert entry["charging_state"] == "CHARGING"
    assert entry["updated_at"] is not None

    # Ausgeschaltete Quelle zeigt nichts, auch mit offenem Zustand.
    config.enabled = False
    db_session.commit()
    assert _active(client) == []


def test_other_users_see_nothing(client):
    register(client)
    create_vehicle(client)
    client.post("/api/sessions/active", json={"vehicle_external_id": "enyaq", "soc_start": 30})
    client.headers.pop("Authorization")
    register(client, username="zweiter")
    assert _active(client) == []


def test_deleting_vehicle_removes_report(client):
    register(client)
    vehicle = create_vehicle(client)
    client.post("/api/sessions/active", json={"vehicle_external_id": "enyaq", "soc_start": 30})
    assert client.delete(f"/api/vehicles/{vehicle['id']}").status_code == 204
    assert _active(client) == []
