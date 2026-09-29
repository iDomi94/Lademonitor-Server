"""DOT-Alter und Profiltiefe der Reifensaetze (tires.py, routers/tires.py).

Zwei Regeln tragen das Ganze: die DOT reicht an EINER Montage eines Satzes
(Wiedermontagen erben sie in der Uebersicht), und die Profiltiefe ist immer
die geringste gemessene - das ist der Wert, an dem Mindestprofil und
Austausch haengen.
"""

from datetime import date

import pytest

from app import tires

from conftest import create_vehicle, register


def test_parse_dot_accepts_common_spellings():
    assert tires.parse_dot("2323") == "2323"
    assert tires.parse_dot(" 23/23 ") == "2323"
    # Komplette DOT-Nummer: nur die letzten vier Ziffern sind das Datum.
    assert tires.parse_dot("DOT EX2A 4Y7 0822") == "0822"
    assert tires.parse_dot("") is None
    assert tires.parse_dot(None) is None


@pytest.mark.parametrize("value", ["123", "5423", "0023", "0199"])
def test_parse_dot_rejects_impossible_codes(value):
    # zu kurz, Woche 54, Woche 0, Zukunft (2099)
    with pytest.raises(ValueError):
        tires.parse_dot(value)


def test_production_date_is_monday_of_iso_week():
    assert tires.dot_production_date("0122") == date(2022, 1, 3)


def test_age_and_tread_status_thresholds():
    today = date(2026, 9, 29)
    assert tires.age_status(date(2021, 1, 1), today) == "ok"
    assert tires.age_status(date(2020, 6, 1), today) == "check"
    assert tires.age_status(date(2016, 6, 1), today) == "replace"
    assert tires.age_status(None, today) is None

    assert tires.tread_status(5.0, "winter") == "ok"
    # 3,5 mm reicht im Sommer, im Winter nicht.
    assert tires.tread_status(3.5, "summer") == "ok"
    assert tires.tread_status(3.5, "winter") == "low"
    assert tires.tread_status(1.6, "summer") == "legal_min"


