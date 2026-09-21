"""Verbrauch gegen Aussentemperatur (temperature.py).

Die Auswertung trifft eine Aussage ueber Geld und Reichweite ("im Winter
X % mehr"), sie muss also erkennbar falsche Grundlagen ablehnen statt eine
Zahl zu erfinden. Genau darum drehen sich die meisten Faelle hier.
"""

from datetime import datetime, timedelta

import pytest

from app import models
from app.temperature import (
    BUCKET_WIDTH_C,
    build_buckets,
    build_seasons,
    build_trend,
    collect_points,
)

BASE = datetime(2026, 1, 1, 8, 0, 0)


def session(idx, *, temp=None, odo=None, kwh=None, month=None, soc_end=None):
    start = BASE + timedelta(days=idx)
    if month is not None:
        start = start.replace(month=month)
    return models.ChargingSession(
        id=f"s{idx}",
        vehicle_id="v1",
        start_time=start,
        outside_temp_c=temp,
        odometer_km=odo,
        energy_kwh=kwh,
        soc_end=soc_end,
        energy_is_estimated=False,
    )


def points_for(sessions, capacity=77.0):
    return collect_points(sessions, {"v1": capacity})


def test_temperature_of_a_drive_is_the_mean_of_both_ends():
    """Der Verbrauch eines Vorgangs beschreibt die Fahrt DAVOR. Deren
    Temperatur liegt zwischen der beim vorigen Einstecken und der bei diesem -
    deshalb das Mittel, nicht einer der beiden Randwerte."""
    sessions = [session(0, temp=0.0, odo=1000, kwh=30), session(1, temp=10.0, odo=1200, kwh=40)]

    points, _ = points_for(sessions)

    assert len(points) == 1
    assert points[0].temp_c == pytest.approx(5.0)


def test_without_a_predecessor_temperature_the_own_value_is_used():
    sessions = [session(0, temp=None, odo=1000, kwh=30), session(1, temp=12.0, odo=1200, kwh=40)]

    points, _ = points_for(sessions)

    assert points[0].temp_c == pytest.approx(12.0)


def test_sessions_without_own_temperature_are_counted_not_guessed():
    """Fehlt die Temperatur am Ende der Fahrt, wird der Startwert NICHT
    ersatzweise genommen - zwischen dem Einstecken davor und dieser Fahrt
    koennen Tage liegen. Stattdessen faellt der Punkt heraus und wird gezaehlt,
    damit die Oberflaeche die duenne Grundlage zeigen kann."""
    sessions = [session(0, temp=5.0, odo=1000, kwh=30), session(1, temp=None, odo=1200, kwh=40)]

    points, without_temp = points_for(sessions)

    assert points == []
    assert without_temp == 1


def test_sessions_without_computable_consumption_are_not_counted_as_missing_temp():
    """Ohne Kilometerstand gibt es gar keinen Verbrauch - so ein Vorgang fehlt
    der Auswertung nicht wegen der Temperatur und darf den Zaehler nicht
    hochtreiben, sonst liest sich die Anzeige wie ein Datenproblem."""
    sessions = [session(0, temp=5.0, odo=None, kwh=30), session(1, temp=5.0, odo=None, kwh=40)]

    points, without_temp = points_for(sessions)

    assert points == []
    assert without_temp == 0


def test_buckets_round_towards_minus_infinity():
    """-3 Grad gehoert in die Klasse -5..0, nicht in 0..5. Klassisches
    Vorzeichen-Problem beim Abrunden - und im Winter genau der Bereich, um
    den es geht."""
    sessions = [
        session(0, temp=-4.0, odo=1000, kwh=30),
        session(1, temp=-2.0, odo=1200, kwh=40),
    ]

    points, _ = points_for(sessions)
    buckets = build_buckets(points)

    assert len(buckets) == 1
    assert (buckets[0].from_c, buckets[0].to_c) == (-5, 0)


def test_buckets_average_is_weighted_by_kilometres():
    """Eine 20-km-Kurzstrecke darf eine 400-km-Fahrt in derselben Klasse nicht
    gleichberechtigt verwaessern - dieselbe Regel wie beim Monatsdurchschnitt
    in stats.py."""
    sessions = [
        session(0, temp=10.0, odo=1000, kwh=10),
        session(1, temp=10.0, odo=1020, kwh=4),     # 20 km, 20 kWh/100km
        session(2, temp=10.0, odo=1420, kwh=40),    # 400 km, 10 kWh/100km
    ]

    points, _ = points_for(sessions)
    buckets = build_buckets(points)

    assert len(buckets) == 1
    # Ungewichtet waeren es 15.0; km-gewichtet liegt der Wert nahe bei 10.
    assert buckets[0].avg_consumption < 11.0
    assert buckets[0].session_count == 2


def test_trend_finds_the_expected_slope():
    """Kuenstliche Daten mit genau 0.1 kWh/100km je Grad: die Gerade muss das
    wiederfinden, sonst stimmt die Rechnung nicht."""
    sessions = [session(0, temp=-10.0, odo=1000, kwh=20)]
    odo = 1000
    for i, temp in enumerate([-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0], start=1):
        # Verbrauch = 20 - 0.1 * Temperatur, ueber je 100 km
        consumption = 20 - 0.1 * temp
        odo += 100
        sessions.append(session(i, temp=temp, odo=odo, kwh=consumption))

    points, _ = points_for(sessions, capacity=None)
    trend = build_trend(points)

    assert trend is not None
    assert trend.slope == pytest.approx(-0.1, abs=0.02)
    assert trend.r2 > 0.9
    assert trend.extra_pct_at_0c > 0


