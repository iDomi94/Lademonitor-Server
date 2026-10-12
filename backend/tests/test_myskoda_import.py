"""Abgleich mit dem Ladeverlauf der MySkoda-App (`myskoda_import.py`).

Die Faelle sind den echten Daten des Nutzers nachgebaut (Abgleich vom
11.10.2026): ein Spritmonitor-Import mit dem falschen Start-SoC 20 aus dem
App-Bearbeitungsformular, ein Sammeleintrag ueber zwei Ladungen, automatisch
erfasste Vorgaenge, die zwei Stunden zu frueh stehen, ein verpasster
Ladebeginn des Pollers und Ladungen, zu denen es gar keinen Vorgang gibt.

Die Zeiten in den Test-CSVs tragen keine Zone und gelten damit als lokale
Zeit - so haengen die Tests nicht an der Zeitzone des Rechners. Die
UTC-Umrechnung hat einen eigenen Test mit umgestellter Prozess-Zeitzone.
"""

import json
import time
from datetime import datetime

import pytest

from app import myskoda_import
from conftest import create_vehicle, register

HEADER = (
    '"Session ID","Begonnen am","Beendet am","Ladezeit (s)","Tatsächliche Ladezeit (s)",'
    '"Gesamtenergie (kWh)","Start SOC (%)","Ende SOC (%)"'
)


