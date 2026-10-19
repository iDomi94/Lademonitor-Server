"""Erweitertes Bearbeiten eines Ladevorgangs (Web-UI, Schalter "Alle Felder").

PATCH nimmt seitdem auch Fahrzeug, Quelle und externe ID an. Die Faelle
halten fest, dass `null` bei den Pflichtfeldern nichts aendert und ein
fremdes Fahrzeug abgelehnt wird.
"""

from conftest import create_vehicle, login, register


def _session(client, vehicle_id):
    response = client.post(
        "/api/sessions",
        json={"vehicle_id": vehicle_id, "start_time": "2026-10-01T08:00:00", "soc_start": 20, "soc_end": 80},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_all_stored_fields_can_be_patched(client):
    register(client)
    first = create_vehicle(client)
    second = create_vehicle(client, external_id="id3", name="ID.3", capacity=58.0)
    session = _session(client, first["id"])

    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={
            "vehicle_id": second["id"],
            "source": "automatic",
            "external_session_id": "vwda-x-1",
            "end_time": "2026-10-01T10:30:15",
            "grid_energy_kwh": 5.0,
            "energy_kwh": 30.0,
            "notes": "Notiz",
            "latitude": 48.8,
            "longitude": 9.0,
            "outside_temp_c": 7.5,
            "outside_temp_source": "weather_daily",
            "needs_review": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vehicle_id"] == second["id"]
    assert body["source"] == "automatic"
    assert body["external_session_id"] == "vwda-x-1"
    assert body["end_time"].startswith("2026-10-01T10:30:15")
    assert body["grid_energy_kwh"] == 5.0
    assert body["notes"] == "Notiz"
    assert body["latitude"] == 48.8 and body["longitude"] == 9.0
    assert body["outside_temp_source"] == "weather_daily"
    assert body["needs_review"] is True


def test_null_on_required_fields_keeps_them(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _session(client, vehicle["id"])

    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={"vehicle_id": None, "source": None, "external_session_id": ""},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vehicle_id"] == vehicle["id"]
    assert body["source"] == "manual"
    assert body["external_session_id"] is None


def test_foreign_vehicle_is_rejected(client):
    register(client, username="other")
    foreign = create_vehicle(client, external_id="fremd")
    register(client, username="owner")
    login(client, "owner", "geheim1234")
    own = create_vehicle(client)
    session = _session(client, own["id"])

    response = client.patch(f"/api/sessions/{session['id']}", json={"vehicle_id": foreign["id"]})
    assert response.status_code == 404
    assert client.get("/api/sessions").json()[0]["vehicle_id"] == own["id"]
