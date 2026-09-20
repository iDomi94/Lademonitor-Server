"""Regressionstests fuer GET /api/sessions.

Anlass ist ein Fehler, der lange unbemerkt blieb: der Endpunkt hatte
`limit=200` als DEFAULT, und kein einziger Client (Web-UI, iOS, Android) hat je
einen Wert mitgeschickt. Wer mehr als 200 Ladevorgaenge hatte - nach einem
Spritmonitor-Import schnell der Fall -, sah ueberall nur die neuesten 200, ohne
jeden Hinweis. Die Statistik zeigte trotzdem die vollstaendigen Zahlen, weil sie
serverseitig aggregiert wird; genau das machte die Luecke so unauffaellig.
"""

from datetime import datetime, timedelta

from conftest import create_vehicle, register


def _create_sessions(client, vehicle_id, count, start=None):
    start = start or datetime(2026, 1, 1, 8, 0, 0)
    created = []
    for i in range(count):
        response = client.post(
            "/api/sessions",
            json={
                "vehicle_id": vehicle_id,
                "start_time": (start + timedelta(days=i)).isoformat(),
                "soc_start": 20,
                "soc_end": 80,
                "odometer_km": 10000 + i * 100,
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json())
    return created


def test_returns_all_sessions_without_limit(client):
    """Der eigentliche Regressionstest: mehr als die alten 200."""
    register(client)
    vehicle = create_vehicle(client)
    _create_sessions(client, vehicle["id"], 205)

    body = client.get("/api/sessions").json()

    assert len(body) == 205


def test_explicit_limit_and_offset_paginate(client):
    register(client)
    vehicle = create_vehicle(client)
    _create_sessions(client, vehicle["id"], 10)

    first = client.get("/api/sessions", params={"limit": 4}).json()
    second = client.get("/api/sessions", params={"limit": 4, "offset": 4}).json()

    assert len(first) == 4
    assert len(second) == 4
    # Absteigend nach Startzeit, und die zweite Seite setzt hinter der ersten an.
    assert [s["id"] for s in first] != [s["id"] for s in second]
    assert first[0]["start_time"] > second[0]["start_time"]


def test_limit_zero_is_rejected(client):
    """`limit=0` waere zweideutig ("nichts" oder "alles") - deshalb ungueltig.
    Wer alles will, laesst den Parameter weg."""
    register(client)
    assert client.get("/api/sessions", params={"limit": 0}).status_code == 422


def test_date_filter_still_applies(client):
    register(client)
    vehicle = create_vehicle(client)
    _create_sessions(client, vehicle["id"], 5, start=datetime(2026, 3, 1, 8, 0, 0))

    body = client.get(
        "/api/sessions", params={"start_date": "2026-03-03", "end_date": "2026-03-04"}
    ).json()

    assert len(body) == 2


def test_sessions_are_isolated_per_user(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)
    _create_sessions(client, vehicle["id"], 3)

    register(client, username="zweite")
    assert client.get("/api/sessions").json() == []
