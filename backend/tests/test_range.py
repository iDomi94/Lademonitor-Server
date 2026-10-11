"""Reale Reichweite je Temperatur (range_estimate.py).

Die Apps rechnen ihren Reichweitenrechner aus `range.curve` - die Kurve muss
also ab Akku stimmen, sonst ist jede Reichweite um den Ladeverlust zu kurz.
"""

from datetime import datetime, timedelta

import pytest
from conftest import create_vehicle, register

from app import models
from app import range_estimate as re_
from app.temperature import CurveVertex, TempPoint, Trend

BASE = datetime(2026, 1, 1, 8, 0, 0)


def _trend(**fields):
    base = dict(
        slope=-0.2, intercept=20.0, r2=0.8, consumption_at_0c=20.0, consumption_at_20c=16.0,
        extra_pct_at_0c=25.0, model="linear", curve=[],
    )
    base.update(fields)
    return Trend(**base)


def _point(idx, temp, consumption, method="soc_corrected"):
    return TempPoint(
        session_id=f"s{idx}", start_time=BASE + timedelta(days=idx), temp_c=temp,
        consumption=consumption, km=150.0, method=method, season="winter",
    )


def _session(idx, **fields):
    base = dict(id=f"s{idx}", vehicle_id="v1", start_time=BASE + timedelta(days=idx),
                energy_is_estimated=False)
    base.update(fields)
    return models.ChargingSession(**base)


def test_predict_linear_and_breakpoint_outside_the_data():
    assert re_.predict(_trend(), -10) == pytest.approx(22.0)
    knick = _trend(
        model="breakpoint", breakpoint_c=18.0, slope_cold=-0.3, slope_warm=0.1,
        curve=[CurveVertex(0.0, 20.4), CurveVertex(18.0, 15.0), CurveVertex(30.0, 16.2)],
    )
    assert re_.predict(knick, -10) == pytest.approx(15.0 + 0.3 * 28)
    assert re_.predict(knick, 38) == pytest.approx(15.0 + 0.1 * 20)


def test_only_charger_measured_drives_lose_the_charging_loss():
    sessions = {
        "s0": _session(0),
        "s1": _session(1, energy_is_estimated=True),
        "s2": _session(2, energy_meter="vehicle"),
    }
    points = [
        _point(0, 5, 22.0),
        _point(1, 5, 20.0, method="estimated_energy"),
        _point(2, 5, 20.0),
    ]
    out = re_.battery_side_points(points, sessions, {}, 0.10)
    assert out[0].consumption == pytest.approx(20.0)
    assert out[1].consumption == 20.0
    assert out[2].consumption == 20.0


def test_overall_loss_needs_three_measured_sessions_and_is_never_negative():
    # 77 kWh Nennkapazitaet, 50 % Hub -> 38,5 kWh ohne Verlust.
    measured = [_session(i, soc_start=20, soc_end=70, energy_kwh=42.35) for i in range(3)]
    assert re_.overall_loss(measured, 77.0, {}) == pytest.approx(0.10)
    assert re_.overall_loss(measured[:2], 77.0, {}) is None
    below = [_session(i, soc_start=20, soc_end=70, energy_kwh=37.0) for i in range(3)]
    assert re_.overall_loss(below, 77.0, {}) == 0.0
    assert re_.overall_loss(measured, None, {}) is None


def _drives(temps, loss=0.0):
    """Fahrten mit bekanntem Verbrauch ab Akku: 18 - 0.2 * T, an der Saeule
    um `loss` hoeher gemessen."""
    return [_point(i, t, (18.0 - 0.2 * t) * (1 + loss)) for i, t in enumerate(temps)]


def test_range_rows_mark_extrapolation_and_remove_the_loss():
    temps = [4.0, 6.0, 9.0, 12.0, 15.0, 18.0, 22.0, 25.0]
    points = _drives(temps, loss=0.10)
    sessions = {p.session_id: _session(int(p.session_id[1:])) for p in points}
    # Drei Ladungen mit 10 % Verlust, damit der Verlust bekannt ist.
    for i in range(3):
        sessions[f"x{i}"] = _session(100 + i, id=f"x{i}", soc_start=20, soc_end=70, energy_kwh=42.35)
    vehicle = models.Vehicle(id="v1", battery_capacity_kwh=77.0)

    estimate = re_.build_range(points, sessions, vehicle, {})

    assert estimate.loss_pct == pytest.approx(10.0)
    rows = {r.temp_c: r for r in estimate.rows}
    assert rows[0].extrapolated and not rows[20].extrapolated
    assert rows[20].consumption == pytest.approx(14.0, abs=0.05)
    assert rows[20].range_km == round(77 / 14.0 * 100)
    assert [t for t, _ in estimate.curve][:1] == [-20]
    assert estimate.measured_from_c == 4.0


def test_without_known_loss_nothing_is_removed():
    points = _drives([4.0, 6.0, 9.0, 12.0, 15.0, 18.0, 22.0, 25.0], loss=0.10)
    sessions = {p.session_id: _session(int(p.session_id[1:])) for p in points}
    estimate = re_.build_range(points, sessions, models.Vehicle(id="v1", battery_capacity_kwh=77.0), {})
    assert estimate.loss_pct is None
    assert {r.temp_c: r for r in estimate.rows}[20].consumption == pytest.approx(15.4, abs=0.05)


def test_no_range_without_capacity():
    points = _drives([4.0, 6.0, 9.0, 12.0, 15.0, 18.0, 22.0, 25.0])
    sessions = {p.session_id: _session(int(p.session_id[1:])) for p in points}
    assert re_.build_range(points, sessions, models.Vehicle(id="v1"), {}) is None


# ---------- API ----------


def _post(client, vehicle_id, day, temp, odo, kwh):
    response = client.post("/api/sessions", json={
        "vehicle_id": vehicle_id, "start_time": datetime(2026, 1, day, 8).isoformat(),
        "outside_temp_c": temp, "odometer_km": odo, "energy_kwh": kwh,
    })
    assert response.status_code == 201, response.text


def _fill(client, vehicle_id):
    odo = 10000
    for day, temp in enumerate([-8.0, -2.0, 4.0, 10.0, 16.0, 22.0, 26.0, 18.0, 6.0], start=1):
        odo += 150
        _post(client, vehicle_id, day, temp, odo, 30 - 0.15 * temp)


def test_endpoint_returns_range_and_full_trend(client):
    register(client)
    vehicle = create_vehicle(client)
    _fill(client, vehicle["id"])

    body = client.get("/api/stats/temperature").json()

    assert body["trend"]["curve"], "die Kurve muss mit in die Antwort"
    rng = body["range"]
    assert rng["vehicle_id"] == vehicle["id"] and rng["capacity_kwh"] == 77.0
    temps = [r["temp_c"] for r in rng["rows"]]
    assert 0 in temps and 20 in temps
    assert all(r["range_km"] > 0 for r in rng["rows"])
    assert len(rng["curve"]) == 61


def test_endpoint_has_no_range_for_two_vehicles_without_filter(client):
    register(client)
    a = create_vehicle(client)
    b = create_vehicle(client, external_id="zweiter", name="Zweiter")
    _fill(client, a["id"])
    _fill(client, b["id"])

    assert client.get("/api/stats/temperature").json()["range"] is None
    filtered = client.get(f"/api/stats/temperature?vehicle_id={b['id']}").json()
    assert filtered["range"]["vehicle_id"] == b["id"]
