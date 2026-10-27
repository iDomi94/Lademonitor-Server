"""Mehrverbrauch aufteilen in Tempo, Temperatur und Rest (drive_breakdown.py).

Die Faelle bauen Fahrten aus einem bekannten Modell und pruefen, dass die
Aufteilung genau das wieder herausgibt - und dass sie bei zu wenigen Daten
lieber nichts sagt.
"""

import random
from datetime import datetime, timedelta

import pytest

from app import models
from app.consumption import compute_vehicle_consumptions
from app.drive_breakdown import (
    MIN_DRIVES,
    Model,
    breakdowns_for_vehicle,
    collect_drives,
    fit,
    split,
)
from conftest import create_vehicle, register

BASE = datetime(2026, 3, 1, 8, 0, 0)

TRUTH = Model(
    base_consumption=11.0,
    aero=0.0007,
    base_load_kw=0.4,
    heat_kw_per_k=0.15,
    cool_kw_per_k=0.1,
    reference_speed_kmh=70.0,
    drive_count=0,
)


def build_sessions(drives, *, noise=0.0, seed=1):
    """drives: [(Tempo, Temperatur, km)]. Erster Vorgang ist nur der Anker."""
    rng = random.Random(seed)
    sessions = [
        models.ChargingSession(
            id="s0", vehicle_id="v1", start_time=BASE, odometer_km=10000,
            energy_kwh=20.0, energy_is_estimated=False, outside_temp_c=15.0,
        )
    ]
    odo = 10000
    for i, (speed, temp, km) in enumerate(drives, start=1):
        odo += km
        consumption = TRUTH.predict(speed, temp) + rng.gauss(0, noise)
        sessions.append(
            models.ChargingSession(
                id=f"s{i}", vehicle_id="v1", start_time=BASE + timedelta(days=3 * i),
                odometer_km=odo, energy_kwh=consumption * km / 100,
                energy_is_estimated=False, outside_temp_c=temp,
                outside_temp_source=models.TemperatureSource.WEATHER_DAILY,
                avg_speed_kmh=speed,
            )
        )
    return sessions


def varied_drives(count=40, seed=3):
    rng = random.Random(seed)
    return [
        (rng.uniform(30, 125), rng.uniform(-5, 30), rng.uniform(150, 350))
        for _ in range(count)
    ]


def drives_of(sessions):
    results = compute_vehicle_consumptions(sessions, None)
    return collect_drives(sessions, results)


def test_parts_add_up_to_the_measured_consumption():
    sessions = build_sessions(varied_drives(), noise=0.8)
    drives, _, _ = drives_of(sessions)
    model = fit(drives)
    assert model is not None
    for d in drives:
        part = split(model, d.speed_kmh, d.temp_c, d.consumption)
        assert part.base + part.speed + part.temperature + part.rest == pytest.approx(d.consumption)


def test_known_effects_are_recovered_from_enough_drives():
    """Aus 40 Fahrten ohne Rauschen muss die Aufteilung der Wahrheit nahe
    kommen - auch wenn die Vorgaben anders liegen als die Wahrheit."""
    drives, _, _ = drives_of(build_sessions(varied_drives()))
    model = fit(drives)

    # Die Motorbahnfahrt aus dem Anlass: 105 km/h bei 12 Grad.
    got = split(model, 105, 12, TRUTH.predict(105, 12))
    reference = model.reference_speed_kmh
    expected_speed = TRUTH.predict(105, 18) - TRUTH.predict(reference, 18)
    expected_temp = TRUTH.predict(105, 12) - TRUTH.predict(105, 18)
    assert got.speed == pytest.approx(expected_speed, abs=0.4)
    assert got.temperature == pytest.approx(expected_temp, abs=0.3)
    assert abs(got.rest) < 0.3