def _csv(*rows) -> bytes:
    """rows: (id, start, end, active_s, kwh, soc_start, soc_end)"""
    lines = [HEADER]
    for charge_id, start, end, active_s, kwh, soc_start, soc_end in rows:
        lines.append(
            f'"{charge_id}","{start}","{end}","{active_s}","{active_s}","{kwh}","{soc_start}","{soc_end}"'
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _session(client, vehicle_id, **fields):
    payload = {"vehicle_id": vehicle_id, **fields}
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _preview(client, vehicle_id, content):
    response = client.post(
        f"/api/import/myskoda/preview?vehicle_id={vehicle_id}",
        files={"file": ("ladestatistik.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client, vehicle_id, content, accept):
    response = client.post(
        f"/api/import/myskoda/commit?vehicle_id={vehicle_id}",
        files={"file": ("ladestatistik.csv", content, "text/csv")},
        data={"accept": json.dumps(accept)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _get(client, session_id):
    response = client.get("/api/sessions")
    assert response.status_code == 200, response.text
    return next(s for s in response.json() if s["id"] == session_id)


def _changes(update):
    return {c["field"]: (c["old"], c["new"]) for c in update["changes"]}


def _all_keys(preview):
    return [u["key"] for u in preview["updates"]] + [n["key"] for n in preview["new_sessions"]]


def test_import_with_start_soc_20_gets_real_values(client):
    """Spritmonitor-Import ohne Uhrzeit, Start-SoC 20 aus dem Formular."""
    register(client)
    vehicle = create_vehicle(client)
    session = _session(
        client, vehicle["id"],
        start_time="2026-05-23T00:00:00", soc_start=20, soc_end=80, energy_kwh=36.5,
        energy_is_estimated=False, odometer_km=12000,
    )
    content = _csv(("a1", "2026-05-23T08:10:00", "2026-05-23T08:40:00", 1700, 36, 33, 80))

    preview = _preview(client, vehicle["id"], content)
    assert len(preview["updates"]) == 1
    changes = _changes(preview["updates"][0])
    assert changes["soc_start"] == (20, 33)
    assert "soc_end" not in changes
    # Der Import hatte keine Uhrzeit - die echte wird gesetzt.
    assert changes["start_time"][1] == "2026-05-23T08:10:00"
    assert changes["end_time"] == (None, "2026-05-23T08:40:00")
    # 36 kWh in 1700 s sind rund 76 kW - DC.
    assert changes["charging_type"] == (None, "DC")
    # Vorhandene kWh bleiben unangetastet.
    assert "energy_kwh" not in changes

    result = _commit(client, vehicle["id"], content, _all_keys(preview))
    assert result == {"updated": 1, "created": 0}
    stored = _get(client, session["id"])
    assert stored["soc_start"] == 33
    assert stored["energy_kwh"] == 36.5
    assert stored["odometer_km"] == 12000
    assert stored["charging_type"] == "DC"
    assert "MySkoda-Abgleich: Start-SoC 20 → 33" in stored["notes"]


def test_collective_entry_is_matched_to_several_charges(client):
    """Spritmonitor-Eintrag vom 04.08. = Ladungen vom 01.08. und 03.08."""
    register(client)
    vehicle = create_vehicle(client)
    session = _session(
        client, vehicle["id"],
        start_time="2026-08-04T00:00:00", soc_start=20, soc_end=80, energy_kwh=40.0,
        energy_is_estimated=False,
    )
    content = _csv(
        ("b1", "2026-08-01T18:00:00", "2026-08-01T21:00:00", 10000, 22, 30, 58),
        ("b2", "2026-08-03T18:00:00", "2026-08-03T20:00:00", 6500, 18, 57, 80),
    )

    preview = _preview(client, vehicle["id"], content)
    assert len(preview["updates"]) == 1
    update = preview["updates"][0]
    assert len(update["charges"]) == 2
    changes = _changes(update)
    assert changes["soc_start"] == (20, 30)
    assert changes["start_time"][1] == "2026-08-01T18:00:00"
    assert changes["end_time"][1] == "2026-08-03T20:00:00"
    assert changes["charging_type"] == (None, "AC")
    assert preview["new_sessions"] == []

    _commit(client, vehicle["id"], content, _all_keys(preview))
    assert "(2 Ladungen zusammengefasst)" in _get(client, session["id"])["notes"]


def test_collective_entry_with_estimated_energy_is_matched_by_soc(client):
    """18.05.: geschaetzte kWh, Start-SoC 21 stimmt - der Eintrag umfasst drei
    Ladungen. Die letzte allein (79 -> 93) passte zum End-SoC und haette den
    richtigen Start-SoC 21 auf 79 "korrigiert"."""
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-05-18T00:00:00", soc_start=21, soc_end=93,
             energy_kwh=57.0, energy_is_estimated=True)
    content = _csv(
        ("m1", "2026-05-14T09:33:00", "2026-05-15T09:58:00", 37000, 32, 21, 66),
        ("m2", "2026-05-15T11:59:00", "2026-05-16T09:24:00", 14800, 13, 64, 82),
        ("m3", "2026-05-18T12:18:00", "2026-05-18T17:02:00", 13300, 12, 79, 93),
    )

    preview = _preview(client, vehicle["id"], content)
    assert len(preview["updates"]) == 1
    update = preview["updates"][0]
    assert len(update["charges"]) == 3
    assert "soc_start" not in _changes(update)
    assert preview["hints"] == []


def test_session_stored_two_hours_early_gets_its_times_corrected(client):
    """12.-16.08.: automatisch erfasst, aber mit UTC statt Ortszeit gespeichert."""
    register(client)
    vehicle = create_vehicle(client)
    session = _session(
        client, vehicle["id"],
        start_time="2026-08-12T16:05:00", end_time="2026-08-12T19:00:00",
        soc_start=45, soc_end=80, charging_type="AC",
    )
    content = _csv(("c1", "2026-08-12T18:05:00", "2026-08-12T21:00:00", 10000, 27, 45, 80))

    preview = _preview(client, vehicle["id"], content)
    changes = _changes(preview["updates"][0])
    assert changes == {
        "start_time": ("2026-08-12T16:05:00", "2026-08-12T18:05:00"),
        "end_time": ("2026-08-12T19:00:00", "2026-08-12T21:00:00"),
    }


def test_missed_charging_start_gets_real_start(client):
    """03.10.: der Poller hat den Ladebeginn erst spaet bemerkt."""
    register(client)
    vehicle = create_vehicle(client)
    _session(
        client, vehicle["id"],
        start_time="2026-10-03T14:50:00", end_time="2026-10-03T16:30:00",
        soc_start=52, soc_end=80, charging_type="AC",
    )
    content = _csv(("d1", "2026-10-03T12:40:00", "2026-10-03T16:25:00", 13000, 33, 38, 80))

    changes = _changes(_preview(client, vehicle["id"], content)["updates"][0])
    assert changes["soc_start"] == (52, 38)
    assert changes["start_time"][1] == "2026-10-03T12:40:00"
    # Ende weicht nur 5 Minuten ab - keine Korrektur.
    assert "end_time" not in changes


def test_charge_far_from_any_session_is_created(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-06-01T10:00:00", end_time="2026-06-01T12:00:00",
             soc_start=30, soc_end=70)
    content = _csv(
        ("e1", "2026-06-01T10:05:00", "2026-06-01T12:00:00", 7000, 30, 30, 70),
        ("e2", "2026-06-20T09:00:00", "2026-06-20T09:30:00", 1800, 40, 20, 72),
    )

    preview = _preview(client, vehicle["id"], content)
    assert [n["key"] for n in preview["new_sessions"]] == ["new:e2"]
    assert preview["new_sessions"][0]["charging_type"] == "DC"

    result = _commit(client, vehicle["id"], content, _all_keys(preview))
    assert result["created"] == 1
    sessions = client.get("/api/sessions").json()
    new = next(s for s in sessions if s["start_time"].startswith("2026-06-20"))
    assert new["source"] == "import"
    assert new["needs_review"] is True
    assert new["energy_kwh"] == 40
    assert new["energy_is_estimated"] is False
    assert new["energy_meter_effective"] == "vehicle"
    assert new["external_session_id"] == "myskoda-app-e2"


def test_charge_next_to_unmatched_session_is_only_a_hint(client):
    """Passt eine Ladung nicht zu einem nahen Vorgang (hier: kWh weit daneben),
    ist sie trotzdem mit hoher Wahrscheinlichkeit derselbe Vorgang - kein Duplikat."""
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-06-29T00:00:00", soc_end=80, energy_kwh=60.0,
             energy_is_estimated=False)
    content = _csv(("f1", "2026-06-28T18:00:00", "2026-06-28T22:00:00", 14000, 30, 40, 80))

    preview = _preview(client, vehicle["id"], content)
    assert preview["updates"] == []
    assert preview["new_sessions"] == []
    kinds = sorted(h["kind"] for h in preview["hints"])
    assert kinds == ["unassigned_charge", "unmatched_session"]


def test_second_run_changes_nothing(client):
    register(client)
    vehicle = create_vehicle(client)
    _session(client, vehicle["id"], start_time="2026-05-23T00:00:00", soc_start=20, soc_end=80,
             energy_kwh=36.5, energy_is_estimated=False)
    content = _csv(
        ("g1", "2026-05-23T08:10:00", "2026-05-23T08:40:00", 1700, 36, 33, 80),
        ("g2", "2026-07-10T09:00:00", "2026-07-10T09:30:00", 1800, 40, 20, 72),
    )
    preview = _preview(client, vehicle["id"], content)
    _commit(client, vehicle["id"], content, _all_keys(preview))

    again = _preview(client, vehicle["id"], content)
    assert again["updates"] == []
    assert again["new_sessions"] == []
    assert again["unchanged"] == 1
    assert again["already_imported"] == 1
    assert _commit(client, vehicle["id"], content, _all_keys(preview)) == {"updated": 0, "created": 0}
    assert len(client.get("/api/sessions").json()) == 2


def test_only_accepted_keys_are_applied(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _session(client, vehicle["id"], start_time="2026-05-23T00:00:00", soc_start=20, soc_end=80,
                       energy_kwh=36.5, energy_is_estimated=False)
    content = _csv(
        ("h1", "2026-05-23T08:10:00", "2026-05-23T08:40:00", 1700, 36, 33, 80),
        ("h2", "2026-07-10T09:00:00", "2026-07-10T09:30:00", 1800, 40, 20, 72),
    )
    result = _commit(client, vehicle["id"], content, ["new:h2"])
    assert result == {"updated": 0, "created": 1}
    assert _get(client, session["id"])["soc_start"] == 20


def test_charges_without_soc_increase_are_ignored(client):
    register(client)
    vehicle = create_vehicle(client)
    content = _csv(("i1", "2026-03-19T10:00:00", "2026-03-19T11:00:00", 0, 0, 80, 80))
    preview = _preview(client, vehicle["id"], content)
    assert preview["charges_ignored"] == 1
    assert preview["new_sessions"] == []


def test_utc_timestamps_are_converted_to_local_time(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        charges = myskoda_import.parse_export(
            _csv(("j1", "2026-08-12T16:05:00Z", "2026-08-12T19:00:00Z", 10000, 27, 45, 80))
        )
    finally:
        monkeypatch.undo()
        time.tzset()
    assert charges[0].start == datetime(2026, 8, 12, 18, 5)
    assert charges[0].end == datetime(2026, 8, 12, 21, 0)


def test_unknown_file_gives_a_clear_error(client):
    register(client)
    vehicle = create_vehicle(client)
    response = client.post(
        f"/api/import/myskoda/preview?vehicle_id={vehicle['id']}",
        files={"file": ("x.csv", b"Datum;Km-Stand;Spritmenge\n01.01.2026;100;20\n", "text/csv")},
    )
    assert response.status_code == 422
    assert "Gefundene Spalten" in response.json()["detail"]


def test_other_users_sessions_are_not_touched(client, db_session):
    register(client, "anna")
    vehicle_a = create_vehicle(client)
    session_a = _session(client, vehicle_a["id"], start_time="2026-05-23T00:00:00", soc_start=20,
                         soc_end=80, energy_kwh=36.5, energy_is_estimated=False)
    register(client, "ben")
    vehicle_b = create_vehicle(client)
    content = _csv(("k1", "2026-05-23T08:10:00", "2026-05-23T08:40:00", 1700, 36, 33, 80))

    preview = _preview(client, vehicle_b["id"], content)
    assert preview["updates"] == []
    assert [n["key"] for n in preview["new_sessions"]] == ["new:k1"]

    response = client.post(
        f"/api/import/myskoda/preview?vehicle_id={vehicle_a['id']}",
        files={"file": ("x.csv", content, "text/csv")},
    )
    assert response.status_code == 404

    _commit(client, vehicle_b["id"], content, _all_keys(preview))
    from app import models

    assert db_session.get(models.ChargingSession, session_a["id"]).soc_start == 20


@pytest.mark.parametrize("delimiter", [";", ","])
def test_delimiter_is_detected(delimiter):
    content = _csv(("l1", "2026-05-23T08:10:00", "2026-05-23T08:40:00", 1700, 36, 33, 80))
    content = content.decode().replace('","', f'"{delimiter}"').encode()
    charges = myskoda_import.parse_export(content)
    assert charges[0].soc_start == 33
    assert charges[0].energy_kwh == 36
