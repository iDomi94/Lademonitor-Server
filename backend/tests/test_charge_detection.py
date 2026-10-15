"""Gemeinsame Ladeerkennung (`charge_detection.py`) mit gemessener Energie.

Ob der Energiezaehler eines Fahrzeugs je Ladevorgang bei null beginnt oder
insgesamt zaehlt, ist beim VW-Portal nicht dokumentiert - beide Varianten
muessen dieselbe Energie ergeben. Die Berichte hier sind schlichte Objekte mit
den Eigenschaften, die die Zustandsmaschine liest.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import charge_detection, models
from app.vw_data_act_poller import SOURCE
from conftest import create_vehicle, register

T0 = datetime(2026, 10, 3, 18, 0)


def report(minutes, *, plugged, soc, energy=None, power=0.0, state=None):
    when = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(
        charging_state=state or ("CHARGING_HV_BATTERY" if plugged else "NOT_READY_FOR_CHARGING"),
        is_plugged_in=plugged,
        soc_percent=soc,
        charge_power_kw=power,
        charge_type="AC" if plugged else "OFF",
        odometer_km=20000,
        outside_temp_c=None,
        coordinates=None,
        is_in_saved_location=None,
        captured_at=when,
        fetched_at=when,
        charged_energy_kwh=energy,
    )


@pytest.fixture
def config(client, db_session):
    register(client)
    vehicle = create_vehicle(client, external_id="id3", name="ID.3", capacity=58.0)
    owner_id = db_session.get(models.Vehicle, vehicle["id"]).user_id
    cfg = models.VwDataActConfig(user_id=owner_id, vehicle_id=vehicle["id"], brand="VOLKSWAGEN_PASSENGER_CARS",
                               log_raw_payload=False)
    db_session.add(cfg)
    db_session.commit()
    return cfg


def run(db, cfg, reports):
    for r in reports:
        assert charge_detection.apply_snapshot(db, cfg, r, SOURCE)
    db.commit()
    return db.query(models.ChargingSession).all()


def test_cumulative_counter_counts_the_difference(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=1200.0),
        report(15, plugged=True, soc=42, energy=1201.5, power=11),
        report(30, plugged=True, soc=50, energy=1206.0, power=11),
        report(45, plugged=False, soc=55, energy=1209.0),
    ])
    assert len(sessions) == 1
    # Ausgangswert ist der Stand VOR dem Einstecken - der verschluckte Anfang zaehlt mit.
    assert sessions[0].energy_kwh == pytest.approx(9.0)
    assert sessions[0].energy_is_estimated is False
    assert config.last_energy_raw_kwh == 1209.0


def test_counter_per_session_restarts_from_zero(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=25.0),  # Stand der letzten Ladung
        report(15, plugged=True, soc=42, energy=0.8, power=11),
        report(30, plugged=True, soc=50, energy=6.3, power=11),
        report(45, plugged=False, soc=55, energy=8.0),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(8.0)


def test_without_counter_energy_is_estimated_from_soc(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40),
        report(15, plugged=True, soc=42, power=11),
        report(45, plugged=False, soc=60),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_is_estimated is True
    # Rueckdatiert auf den SoC vor dem Einstecken: 20 % von 58 kWh.
    assert sessions[0].soc_start == 40
    assert sessions[0].energy_kwh == pytest.approx(11.6, abs=0.1)


def test_plugged_without_charging_is_discarded(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=80, energy=10.0),
        report(15, plugged=True, soc=80, energy=10.0, state="CHARGE_PURPOSE_REACHED_AND_CONSERVATION"),
        report(30, plugged=False, soc=80, energy=10.0),
    ])
    assert sessions == []
    assert config.open_start_time is None


def test_report_without_soc_keeps_the_last_known_value(db_session, config):
    # Echtes Muster aus dem Skoda-Datenportal (15.10.2026): neben vollstaendigen
    # Berichten kommen welche nur mit Einstellungen und Ladezustand, ohne SoC.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=52),
        report(15, plugged=False, soc=None),
        report(30, plugged=True, soc=None, power=11),
        report(45, plugged=True, soc=70, power=11),
        report(60, plugged=False, soc=None),
    ])
    assert len(sessions) == 1
    assert sessions[0].soc_start == 52
    assert sessions[0].soc_end == 70
    assert config.last_soc == 70
