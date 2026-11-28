"""Rueckdatierung des Ladebeginns beim Home-Assistant-Push (v0.58.0).

Auch Home Assistant liest die Fahrzeugdaten aus der Cloud und bemerkt das
Einstecken erst beim naechsten Abruf. Lademonitor-HA 0.11.0 schickt deshalb
den letzten SoC VOR dem Einstecken aus dem HA-Verlauf mit (und, mit
Wallbox-Zaehler, den gemessenen Ladebeginn). Der Server rechnet damit wie
beim MyŠkoda-Abruf zurueck - mit denselben Waechtern.
"""

from datetime import datetime, timedelta

from app import charge_detection, models
from app.vw_data_act_poller import SOURCE as VW_SOURCE
from conftest import create_vehicle, register


def _push(client, sid="ha-1", **extra):
    body = {
        "vehicle_external_id": "enyaq",
        "external_session_id": sid,
        "start_time": "2026-11-28T10:20:00",
        "end_time": "2026-11-28T10:50:00",
        "charging_type": "dc",
        "soc_start": 30,
        "soc_end": 80,
    }
    body.update(extra)
    response = client.post("/api/sessions/auto", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _setup(client):
    register(client)
    create_vehicle(client, external_id="enyaq", capacity=77.0)


def test_without_values_before_nothing_changes(client):
    _setup(client)
    s = _push(client)
    assert s["soc_start"] == 30
    assert s["start_time"].startswith("2026-11-28T10:20")
    assert not s["notes"]


def test_dc_start_is_backdated_with_soc_before(client):
    """Erkannt bei 30 % um 10:20, 15 min davor stand der SoC bei 8 %. Bei
    77 kW (50 % von 77 kWh in 30 min) passen 22 Punkte in 13 min."""
    _setup(client)
    s = _push(client, soc_before=8, soc_before_time="2026-11-28T10:05:00")

    assert s["soc_start"] == 8
    assert s["start_time"].startswith("2026-11-28T10:06:48")
    # Die Energieschaetzung bekommt den verschluckten Anteil mit: 72 % von 77.
    assert s["energy_kwh"] == 55.44
    assert "Ladebeginn zurückdatiert" in s["notes"]
    assert s["external_session_id"] == "ha-1"


def test_physics_limits_the_correction_for_slow_ac(client):
    """AC mit 7,7 kW: in 30 min Luecke passen nur 5 Punkte, nicht 10."""
    _setup(client)
    s = _push(
        client,
        start_time="2026-11-28T22:30:00",
        end_time="2026-11-29T02:30:00",
        charging_type="ac",
        soc_start=40,
        soc_end=80,
        soc_before=30,
        soc_before_time="2026-11-28T22:00:00",
    )
    assert s["soc_start"] == 35
    assert s["start_time"].startswith("2026-11-28T22:00")


def test_too_old_value_before_is_ignored(client):
    _setup(client)
    s = _push(client, soc_before=8, soc_before_time="2026-11-28T08:50:00")
    assert s["soc_start"] == 30
    assert s["start_time"].startswith("2026-11-28T10:20")


def test_higher_value_before_means_driving_not_charging(client):
    _setup(client)
    s = _push(client, soc_before=45, soc_before_time="2026-11-28T10:05:00")
    assert s["soc_start"] == 30


def test_unusable_soc_before_does_not_reject_the_push(client):
    _setup(client)
    s = _push(client, soc_before="unknown", soc_before_time="2026-11-28T10:05:00")
    assert s["soc_start"] == 30


def test_measured_start_keeps_time_and_ignores_age(client):
    """Start laut Wallbox 10:05, HA hat 10:20 bemerkt. Der SoC davor ist
    zwei Stunden alt - das Einstecken ist aber gemessen, also zaehlt nur, was
    in den 15 min bis zum Erkennen geladen werden konnte."""
    _setup(client)
    s = _push(
        client,
        start_time="2026-11-28T10:05:00",
        detected_time="2026-11-28T10:20:00",
        start_time_measured=True,
        soc_before=8,
        soc_before_time="2026-11-28T08:20:00",
        energy_kwh=60.0,
        energy_meter="charger",
    )
    assert s["soc_start"] == 8
    assert s["start_time"].startswith("2026-11-28T10:05:00")
    assert s["energy_kwh"] == 60.0
    assert "Start-SoC zurückgerechnet" in s["notes"]


def test_measured_start_at_detection_changes_nothing(client):
    _setup(client)
    s = _push(
        client,
        detected_time="2026-11-28T10:20:00",
        start_time_measured=True,
        soc_before=8,
        soc_before_time="2026-11-28T10:05:00",
    )
    assert s["soc_start"] == 30


def test_later_ha_start_does_not_overwrite_earlier_poller_start(client, db_session):
    """Der Poller hat den Vorgang schon zurueckdatiert angelegt (10:05, 8 %).
    Ein HA-Push ohne Werte davor sieht ihn erst um 10:20 bei 30 % - das ist
    nicht genauer, nur spaeter bemerkt."""
    _setup(client)
    vehicle = db_session.query(models.Vehicle).one()
    config = models.VwDataActConfig(user_id=vehicle.user_id, vehicle_id=vehicle.id, brand="SKODA")
    db_session.add(config)
    db_session.commit()
    # Die Ladeerkennung rechnet in naiver UTC und legt in Ortszeit ab.
    offset = charge_detection.utc_to_local(datetime(2026, 11, 28, 12)) - datetime(2026, 11, 28, 12)
    poller_start_utc = datetime(2026, 11, 28, 10, 5) - offset
    charge_detection.create_session(
        db_session, config, VW_SOURCE,
        start_time=poller_start_utc, end_time=poller_start_utc + timedelta(minutes=50),
        soc_start=8, soc_end=80, charging_type=models.ChargingType.DC,
        odometer_km=None, outside_temp_c=None, latitude=None, longitude=None,
        notes="Erkannt ueber das VW-Datenportal.", energy_kwh=None,
    )
    db_session.commit()

    _push(client)

    db_session.expire_all()
    [session] = db_session.query(models.ChargingSession).all()
    assert session.start_time == datetime(2026, 11, 28, 10, 5)
    assert session.soc_start == 8