def _set(client, vehicle_id, day, **extra):
    payload = {
        "vehicle_id": vehicle_id,
        "kind": extra.pop("kind", "summer"),
        "installed_on": f"{day}T00:00:00",
        "brand": "Michelin",
        "model": "Pilot",
        "size": "235/45 R21",
        **extra,
    }
    response = client.post("/api/tires", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_dot_is_validated_and_normalised_over_the_api(client):
    register(client)
    vehicle = create_vehicle(client)

    created = _set(client, vehicle["id"], "2026-04-01", dot="12/24", dot_rear=None)
    assert created["dot"] == "1224"

    bad = client.post(
        "/api/tires",
        json={"vehicle_id": vehicle["id"], "kind": "summer",
              "installed_on": "2026-04-01T00:00:00", "dot": "5424"},
    )
    assert bad.status_code == 422

    patched = client.patch(f"/api/tires/{created['id']}", json={"dot_rear": "0923"})
    assert patched.json()["dot_rear"] == "0923"


def test_overview_takes_dot_from_any_mounting_and_the_older_axle(client):
    """Die DOT beim ersten Sommer eingetragen, beim zweiten nicht - der Satz
    hat trotzdem ein Alter, und zwar das der aelteren Achse."""
    register(client)
    vehicle = create_vehicle(client)
    _set(client, vehicle["id"], "2024-04-01", dot="1224", dot_rear="0923")
    _set(client, vehicle["id"], "2024-10-20", kind="winter", brand="Conti", model="WC")
    _set(client, vehicle["id"], "2025-04-05")

    sets = client.get("/api/tires/overview").json()["sets"]
    summer = next(s for s in sets if s["kind"] == "summer")
    winter = next(s for s in sets if s["kind"] == "winter")

    assert summer["mountings"] == 2
    assert summer["produced_on"] == "2023-02-27"  # KW 9/2023, Montag
    assert summer["age_status"] == "ok"
    assert summer["production_age_days"] > 0
    assert winter["produced_on"] is None
    assert winter["age_status"] is None


def test_tread_at_a_change_lands_on_both_sets(client):
    """Beim Wechsel werden beide Saetze gemessen: der aufgezogene an der neuen
    Montage, der abgenommene an der vorherigen - beide mit Wechseldatum und
    Kilometerstand."""
    register(client)
    vehicle = create_vehicle(client)
    summer = _set(client, vehicle["id"], "2025-04-01")
    winter = _set(
        client, vehicle["id"], "2025-10-20", kind="winter", brand="Conti", model="WC",
        odometer_km=18000,
        tread={"depth_mm": 7.5},
        removed_tread={"front_left_mm": 4.1, "front_right_mm": 4.0,
                       "rear_left_mm": 3.6, "rear_right_mm": 3.8},
    )

    rows = client.get("/api/tires/tread").json()
    by_set = {r["tire_set_id"]: r for r in rows}
    assert by_set[winter["id"]]["depth_mm"] == 7.5
    removed = by_set[summer["id"]]
    assert removed["depth_mm"] == 3.6  # das Minimum der vier Raeder
    assert removed["rear_left_mm"] == 3.6
    assert removed["odometer_km"] == 18000
    assert removed["measured_on"].startswith("2025-10-20")

    overview = client.get("/api/tires/overview").json()
    summer_set = next(s for s in overview["sets"] if s["kind"] == "summer")
    assert summer_set["tread_depth_mm"] == 3.6
    assert summer_set["tread_status"] == "ok"
    mounting = next(m for m in overview["mountings"] if m["tire_set_id"] == winter["id"])
    assert mounting["tread_depth_mm"] == 7.5


def test_removed_tread_without_a_previous_set_is_rejected(client):
    register(client)
    vehicle = create_vehicle(client)
    response = client.post(
        "/api/tires",
        json={"vehicle_id": vehicle["id"], "kind": "summer",
              "installed_on": "2025-04-01T00:00:00", "removed_tread": {"depth_mm": 3.0}},
    )
    assert response.status_code == 422
    assert client.get("/api/tires").json() == []


def test_measurement_crud_and_latest_wins(client):
    register(client)
    vehicle = create_vehicle(client)
    winter = _set(client, vehicle["id"], "2025-10-20", kind="winter")

    first = client.post(
        f"/api/tires/{winter['id']}/tread",
        json={"measured_on": "2025-12-01T00:00:00", "depth_mm": 6.8},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/tires/{winter['id']}/tread",
        json={"measured_on": "2026-02-01T00:00:00", "front_left_mm": 3.9, "front_right_mm": 4.2},
    ).json()
    assert second["depth_mm"] == 3.9

    sets = client.get("/api/tires/overview").json()["sets"]
    assert sets[0]["tread_depth_mm"] == 3.9
    assert sets[0]["tread_status"] == "low"  # Winter unter 4 mm

    # Ein Rad korrigiert: das Minimum zieht nach.
    patched = client.patch(f"/api/tires/tread/{second['id']}", json={"front_left_mm": 4.4})
    assert patched.json()["depth_mm"] == 4.2

    missing = client.post(
        f"/api/tires/{winter['id']}/tread", json={"measured_on": "2026-03-01T00:00:00"}
    )
    assert missing.status_code == 422

    assert client.delete(f"/api/tires/tread/{second['id']}").status_code == 204
    assert len(client.get(f"/api/tires/tread?tire_set_id={winter['id']}").json()) == 1

    # Loeschen des Satzes nimmt die Messungen mit (sonst scheitert es am FK).
    assert client.delete(f"/api/tires/{winter['id']}").status_code == 204
    assert client.get("/api/tires/tread").json() == []


def test_measurements_are_per_user(client):
    register(client, "alice")
    vehicle = create_vehicle(client)
    winter = _set(client, vehicle["id"], "2025-10-20", kind="winter")
    row = client.post(
        f"/api/tires/{winter['id']}/tread",
        json={"measured_on": "2025-12-01T00:00:00", "depth_mm": 6.8},
    ).json()

    register(client, "bruno")
    assert client.get("/api/tires/tread").json() == []
    assert client.delete(f"/api/tires/tread/{row['id']}").status_code == 404
    assert client.post(
        f"/api/tires/{winter['id']}/tread",
        json={"measured_on": "2025-12-01T00:00:00", "depth_mm": 6.8},
    ).status_code == 404
