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
    TempPoint,
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


# ---------- Vertrag mit den Apps ----------
#
# iOS und Android kennen outside_temp_c (noch) nicht und schicken beim
# Speichern eines Ladevorgangs alle ihnen bekannten Felder. Dass der Wert
# dabei stehen bleibt, haengt allein an `exclude_unset` in
# routers/sessions.py::update_session - diese beiden Tests halten das fest.

import pytest
from conftest import create_vehicle, register


def test_patch_without_the_field_keeps_the_temperature(client):
    register(client)
    vehicle = create_vehicle(client)
    created = client.post("/api/sessions", json={
        "vehicle_id": vehicle["id"], "start_time": "2026-02-01T08:00:00",
        "outside_temp_c": -3.5, "energy_kwh": 20.0, "odometer_km": 10000,
    }).json()
    assert created["outside_temp_c"] == pytest.approx(-3.5)

    # Exakt der Payload, den die Apps heute schicken (siehe AddEditSession):
    # alle bekannten Felder, outside_temp_c ist nicht dabei.
    patched = client.patch(f"/api/sessions/{created['id']}", json={
        "provider_id": None, "location_id": None,
        "start_time": "2026-02-01T09:00:00",
        "charging_type": "AC", "soc_start": 30, "soc_end": 80,
        "energy_kwh": 21.0, "odometer_km": 10100,
        "price_total": 6.0, "price_per_kwh": 0.28,
        "latitude": None, "longitude": None, "notes": "von der App",
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["outside_temp_c"] == pytest.approx(-3.5), "App-Speichern hat den Wert geloescht!"


def test_explicit_null_does_clear_it(client):
    """Gegenprobe: wer das Feld AUSDRUECKLICH auf null setzt, loescht es auch -
    sonst waere der Wert von Hand nicht mehr zu entfernen."""
    register(client)
    vehicle = create_vehicle(client)
    created = client.post("/api/sessions", json={
        "vehicle_id": vehicle["id"], "start_time": "2026-02-01T08:00:00", "outside_temp_c": 5.0,
    }).json()

    patched = client.patch(f"/api/sessions/{created['id']}", json={"outside_temp_c": None})

    assert patched.json()["outside_temp_c"] is None


def test_a_weather_value_is_not_averaged_with_its_predecessor():
    """Ein Wetterdienstwert deckt den ganzen Fahrt-Zeitraum schon ab (er ist
    das Mittel der Tagstunden seit dem vorherigen Ladevorgang, siehe
    weather.py). Ihn nochmal mit dem Wert davor zu mitteln wuerde die
    Nachbarfahrt hineinmischen und die bessere Angabe verwaessern."""
    sessions = [
        session(0, temp=-10.0, odo=1000, kwh=30),
        session(1, temp=5.0, odo=1200, kwh=40),
    ]
    for row in sessions:
        row.outside_temp_source = models.TemperatureSource.WEATHER_DAILY

    points, _ = points_for(sessions)

    assert len(points) == 1
    # 5.0, nicht das Mittel -2.5 aus 5.0 und -10.0.
    assert points[0].temp_c == pytest.approx(5.0)


def test_a_vehicle_value_is_still_paired_with_its_predecessor():
    """Gegenprobe: ein Fahrzeugsensor misst punktuell beim Einstecken, also am
    ENDE der Fahrt - dort bleibt es bei der Paarung."""
    sessions = [
        session(0, temp=0.0, odo=1000, kwh=30),
        session(1, temp=10.0, odo=1200, kwh=40),
    ]
    for row in sessions:
        row.outside_temp_source = models.TemperatureSource.VEHICLE

    points, _ = points_for(sessions)

    assert points[0].temp_c == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Knickmodell: zwei Geraden statt einer
# ---------------------------------------------------------------------------


def _wanne(temp: float) -> float:
    """Verbrauch einer idealisierten Wanne: Knick bei 18 Grad.

    Unterhalb steigt er je Grad Kaelte um 0,25 kWh/100 km (Heizen), oberhalb je
    Grad Waerme um 0,10 (Kuehlen) - Heizen kostet deutlich mehr, genau die
    Asymmetrie, an der eine Parabel scheitern wuerde.
    """
    return 16.0 + (0.25 * (18 - temp) if temp < 18 else 0.10 * (temp - 18))


def _wannen_sessions() -> list[models.ChargingSession]:
    rows = []
    odo = 1000.0
    for index, temp in enumerate(
        [-5.0, 0.0, 3.0, 7.0, 11.0, 15.0, 19.0, 22.0, 26.0, 30.0, 33.0, 35.0]
    ):
        km = 200.0
        # Energie so waehlen, dass genau der gewuenschte Verbrauch herauskommt;
        # SoC_Ende konstant, damit der Korrekturterm der Kette wegfaellt.
        rows.append(
            session(index, temp=temp, odo=odo + km, kwh=_wanne(temp) * km / 100, soc_end=80)
        )
        odo += km
    # Der erste Vorgang liefert nur den Ausgangs-Kilometerstand.
    rows.insert(0, session(99, temp=None, odo=1000.0, kwh=10.0, soc_end=80))
    rows.sort(key=lambda r: r.start_time)
    return rows


def test_a_u_shape_is_fitted_with_two_lines_not_one():
    """Eine Gerade presst Heizen und Kuehlen in eine Steigung und mittelt sie
    gegeneinander weg. Genau deshalb gibt es das Knickmodell."""
    points, _ = points_for(_wannen_sessions())

    trend = build_trend(points)

    assert trend is not None
    assert trend.model == "breakpoint"
    # Der gesuchte Knick liegt beim Minimum der Wanne.
    assert trend.breakpoint_c == pytest.approx(18.0, abs=1.5)
    assert trend.slope_cold < 0 and trend.slope_warm > 0
    # Eine Gerade wuerde hier deutlich schlechter passen.
    assert trend.r2 > 0.9


def test_the_curve_has_three_vertices_and_stays_inside_the_data():
    """Gezeichnet wird nur ueber den gemessenen Bereich - eine bis 0 Grad
    verlaengerte Kurve liesse eine Hochrechnung wie eine Messung aussehen."""
    points, _ = points_for(_wannen_sessions())

    trend = build_trend(points)

    assert len(trend.curve) == 3
    temps = [v.temp_c for v in trend.curve]
    assert temps[0] == pytest.approx(min(p.temp_c for p in points))
    assert temps[-1] == pytest.approx(max(p.temp_c for p in points))
    assert temps[0] < temps[1] < temps[2]


def test_a_purely_linear_relation_stays_a_single_line():
    """Zwei zusaetzliche Parameter passen IMMER besser. Ohne die Huerde waere
    das Ergebnis nie wieder eine Gerade."""
    rows = [session(99, temp=None, odo=1000.0, kwh=10.0, soc_end=80)]
    odo = 1000.0
    for index, temp in enumerate([-5.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0]):
        km = 200.0
        consumption = 22.0 - 0.2 * temp
        rows.append(session(index, temp=temp, odo=odo + km, kwh=consumption * km / 100, soc_end=80))
        odo += km
    rows.sort(key=lambda r: r.start_time)

    trend = build_trend(points_for(rows)[0])

    assert trend.model == "linear"
    assert trend.breakpoint_c is None
    assert trend.slope < 0


def test_data_without_a_cold_branch_stays_a_single_line():
    """Genau der Fall des Nutzers im ersten Jahr: gemessen ab Maerz, kaeltester
    Wert 5,7 Grad. Fuer einen kalten Ast fehlen schlicht die Fahrten - dann
    darf das Modell auch keinen behaupten."""
    rows = [session(99, temp=None, odo=1000.0, kwh=10.0, soc_end=80)]
    odo = 1000.0
    for index, temp in enumerate([16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0, 30.0]):
        km = 200.0
        rows.append(
            session(index, temp=temp, odo=odo + km, kwh=_wanne(temp) * km / 100, soc_end=80)
        )
        odo += km
    rows.sort(key=lambda r: r.start_time)

    trend = build_trend(points_for(rows)[0])

    assert trend.model == "linear"


def test_the_extrapolation_flag_marks_a_winter_that_was_never_measured():
    """Die Kennzahl "bei 0 statt 20 Grad" ist dann eine Hochrechnung. Ohne
    diesen Hinweis liest man sie als Messung."""
    points, _ = points_for(_wannen_sessions())
    assert build_trend(points).at_0c_is_extrapolated is False

    rows = [session(99, temp=None, odo=1000.0, kwh=10.0, soc_end=80)]
    odo = 1000.0
    for index, temp in enumerate([6.0, 10.0, 14.0, 18.0, 22.0, 26.0, 30.0]):
        km = 200.0
        rows.append(
            session(index, temp=temp, odo=odo + km, kwh=_wanne(temp) * km / 100, soc_end=80)
        )
        odo += km
    rows.sort(key=lambda r: r.start_time)

    assert build_trend(points_for(rows)[0]).at_0c_is_extrapolated is True


def test_the_simple_line_is_reported_even_when_the_breakpoint_model_wins():
    """slope/intercept beschreiben IMMER die Gerade - aeltere Apps zeichnen
    damit weiter eine plausible Linie, statt gar keine."""
    points, _ = points_for(_wannen_sessions())

    trend = build_trend(points)

    assert trend.model == "breakpoint"
    assert trend.slope != 0.0
    # Die Gerade durch eine Wanne mit laengerem kalten Ast faellt.
    assert trend.intercept > 0


# Die echten Fahrten des Nutzers, Stand 09/2026: (Temperatur, kWh/100 km,
# Kilometer). Gemessen ab Maerz, kaeltester Wert 5,7 Grad - es gibt also gar
# keinen Winter in den Daten, nur Streuung zwischen 6 und 30 Grad.
REAL_DRIVES = [
    (9.1, 13.06, 432.0), (5.7, 18.01, 348.0), (12.6, 17.46, 282.0), (12.7, 25.83, 82.0),
    (11.6, 14.45, 158.0), (15.1, 19.08, 246.0), (23.7, 17.16, 186.0), (15.9, 26.19, 52.0),
    (9.8, 16.29, 298.0), (16.2, 17.29, 283.0), (24.1, 17.34, 116.0), (22.8, 16.89, 330.0),
    (20.6, 17.26, 110.0), (18.5, 15.16, 218.0), (24.6, 21.84, 58.0), (30.2, 24.28, 81.0),
    (22.4, 18.29, 246.0), (24.5, 18.62, 322.0), (24.4, 16.61, 300.0), (26.5, 20.98, 175.0),
    (24.3, 12.37, 274.0), (20.1, 17.08, 120.0), (20.0, 17.44, 215.0), (25.7, 19.06, 123.0),
    (28.1, 19.63, 317.0), (27.0, 17.15, 144.0), (24.6, 18.59, 127.0), (28.2, 12.44, 130.0),
    (30.2, 38.5, 6.0), (23.4, 17.42, 379.0), (21.7, 18.4, 113.0), (17.8, 18.27, 367.0),
    (22.4, 15.88, 95.0), (22.6, 12.43, 316.0), (19.4, 15.86, 145.0), (19.9, 18.61, 269.0),
    (17.5, 15.0, 272.0),
]


def test_a_year_without_winter_does_not_get_a_breakpoint():
    """Regression an echten Daten: ohne kalte Fahrten darf kein kalter Ast
    behauptet werden.

    Mit den urspruenglichen Huerden (5 K je Ast, 0,05 r2-Gewinn) legte das
    Modell den Knick genau hier auf 23 Grad, machte aus dem gesamten Bereich
    5,7-23 Grad einen praktisch waagerechten "kalten" Ast und aus den zwei
    heissesten Fahrten einen steilen "warmen" - formal besser (r2 0,08 statt
    0,03), inhaltlich zwei Ausreisser mit einer Geschichte drumherum.
    """
    points = [
        TempPoint(f"s{i}", BASE + timedelta(days=i), temp, consumption, km,
                  "soc_corrected", "summer")
        for i, (temp, consumption, km) in enumerate(REAL_DRIVES)
    ]

    trend = build_trend(points)

    assert trend.model == "linear"
    assert trend.breakpoint_c is None
    # Und der Hinweis, dass die Kennzahl fuer 0 Grad eine Hochrechnung ist.
    assert trend.at_0c_is_extrapolated is True
