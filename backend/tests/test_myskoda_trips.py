"""Ø-Geschwindigkeit aus der MySkoda-Fahrtenstatistik (`myskoda_trips.py`).

Format wie der echte Export vom 27.10.2026 (englische Spalten, lokale Zeit,
neueste Fahrt zuerst, einzelne Fahrten ohne Start-km-Stand).
"""

import json

import pytest

from app import myskoda_trips
from conftest import create_vehicle, register

HEADER = (
    '"Start of trip","End of trip","Classification","Start address","End address",'
    '"Start coordinates","End coordinates","Distance in km","Start odometer in km",'
    '"End odometer in km","Driving time in minutes","Travel time in minutes",'
    '"Average speed in km/h","Electricity consumption in kWh",'
    '"Average electricity consumption in kWh/100km","Start SoC in %","End SoC in %",'
    '"Public charging stops","Public charge SoC gained in %","Short trip"'
)


def _csv(*trips) -> bytes:
    """trips: (start, end, km, odo_start, odo_end, minutes)"""
    lines = [HEADER]
    for start, end, km, odo_start, odo_end, minutes in reversed(trips):
        speed = round(km / (minutes / 60)) if minutes else 0
        lines.append(
            f'"{start}","{end}","","","","","","{km}","{odo_start}","{odo_end}",'
            f'"{minutes}","{minutes}","{speed}","0.0","0.0","","","0","0","false"'
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _session(client, vehicle_id, **fields):
    response = client.post("/api/sessions", json={"vehicle_id": vehicle_id, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def _preview(client, vehicle_id, content):
    response = client.post(
        f"/api/import/myskoda/preview?vehicle_id={vehicle_id}",
        files={"file": ("tripStatistics.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client, vehicle_id, content, accept):
    response = client.post(
        f"/api/import/myskoda/commit?vehicle_id={vehicle_id}",
        files={"file": ("tripStatistics.csv", content, "text/csv")},
        data={"accept": json.dumps(accept)},
    )
    assert response.status_code == 200, response.text
    return response.json()


TRIPS = _csv(
    ("01.10.2026 08:00", "01.10.2026 08:30", 20, 1000, 1020, 30),
    ("02.10.2026 07:00", "02.10.2026 07:10", 0, 1020, 1020, 10),  # Rangieren
    ("03.10.2026 09:00", "03.10.2026 11:00", 200, "", 1220, 120),  # ohne Start-km
    ("05.10.2026 10:00", "05.10.2026 12:00", 180, 1220, 1400, 120),  # mit Ladestopp bei 1300
    ("06.10.2026 08:00", "06.10.2026 08:20", 10, 1400, 1410, 20),
)


def test_trip_export_is_recognised_by_its_columns():
    assert myskoda_trips.is_trip_export(TRIPS)
    assert not myskoda_trips.is_trip_export(b'"Session ID","Begonnen am"\n')
    trips = myskoda_trips.parse_trips(TRIPS)
    assert len(trips) == 5
    assert trips[0].start.isoformat() == "2026-10-01T08:00:00"
    assert trips[2].odo_from == 1020  # aus Ende minus Strecke


def test_speed_is_total_km_over_total_driving_time(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    target = _session(client, vehicle["id"], start_time="2026-10-04T18:00:00", odometer_km=1220)

    preview = _preview(client, vehicle["id"], TRIPS)
    assert preview["kind"] == "trips"
    update = next(u for u in preview["updates"] if u["session_id"] == target["id"])
    # 220 km in 30 + 10 + 120 min = 160 min -> 82,5 km/h (nicht das Mittel der
    # einzelnen Ø-Werte 40/0/100).
    assert update["changes"] == [{"field": "avg_speed_kmh", "old": None, "new": 82.5}]
    assert update["trips"] == {"count": 3, "km": 220.0, "minutes": 160}


def test_trip_with_a_charging_stop_is_split_by_km(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-10-04T18:00:00", odometer_km=1220)
    stop = _session(client, vehicle["id"], start_time="2026-10-05T11:00:00", odometer_km=1300)
    after = _session(client, vehicle["id"], start_time="2026-10-06T18:00:00", odometer_km=1410)

    updates = {u["session_id"]: u for u in _preview(client, vehicle["id"], TRIPS)["updates"]}
    # 80 von 180 km der Fahrt am 05.10. liegen vor dem Ladestopp.
    assert updates[stop["id"]]["trips"]["km"] == pytest.approx(80)
    assert updates[stop["id"]]["changes"][0]["new"] == 90.0
    # 100 km Rest in 66,7 min plus 10 km in 20 min.
    assert updates[after["id"]]["trips"]["km"] == pytest.approx(110)
    assert updates[after["id"]]["changes"][0]["new"] == pytest.approx(76.2, abs=0.1)


def test_missing_odometer_is_filled_from_the_last_trip_and_then_used(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    target = _session(client, vehicle["id"], start_time="2026-10-03T11:05:00")

    update = _preview(client, vehicle["id"], TRIPS)["updates"][0]
    assert update["session_id"] == target["id"]
    fields = {c["field"]: c["new"] for c in update["changes"]}
    assert fields == {"odometer_km": 1220, "avg_speed_kmh": 82.5}


def test_no_odometer_while_a_trip_runs_over_the_charge_start(client):
    register(client)
    vehicle = create_vehicle(client)
    # Mitten in der Fahrt am 03.10. - der Stand beim Einstecken ist unbekannt.
    _session(client, vehicle["id"], start_time="2026-10-03T10:00:00")
    # Nach der letzten Fahrt - danach kann noch gefahren worden sein.
    _session(client, vehicle["id"], start_time="2026-10-07T10:00:00")
    # Import ohne Uhrzeit bekommt nie einen km-Stand geraten.
    _session(client, vehicle["id"], start_time="2026-10-02T00:00:00")
    assert _preview(client, vehicle["id"], TRIPS)["updates"] == []


def test_existing_speed_stays_and_gaps_are_hinted(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    _session(client, vehicle["id"], start_time="2026-10-04T18:00:00", odometer_km=1220, avg_speed_kmh=70)
    # 1220 -> 1600: nur 190 von 380 km in der Datei, ausserdem jenseits ihres Endes.
    _session(client, vehicle["id"], start_time="2026-10-08T18:00:00", odometer_km=1600)
    # Vor der Datei: kein Wert, kein Hinweis.
    _session(client, vehicle["id"], start_time="2026-09-01T18:00:00", odometer_km=500)

    preview = _preview(client, vehicle["id"], TRIPS)
    assert preview["updates"] == []
    assert preview["unchanged"] == 1


def test_low_coverage_gives_a_hint_instead_of_a_value(client):
    register(client)
    vehicle = create_vehicle(client)
    gappy = _csv(
        ("01.10.2026 08:00", "01.10.2026 08:30", 20, 1000, 1020, 30),
        ("03.10.2026 09:00", "03.10.2026 10:00", 50, 1150, 1200, 60),
    )
    _session(client, vehicle["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    _session(client, vehicle["id"], start_time="2026-10-04T18:00:00", odometer_km=1200)
    preview = _preview(client, vehicle["id"], gappy)
    assert preview["updates"] == []
    assert preview["hints"][0]["kind"] == "trip_gap"
    assert preview["hints"][0]["data"] == {"pct": 35, "km": 200}


def test_commit_writes_only_accepted_and_notes_it(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    target = _session(client, vehicle["id"], start_time="2026-10-04T18:00:00", odometer_km=1220)
    other = _session(client, vehicle["id"], start_time="2026-10-06T18:00:00", odometer_km=1410)

    result = _commit(client, vehicle["id"], TRIPS, [f"trips:{target['id']}"])
    assert result == {"updated": 1, "created": 0}
    sessions = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[target["id"]]["avg_speed_kmh"] == 82.5
    assert "MySkoda-Fahrten: Ø km/h 82.5 (3 Fahrten, 220 km)" in sessions[target["id"]]["notes"]
    assert sessions[other["id"]]["avg_speed_kmh"] is None

    # Zweiter Lauf: der Wert steht, es gibt nichts mehr zu tun.
    preview = _preview(client, vehicle["id"], TRIPS)
    assert [u["session_id"] for u in preview["updates"]] == [other["id"]]


def test_other_users_sessions_are_not_touched(client):
    register(client, username="alice")
    vehicle_a = create_vehicle(client)
    _session(client, vehicle_a["id"], start_time="2026-09-30T18:00:00", odometer_km=1000)
    _session(client, vehicle_a["id"], start_time="2026-10-04T18:00:00", odometer_km=1220)
    client.cookies.clear()
    register(client, username="bob")
    response = client.post(
        f"/api/import/myskoda/preview?vehicle_id={vehicle_a['id']}",
        files={"file": ("tripStatistics.csv", TRIPS, "text/csv")},
    )
    assert response.status_code == 404
