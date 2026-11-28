"""Kabelstatus der MyŠkoda Public API (`plugConnectionState`, seit API 1.1.0).

Die API laesst `charging.status.state` weg, wenn die Kombination aus
Ladeprozess und Stecker keinen Namen hat - etwa eingesteckt und entriegelt
ohne Ladung oder kurz nach einer Fahrt. Weggelassen heisst ausdruecklich nicht
ausgesteckt; fuer den Kabelstatus soll man `plugConnectionState` lesen. Ohne
das Feld entschied der Poller in genau diesen Antworten nichts und verpasste so
Ein- oder Ausstecken.
"""

from datetime import datetime, timedelta

import pytest

from app import charge_detection, models
from app.myskoda import VehicleSnapshot
from app.myskoda_poller import SOURCE
from conftest import create_vehicle, register

VIN = "TMBJX7NY4PM054321"
T0 = datetime(2026, 10, 20, 17, 0)


def snap(minutes=0, *, state=None, plug=None, soc=50, power=None):
    when = T0 + timedelta(minutes=minutes)
    status = {"battery": {"stateOfChargeInPercent": soc}}
    if state is not None:
        status["state"] = state
    if plug is not None:
        status["plugConnectionState"] = plug
    if power is not None:
        status["chargePowerInKw"] = power
    payload = {
        "vehicle": {
            "vin": VIN,
            "charging": {
                "isVehicleInSavedLocation": False,
                "status": status,
                "carCapturedTimestamp": when.isoformat() + "Z",
            },
        }
    }
    return VehicleSnapshot.from_response(VIN, payload, fetched_at=when)


def test_without_plug_field_behaviour_is_unchanged():
    assert snap(state="CHARGING").is_plugged_in is True
    assert snap(state="CONNECT_CABLE").is_plugged_in is False
    assert snap(state="CONNECT_CABLE").charging_state == "CONNECT_CABLE"
    assert snap().charging_state is None


def test_missing_state_falls_back_to_plug():
    plugged = snap(plug="CONNECTED")
    assert plugged.charging_state == "PLUG_CONNECTED"
    assert plugged.is_plugged_in is True
    unplugged = snap(plug="DISCONNECTED")
    assert unplugged.charging_state == "PLUG_DISCONNECTED"
    assert unplugged.is_plugged_in is False


def test_plug_field_wins_over_state():
    assert snap(state="CONNECT_CABLE", plug="CONNECTED").is_plugged_in is True
    assert snap(state="READY_FOR_CHARGING", plug="DISCONNECTED").is_plugged_in is False
    # Der API-Zustand bleibt die Anzeige, wenn es ihn gibt.
    assert snap(state="CHARGING", plug="CONNECTED").charging_state == "CHARGING"


def test_discharging_counts_as_unplugged_even_with_cable():
    assert snap(state="DISCHARGING", plug="CONNECTED").is_plugged_in is False


def test_unknown_plug_value_is_ignored():
    s = snap(state="CHARGING", plug="SOMETHING_NEW")
    assert s.plug_connected is None
    assert s.is_plugged_in is True
    assert snap(plug="SOMETHING_NEW").charging_state is None


def test_summary_contains_plug_state():
    assert snap(plug="CONNECTED").summary()["plug_connection_state"] == "CONNECTED"


@pytest.fixture
def config(client, db_session):
    register(client)
    vehicle = create_vehicle(client, external_id="enyaq", name="Enyaq", capacity=77.0)
    owner_id = db_session.get(models.Vehicle, vehicle["id"]).user_id
    cfg = models.MySkodaConfig(user_id=owner_id, vehicle_id=vehicle["id"], enabled=True,
                               api_key="key", vin=VIN, log_raw_payload=False)
    db_session.add(cfg)
    db_session.commit()
    return cfg


def run(db, cfg, snapshots):
    for s in snapshots:
        assert charge_detection.apply_snapshot(db, cfg, s, SOURCE)
    db.commit()
    return db.query(models.ChargingSession).all()


