"""Gesamtpreis beim Bearbeiten festlegen.

Die Apps schicken bei jedem Speichern kWh-Preis UND Gesamtpreis mit. Wer nur
den Gesamtpreis aendert, darf ihn nicht vom alten kWh-Preis zurueckgerechnet
bekommen (Fehlermeldung 27.10.2026, iOS-App).
"""

from conftest import create_vehicle, register


def _priced_session(client, vehicle_id):
    response = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle_id,
            "start_time": "2026-10-01T08:00:00",
            "energy_kwh": 40.0,
            "price_per_kwh": 0.5,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["price_total"] == 20.0
    return body


def test_changed_total_wins_over_unchanged_price_per_kwh(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _priced_session(client, vehicle["id"])

    # So schickt es die App: kWh-Preis unveraendert, Gesamtpreis neu.
    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={"energy_kwh": 40.0, "price_per_kwh": 0.5, "price_total": 18.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["price_total"] == 18.0
    assert body["price_per_kwh"] == 0.45


def test_total_with_cleared_price_per_kwh(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _priced_session(client, vehicle["id"])

    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={"price_per_kwh": None, "price_total": 16.0},
    )
    body = response.json()
    assert body["price_total"] == 16.0
    assert body["price_per_kwh"] == 0.4


def test_changed_price_per_kwh_still_updates_total(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _priced_session(client, vehicle["id"])

    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={"energy_kwh": 40.0, "price_per_kwh": 0.6, "price_total": 20.0},
    )
    body = response.json()
    assert body["price_per_kwh"] == 0.6
    assert body["price_total"] == 24.0


def test_changed_energy_keeps_price_per_kwh(client):
    register(client)
    vehicle = create_vehicle(client)
    session = _priced_session(client, vehicle["id"])

    response = client.patch(
        f"/api/sessions/{session['id']}",
        json={"energy_kwh": 30.0, "price_per_kwh": 0.5, "price_total": 20.0},
    )
    body = response.json()
    assert body["price_per_kwh"] == 0.5
    assert body["price_total"] == 15.0