def test_no_trend_from_too_few_points():
    sessions = [
        session(0, temp=0.0, odo=1000, kwh=20),
        session(1, temp=20.0, odo=1100, kwh=18),
    ]

    points, _ = points_for(sessions)

    assert build_trend(points) is None


def test_no_trend_from_a_narrow_temperature_range():
    """Acht Punkte, aber alle zwischen 18 und 20 Grad: daraus laesst sich
    keine Aussage ueber den Winter ableiten, also gibt es keine Gerade."""
    sessions = [session(0, temp=19.0, odo=1000, kwh=20)]
    odo = 1000
    for i in range(1, 9):
        odo += 100
        sessions.append(session(i, temp=19.0 + (i % 3) * 0.5, odo=odo, kwh=18 + i * 0.1))

    points, _ = points_for(sessions)

    assert len(points) >= 5
    assert build_trend(points) is None


def test_seasons_are_grouped_and_ordered():
    sessions = [
        session(0, temp=0.0, odo=1000, kwh=20, month=1),
        session(1, temp=0.0, odo=1100, kwh=22, month=1),    # Winter
        session(2, temp=20.0, odo=1200, kwh=16, month=7),   # Sommer
    ]

    points, _ = points_for(sessions)
    seasons = build_seasons(points)

    names = [s.season for s in seasons]
    assert names == ["winter", "summer"]
    assert all(s.session_count >= 1 for s in seasons)


def test_bucket_width_is_exposed_for_the_ui():
    assert BUCKET_WIDTH_C == 5


# ---------- Endpunkt ----------

def _post_session(client, vehicle_id, day, *, temp, odo, kwh):
    response = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle_id,
            "start_time": datetime(2026, 1, day, 8, 0, 0).isoformat(),
            "outside_temp_c": temp,
            "odometer_km": odo,
            "energy_kwh": kwh,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_endpoint_returns_points_buckets_and_trend(client):
    from conftest import create_vehicle, register

    register(client)
    vehicle = create_vehicle(client)
    odo = 10000
    for day, temp in enumerate([-8.0, -2.0, 4.0, 10.0, 16.0, 22.0, 26.0, 18.0, 6.0], start=1):
        odo += 150
        _post_session(client, vehicle["id"], day, temp=temp, odo=odo, kwh=20 - 0.1 * temp)

    body = client.get("/api/stats/temperature").json()

    assert len(body["points"]) >= 7
    assert body["bucket_width_c"] == 5
    assert body["trend"]["slope"] < 0          # kaelter = mehr Verbrauch
    assert body["trend"]["extra_pct_at_0c"] > 0
    assert {b["from_c"] for b in body["buckets"]}     # mindestens eine Klasse


def test_endpoint_reports_sessions_without_temperature(client):
    from conftest import create_vehicle, register

    register(client)
    vehicle = create_vehicle(client)
    _post_session(client, vehicle["id"], 1, temp=5.0, odo=10000, kwh=20)
    _post_session(client, vehicle["id"], 2, temp=None, odo=10200, kwh=20)

    body = client.get("/api/stats/temperature").json()

    assert body["points"] == []
    assert body["sessions_without_temp"] == 1
    assert body["trend"] is None


def test_endpoint_is_empty_without_data(client):
    from conftest import register

    register(client)

    body = client.get("/api/stats/temperature").json()

    assert body["points"] == []
    assert body["buckets"] == []
    assert body["seasons"] == []
    assert body["trend"] is None


def test_temperature_survives_the_auto_push(client):
    """Der Home-Assistant-Push ist der Hauptweg, auf dem die Temperatur
    ueberhaupt in die Datenbank kommt."""
    from conftest import create_vehicle, register

    register(client)
    create_vehicle(client, external_id="enyaq")

    response = client.post(
        "/api/sessions/auto",
        json={
            "vehicle_external_id": "enyaq",
            "external_session_id": "ha-1",
            "start_time": "2026-02-01T18:00:00",
            "soc_start": 30,
            "soc_end": 80,
            "odometer_km": 12000,
            "outside_temp_c": -3.5,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["outside_temp_c"] == pytest.approx(-3.5)


def test_auto_push_tolerates_an_unusable_sensor_value(client):
    """In Home Assistant ist 'unknown' bei einem kurz fehlenden Sensor der
    Normalfall - der komplette Push darf daran nicht scheitern, genau wie bei
    der Lade-Art."""
    from conftest import create_vehicle, register

    register(client)
    create_vehicle(client, external_id="enyaq")

    response = client.post(
        "/api/sessions/auto",
        json={
            "vehicle_external_id": "enyaq",
            "external_session_id": "ha-2",
            "start_time": "2026-02-02T18:00:00",
            "outside_temp_c": "unknown",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["outside_temp_c"] is None


def test_implausible_temperature_is_rejected(client):
    """-60..60 Grad: soll einen vertauschten Wert oder Fahrenheit abfangen."""
    from conftest import create_vehicle, register

    register(client)
    vehicle = create_vehicle(client)

    response = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle["id"],
            "start_time": "2026-02-03T08:00:00",
            "outside_temp_c": 104,
        },
    )

    assert response.status_code == 422
