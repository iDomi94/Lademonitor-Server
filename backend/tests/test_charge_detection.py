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


def test_counter_without_reading_before_plugging_in_starts_at_zero(db_session, config):
    # Nachgebaut am Enyaq vom 18./19.10.2026: vor dem Einstecken kein
    # Zaehlerstand, der erste Ladebericht steht schon bei 13 kWh.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=10),
        report(270, plugged=True, soc=25, energy=13.0, power=7),
        report(600, plugged=True, soc=71, energy=49.0, power=7),
        report(1050, plugged=False, soc=80, energy=49.0),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(49.0)
    assert sessions[0].energy_is_estimated is False


def test_cumulative_counter_without_reading_before_is_not_counted_from_zero(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40),
        report(15, plugged=True, soc=42, energy=1201.5, power=11),
        report(45, plugged=False, soc=55, energy=1209.0),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(7.5)


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


def test_charging_state_seen_creates_session_without_any_numbers(db_session, config):
    # Wie beim ID.3 vom 18./19.10.2026: waehrend des Steckens kommen Berichte
    # ohne SoC, Leistung und Zaehler - aber einer meldet "laedt". Lieber ein
    # Vorgang mit Luecken als keiner.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=None),
        report(15, plugged=True, soc=None, power=None, state="READY_FOR_CHARGING"),
        report(30, plugged=True, soc=None, power=None, state="CHARGING_HV_BATTERY"),
        report(45, plugged=False, soc=None),
    ])
    assert len(sessions) == 1
    assert sessions[0].needs_review is True
    assert sessions[0].energy_kwh is None
    assert config.open_charging_seen is None


def test_ready_state_only_is_still_discarded(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=None),
        report(15, plugged=True, soc=None, power=None, state="READY_FOR_CHARGING"),
        report(45, plugged=False, soc=None),
    ])
    assert sessions == []


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


# --- Nacherkennung ueber den Energiezaehler ---------------------------------
# Ein ZIP des VW-Portals enthaelt je Datenfeld nur den letzten Wert, keinen
# Verlauf. Ein kurzer Vorgang, der komplett zwischen zwei Datensaetzen liegt,
# zeigt sich also nie als "Kabel steckt" - nur am SoC und am Zaehler.


def test_short_charge_between_reports_is_found_by_the_counter(db_session, config):
    # 10 min AC: +2 kWh, +3 % - unter der SoC-Schwelle von 5 Punkten.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=1200.0),
        report(15, plugged=False, soc=43, energy=1202.0),
    ])
    assert len(sessions) == 1
    s = sessions[0]
    assert s.energy_kwh == pytest.approx(2.0)
    assert s.energy_is_estimated is False
    assert s.energy_meter == "vehicle"
    assert (s.soc_start, s.soc_end) == (40, 43)
    assert s.start_time == T0 and s.end_time == T0 + timedelta(minutes=15)
    assert "Energiezähler" in s.notes
    assert config.last_energy_raw_kwh == 1202.0


def test_counter_restarting_from_zero_is_found_too(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=25.0),  # Stand der letzten Ladung
        report(15, plugged=False, soc=43, energy=1.8),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(1.8)


def test_counter_found_session_without_soc(db_session, config):
    # Teilbericht ohne SoC (Skoda-Muster) - der Zaehler allein reicht.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=100.0),
        report(15, plugged=False, soc=None, energy=103.5),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(3.5)
    assert sessions[0].soc_end is None


@pytest.mark.parametrize("energies", [
    (100.0, 100.0),  # unveraendert
    (100.0, 100.6),  # unter 1 kWh
    (100.0, 0.0),    # Zaehler zurueckgesetzt, nichts geladen
])
def test_counter_without_real_gain_creates_nothing(db_session, config, energies):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=energies[0]),
        report(15, plugged=False, soc=41, energy=energies[1]),
    ])
    assert sessions == []


def test_counter_ignored_when_missed_detection_is_off(db_session, config):
    config.detect_missed_sessions = False
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=1200.0),
        report(15, plugged=False, soc=43, energy=1202.0),
    ])
    assert sessions == []


def test_counter_does_not_double_count_a_regular_session(db_session, config):
    # Nach einem normal erkannten Vorgang steht der Ausgangswert auf dem
    # Endstand - der naechste Bericht ohne Kabel legt nichts zusaetzlich an.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40, energy=1200.0),
        report(15, plugged=True, soc=42, energy=1201.5, power=11),
        report(30, plugged=False, soc=50, energy=1205.0),
        report(45, plugged=False, soc=49, energy=1205.0),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == pytest.approx(5.0)


def test_soc_jump_without_counter_still_works(db_session, config):
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=40),
        report(15, plugged=False, soc=60),
    ])
    assert len(sessions) == 1
    assert sessions[0].energy_is_estimated is True
    assert "SoC von 40 % auf 60 %" in sessions[0].notes