def test_heating_costs_more_per_km_in_slow_traffic():
    """Dieselbe Kaelte kostet je km mehr, wenn man langsam faehrt - die
    Heizleistung laeuft ja pro Stunde."""
    drives, _, _ = drives_of(build_sessions(varied_drives()))
    model = fit(drives)
    city = split(model, 30, 0, model.predict(30, 0))
    motorway = split(model, 110, 0, model.predict(110, 0))
    assert city.temperature > 2 * motorway.temperature > 0


def test_too_few_drives_give_no_breakdown():
    sessions = build_sessions(varied_drives(MIN_DRIVES - 1))
    results = compute_vehicle_consumptions(sessions, None)
    assert breakdowns_for_vehicle(sessions, results) == {}


def test_drives_without_speed_or_temperature_are_counted_not_guessed():
    sessions = build_sessions(varied_drives(8))
    sessions[2].avg_speed_kmh = None
    sessions[3].outside_temp_c = None
    sessions[4].avg_speed_kmh = 2.0  # unplausibel
    drives, without_speed, without_temp = drives_of(sessions)
    assert len(drives) == 5
    assert without_speed == 2
    assert without_temp == 1


def test_coefficients_never_turn_negative():
    """Ueber 24 Grad sparsamer als darunter waere physikalisch Unsinn - der
    Kuehlterm wird dann null statt negativ."""
    drives = [(80, 10, 200), (80, 15, 200), (80, 20, 200), (80, 28, 200), (80, 32, 200), (90, 12, 200)]
    sessions = build_sessions(drives)
    # Heisse Fahrten kuenstlich sparsamer machen.
    for s in sessions:
        if s.outside_temp_c and s.outside_temp_c > 25:
            s.energy_kwh *= 0.8
    found, _, _ = drives_of(sessions)
    model = fit(found)
    assert model.cool_kw_per_k == 0.0
    assert model.aero >= 0 and model.heat_kw_per_k >= 0 and model.base_load_kw >= 0


def test_session_api_carries_the_breakdown(client):
    register(client)
    vehicle = create_vehicle(client)
    odo = 10000
    start = BASE
    created = []
    for i, (speed, temp, km) in enumerate([(0, 15, 0)] + varied_drives(8)):
        odo += int(km)
        payload = {
            "vehicle_id": vehicle["id"],
            "start_time": (start + timedelta(days=2 * i)).isoformat(),
            "odometer_km": odo,
            "energy_kwh": 20.0 if i == 0 else round(TRUTH.predict(speed, temp) * int(km) / 100, 2),
            "outside_temp_c": round(temp, 1),
        }
        if i:
            payload["avg_speed_kmh"] = round(speed, 1)
        response = client.post("/api/sessions", json=payload)
        assert response.status_code == 201, response.text
        created.append(response.json())

    sessions = client.get("/api/sessions").json()
    with_breakdown = [s for s in sessions if s["consumption_breakdown"]]
    assert len(with_breakdown) == 8
    part = with_breakdown[0]["consumption_breakdown"]
    total = part["base"] + part["speed"] + part["temperature"] + part["rest"]
    assert total == pytest.approx(with_breakdown[0]["consumption_kwh_per_100km"], abs=0.01)

    stats = client.get("/api/stats/drive-breakdown").json()
    assert stats["model"]["drive_count"] == 8
    assert len(stats["drives"]) == 8
    assert stats["drives_without_speed"] == 0

    # Ein Wert ausserhalb 5..250 km/h wird abgelehnt.
    response = client.patch(f"/api/sessions/{created[1]['id']}", json={"avg_speed_kmh": 400})
    assert response.status_code == 422


def test_stats_need_a_vehicle_when_there_are_several(client):
    register(client)
    first = create_vehicle(client)
    second = create_vehicle(client, external_id="id3", name="ID.3", capacity=58.0)
    for vehicle in (first, second):
        client.post("/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-10-01T08:00:00"})
    stats = client.get("/api/stats/drive-breakdown").json()
    assert stats["needs_vehicle"] is True
    assert stats["model"] is None