def test_session_ends_although_state_is_missing_at_unplug(db_session, config):
    sessions = run(db_session, config, [
        snap(0, state="CONNECT_CABLE", plug="DISCONNECTED", soc=30),
        snap(20, state="CHARGING", plug="CONNECTED", soc=35, power=11),
        snap(40, state="CHARGING", plug="CONNECTED", soc=45, power=11),
        # Kurz nach dem Ausstecken ohne Zustand - frueher: nichts entschieden.
        snap(60, plug="DISCONNECTED", soc=45),
    ])
    assert len(sessions) == 1
    assert sessions[0].soc_end == 45
    assert config.open_start_time is None


def test_plugged_without_state_does_not_end_open_session(db_session, config):
    sessions = run(db_session, config, [
        snap(0, state="CONNECT_CABLE", plug="DISCONNECTED", soc=30),
        snap(20, state="CHARGING", plug="CONNECTED", soc=35, power=11),
        # Eingesteckt und entriegelt, keine Ladung: kein Zustand, Kabel steckt.
        snap(40, plug="CONNECTED", soc=60),
    ])
    assert sessions == []
    assert config.open_start_time is not None
    assert config.last_charging_state == "PLUG_CONNECTED"


# --- API 1.2.0: CHARGING_ERROR und unbekannte Zustaende (0.57.0) -----------

def test_charging_error_counts_as_plugged_in():
    s = snap(state="CHARGING_ERROR")
    assert s.is_plugged_in is True
    assert s.charging_state == "CHARGING_ERROR"


def test_charging_error_reason_is_read_and_summarised():
    when = T0
    payload = {"vehicle": {"vin": VIN, "charging": {
        "status": {"state": "CHARGING_ERROR", "chargingErrorReason": "NO_GRID_VOLTAGE",
                   "battery": {"stateOfChargeInPercent": 40}},
        "carCapturedTimestamp": when.isoformat() + "Z"}}}
    s = VehicleSnapshot.from_response(VIN, payload, fetched_at=when)
    assert s.charging_error_reason == "NO_GRID_VOLTAGE"
    assert s.summary()["charging_error_reason"] == "NO_GRID_VOLTAGE"
    assert snap(state="CHARGING").charging_error_reason is None


def test_unknown_state_without_plug_is_undecided():
    s = snap(state="SOMETHING_NEW")
    assert s.is_plugged_in is None
    assert s.charging_state is None
    assert s.summary()["api_charging_state"] == "SOMETHING_NEW"
    # Mit Steckerangabe entscheidet der Stecker.
    assert snap(state="SOMETHING_NEW", plug="CONNECTED").is_plugged_in is True
    assert snap(state="SOMETHING_NEW", plug="DISCONNECTED").is_plugged_in is False


def test_unknown_state_does_not_end_open_session(db_session, config):
    run(db_session, config, [
        snap(0, state="CONNECT_CABLE", soc=30),
        snap(20, state="CHARGING", soc=35, power=11),
    ])
    assert not charge_detection.apply_snapshot(
        db_session, config, snap(40, state="SOMETHING_NEW", soc=40), SOURCE)
    assert config.open_start_time is not None
    assert db_session.query(models.ChargingSession).count() == 0


def test_charging_error_keeps_open_session_and_is_logged(db_session, config):
    from app import myskoda_poller

    run(db_session, config, [
        snap(0, state="CONNECT_CABLE", soc=30),
        snap(20, state="CHARGING", soc=35, power=11),
    ])
    payload = {"vehicle": {"vin": VIN, "charging": {
        "status": {"state": "CHARGING_ERROR", "chargingErrorReason": "ERROR_CHARGING_SYSTEM",
                   "battery": {"stateOfChargeInPercent": 41}},
        "carCapturedTimestamp": (T0 + timedelta(minutes=40)).isoformat() + "Z"}}}
    err = VehicleSnapshot.from_response(VIN, payload, fetched_at=T0 + timedelta(minutes=40))
    myskoda_poller._apply_snapshot(db_session, config, err)
    db_session.commit()
    assert config.open_start_time is not None
    assert db_session.query(models.ChargingSession).count() == 0
    entry = (db_session.query(models.MySkodaLogEntry)
             .filter_by(event="charging_error").one())
    assert "ERROR_CHARGING_SYSTEM" in entry.message
    # Danach ausgesteckt: der Vorgang wird normal beendet.
    sessions = run(db_session, config, [snap(60, state="CONNECT_CABLE", soc=41)])
    assert len(sessions) == 1
