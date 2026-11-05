"""Wiederholte Anlage mit `Idempotency-Key` legt nichts doppelt an.

Anlass: die Apps speichern offline zuerst lokal und laden spaeter hoch. Geht
die Antwort auf ein POST verloren, wiederholen sie es beim naechsten Sync.
"""

from conftest import create_vehicle, register

KEY = {"Idempotency-Key": "8f6c1c2e-0000-4000-8000-000000000001"}


def _session_payload(vehicle_id):
    return {"vehicle_id": vehicle_id, "start_time": "2026-11-05T08:00:00", "soc_start": 20, "soc_end": 80}


def test_repeated_session_post_returns_the_same_row(client):
    register(client)
    vehicle = create_vehicle(client)
    first = client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY)
    second = client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/sessions").json()) == 1


def test_without_key_behaves_as_before(client):
    register(client)
    vehicle = create_vehicle(client)
    client.post("/api/sessions", json=_session_payload(vehicle["id"]))
    client.post("/api/sessions", json=_session_payload(vehicle["id"]))
    assert len(client.get("/api/sessions").json()) == 2


def test_different_keys_create_two_rows(client):
    register(client)
    vehicle = create_vehicle(client)
    client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers={"Idempotency-Key": "a"})
    client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers={"Idempotency-Key": "b"})
    assert len(client.get("/api/sessions").json()) == 2


def test_deleted_row_is_created_again(client):
    register(client)
    vehicle = create_vehicle(client)
    first = client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY).json()
    assert client.delete(f"/api/sessions/{first['id']}").status_code == 204
    again = client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY)
    assert again.status_code == 201
    assert again.json()["id"] != first["id"]


def test_key_is_per_user(client):
    register(client, "anna")
    vehicle_a = create_vehicle(client)
    client.post("/api/sessions", json=_session_payload(vehicle_a["id"]), headers=KEY)
    register(client, "bert")
    vehicle_b = create_vehicle(client)
    response = client.post("/api/sessions", json=_session_payload(vehicle_b["id"]), headers=KEY)
    assert response.status_code == 201
    assert response.json()["vehicle_id"] == vehicle_b["id"]
    assert len(client.get("/api/sessions").json()) == 1


def test_provider_retry_is_not_a_409(client):
    """Ohne Schluessel waere die Wiederholung ein 409 (Name schon vergeben)."""
    register(client)
    first = client.post("/api/providers", json={"name": "Ionity"}, headers=KEY)
    second = client.post("/api/providers", json={"name": "Ionity"}, headers=KEY)
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_vehicle_retry(client):
    register(client)
    body = {"external_id": "enyaq", "name": "Enyaq", "battery_capacity_kwh": 77.0}
    first = client.post("/api/vehicles", json=body, headers=KEY)
    second = client.post("/api/vehicles", json=body, headers=KEY)
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_tire_set_and_tread_retry(client):
    register(client)
    vehicle = create_vehicle(client)
    body = {"vehicle_id": vehicle["id"], "kind": "winter", "installed_on": "2025-10-20T00:00:00"}
    first = client.post("/api/tires", json=body, headers=KEY)
    second = client.post("/api/tires", json=body, headers=KEY)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/tires").json()) == 1

    tire_id = first.json()["id"]
    tread_key = {"Idempotency-Key": "tread-1"}
    t1 = client.post(f"/api/tires/{tire_id}/tread", json={"measured_on": "2026-01-10T00:00:00", "depth_mm": 6.5}, headers=tread_key)
    t2 = client.post(f"/api/tires/{tire_id}/tread", json={"measured_on": "2026-01-10T00:00:00", "depth_mm": 6.5}, headers=tread_key)
    assert t1.status_code == 201 and t2.status_code == 201
    assert t1.json()["id"] == t2.json()["id"]
    assert len(client.get(f"/api/tires/tread?tire_set_id={tire_id}").json()) == 1


def test_same_key_for_different_types_does_not_collide(client):
    """Die Apps nehmen je Typ eigene UUIDs, aber sicher ist sicher."""
    register(client)
    vehicle = client.post(
        "/api/vehicles", json={"external_id": "enyaq", "name": "Enyaq", "battery_capacity_kwh": 77.0}, headers=KEY
    ).json()
    session = client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY)
    assert session.status_code == 201
    assert session.json()["vehicle_id"] == vehicle["id"]


def test_user_with_keys_can_be_deleted(client):
    register(client, "admin")
    register(client, "zweiter")
    vehicle = create_vehicle(client)
    client.post("/api/sessions", json=_session_payload(vehicle["id"]), headers=KEY)
    response = client.request("DELETE", "/api/auth/me", json={"current_password": "geheim1234"})
    assert response.status_code in (200, 204), response.text
