"""Gemeinsame Ladeerkennung (`charge_detection.py`) mit gemessener Energie.

Ob der Energiezaehler eines Fahrzeugs je Ladevorgang bei null beginnt oder
insgesamt zaehlt, ist beim VW-Portal nicht dokumentiert - beide Varianten
muessen dieselbe Energie ergeben. Die Berichte hier sind schlichte Objekte mit
den Eigenschaften, die die Zustandsmaschine liest.
"""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import charge_detection, models
from app.vw_data_act_poller import SOURCE
from conftest import create_vehicle, register

T0 = datetime(2026, 10, 3, 18, 0)


def report(minutes, *, plugged, soc, energy=None, power=0.0, state=None, km=20000):
    when = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(
        charging_state=state or ("CHARGING_HV_BATTERY" if plugged else "NOT_READY_FOR_CHARGING"),
        is_plugged_in=plugged,
        soc_percent=soc,
        charge_power_kw=power,
        charge_type="AC" if plugged else "OFF",
        odometer_km=km,
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


# --- Zeitzone ------------------------------------------------------------------
# Die Zustandsmaschine rechnet in UTC (captured_at der APIs), Ladevorgaenge
# liegen als lokale Zeit in der DB. Echter Fall (ID.3, 19.10.2026): Beginn
# 05:24 UTC stand als 05:24 in der Liste statt 07:24.


def test_session_times_are_stored_as_local_time(db_session, config, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        sessions = run(db_session, config, [
            report(0, plugged=False, soc=40),
            report(15, plugged=True, soc=40, power=11),
            report(60, plugged=False, soc=60),
        ])
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
    assert len(sessions) == 1
    s = sessions[0]
    # Oktober 2026 = Sommerzeit, UTC+2.
    assert s.start_time == T0 + timedelta(hours=2, minutes=15)
    assert s.end_time == T0 + timedelta(hours=2, minutes=60)


def test_stale_start_soc_is_corrected_by_the_measured_energy(db_session, config):
    # Nachgebaut am ID.3 vom 20.10.2026: der letzte SoC (77 %) stammt aus einem
    # Bericht vor rund 95 km Fahrt, beim Einstecken und waehrend des Ladens
    # kommen nur Berichte ohne SoC. Gemessen: 23 kWh bis 90 %.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=77, energy=16.0, km=51351),
        report(800, plugged=False, soc=None, km=51355),
        report(1000, plugged=True, soc=None, energy=0.0, state="READY_FOR_CHARGING", km=None),
        report(1300, plugged=True, soc=None, energy=10.0, state="CHARGING_HV_BATTERY", km=51450),
        report(1600, plugged=False, soc=90, energy=23.0, km=51450),
    ])
    assert len(sessions) == 1
    # 90 - 23 / 58 * 100 = 50,3
    assert sessions[0].soc_start == 50
    assert sessions[0].energy_kwh == pytest.approx(23.0)
    assert "99 km vor dem Einstecken" in sessions[0].notes


def test_small_gap_is_corrected_after_driving(db_session, config):
    # ID.3 vom 21.10.2026 mit 52 kWh netto: SoC 49 % bei km 51572, eingesteckt
    # nach 23 km, 18 kWh bis 80 % -> 45 %. Nur 4 Punkte Abstand, aber gefahren.
    db_session.get(models.Vehicle, config.vehicle_id).battery_capacity_kwh = 52.0
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=49, energy=23.0, km=51572),
        report(200, plugged=True, soc=None, energy=0.0, state="READY_FOR_CHARGING", km=None),
        report(400, plugged=True, soc=None, energy=10.0, state="CHARGING_HV_BATTERY", km=None),
        report(460, plugged=False, soc=80, energy=18.0, km=51595),
    ])
    assert len(sessions) == 1
    assert sessions[0].soc_start == 45
    assert "23 km vor dem Einstecken" in sessions[0].notes


def test_fresh_start_soc_is_not_touched_by_the_energy(db_session, config):
    # Kein km zwischen SoC-Bericht und Einstecken: der Wert ist frisch, auch
    # wenn die Energie (Kapazitaet zu klein eingetragen) etwas anderes sagt.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=60, energy=5.0, km=30000),
        report(15, plugged=True, soc=None, energy=0.0, state="CHARGING_HV_BATTERY", km=30000),
        report(200, plugged=False, soc=80, energy=20.0, km=30000),
    ])
    assert len(sessions) == 1
    assert sessions[0].soc_start == 60
    assert "nachgerechnet" not in sessions[0].notes


def test_without_odometer_only_large_gaps_are_corrected(db_session, config):
    # Ohne Kilometerstaende bleibt es bei 5 Punkten: 4 Punkte bleiben stehen.
    sessions = run(db_session, config, [
        report(0, plugged=False, soc=53, energy=23.0, km=None),
        report(200, plugged=True, soc=None, energy=0.0, state="CHARGING_HV_BATTERY", km=None),
        report(460, plugged=False, soc=80, energy=18.0, km=None),
    ])
    assert len(sessions) == 1
    assert sessions[0].soc_start == 53


@pytest.mark.parametrize("soc_start, soc_end, energy, capacity, driven, expected", [
    (77, 90, 23.0, None, 99, 77),     # ohne Akkukapazitaet keine Rechnung
    (77, 90, None, 58.0, 99, 77),     # ohne Zaehler keine Rechnung
    (20, 80, 49.0, 58.0, 99, 20),     # unter 0 %: Zaehler/Kapazitaet passen nicht
    (60, 80, 9.0, 58.0, None, 60),    # unbekannt gefahren, 4 Punkte: Rundung
    (48, 80, 18.0, 52.0, 10, 45),     # gefahren, 2,6 Punkte: korrigiert
    (40, 60, 5.0, 58.0, 50, 40),      # gerechneter Wert hoeher: nie nach oben
    (77, 90, 23.0, 58.0, 0, 77),      # nicht gefahren: frischer Wert bleibt
    (50, 80, 18.0, 58.0, 5, 50),      # gefahren, 1 Punkt: Rundung
])
def test_energy_based_start_guards(soc_start, soc_end, energy, capacity, driven, expected):
    corrected, _ = charge_detection.energy_based_start(
        soc_start, soc_end, energy, capacity, driven
    )
    assert corrected == expected
