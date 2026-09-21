"""Grabsteine fuer geloeschte Datensaetze (models.DeletedRecord, routers/sync.py).

Warum es sie gibt: die Apps duerfen aus der ABWESENHEIT einer Zeile in einer
Pull-Antwort nicht auf eine Loeschung schliessen (eine unvollstaendige Antwort
saehe genauso aus und wuerde still lokale Daten vernichten). Genau deshalb hatten
iOS und Android das Aufraeumen abgeschaltet und lebten mit Geisterzeilen. Diese
Tests sichern die Gegenrichtung ab: jede Loeschung hinterlaesst ein
ausdrueckliches Signal, und zwar genau eines, nur fuer den eigenen Nutzer.
"""

from datetime import datetime, timedelta, timezone

from conftest import create_vehicle, register


def _types(body):
    return {(d["entity_type"], d["entity_id"]) for d in body["deletions"]}


def test_deleting_each_entity_leaves_a_tombstone(client):
    register(client)
    vehicle = create_vehicle(client)
    provider = client.post("/api/providers", json={"name": "EnBW"}).json()
    location = client.post(
        "/api/locations",
        json={"name": "Zuhause", "latitude": 48.8, "longitude": 9.0, "radius_m": 100},
    ).json()
    session = client.post(
        "/api/sessions",
        json={"vehicle_id": vehicle["id"], "start_time": "2026-05-01T08:00:00"},
    ).json()

    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    assert client.delete(f"/api/locations/{location['id']}").status_code == 204
    assert client.delete(f"/api/providers/{provider['id']}").status_code == 204
    assert client.delete(f"/api/vehicles/{vehicle['id']}").status_code == 204

    body = client.get("/api/sync/deletions").json()

    assert _types(body) == {
        ("session", session["id"]),
        ("location", location["id"]),
        ("provider", provider["id"]),
        ("vehicle", vehicle["id"]),
    }


def test_since_filters_out_older_tombstones(client):
    register(client)
    vehicle = create_vehicle(client)
    first = client.post(
        "/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-05-01T08:00:00"}
    ).json()
    client.delete(f"/api/sessions/{first['id']}")

    cursor = client.get("/api/sync/deletions").json()["server_time"]

    second = client.post(
        "/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-05-02T08:00:00"}
    ).json()
    client.delete(f"/api/sessions/{second['id']}")

    body = client.get("/api/sync/deletions", params={"since": cursor}).json()

    assert _types(body) == {("session", second["id"])}


def test_since_accepts_a_timezone_aware_cursor(client):
    """Die Apps schicken ihren Zeitstempel je nach Plattform mit Offset (`Z`
    bzw. `+02:00`). In der Datenbank liegen naive UTC-Werte - ohne die
    Umrechnung in routers/sync.py waere der Vergleich um den Offset verschoben
    und wuerde Grabsteine verschlucken."""
    register(client)
    vehicle = create_vehicle(client)
    session = client.post(
        "/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-05-01T08:00:00"}
    ).json()
    client.delete(f"/api/sessions/{session['id']}")

    # Eine Stunde vor "jetzt", ausgedrueckt in einer Zone mit +02:00 - derselbe
    # Moment, nur anders geschrieben.
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(
        timezone(timedelta(hours=2))
    )

    body = client.get("/api/sync/deletions", params={"since": since.isoformat()}).json()

    assert _types(body) == {("session", session["id"])}


def test_tombstones_are_isolated_per_user(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)
    session = client.post(
        "/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-05-01T08:00:00"}
    ).json()
    client.delete(f"/api/sessions/{session['id']}")

    register(client, username="zweite")

    assert client.get("/api/sync/deletions").json()["deletions"] == []


def test_server_time_is_returned_as_cursor(client):
    """Der Cursor kommt bewusst vom Server, nicht von der Geraeteuhr - sonst
    wuerde jede Abweichung (Zeitzone, Drift) Grabsteine ueberspringen."""
    register(client)

    body = client.get("/api/sync/deletions").json()

    assert body["server_time"]
    datetime.fromisoformat(body["server_time"])


def test_deleting_a_user_removes_the_tombstones(client):
    """Grabsteine haengen per Fremdschluessel am Nutzer - bliebe auch nur einer
    stehen, scheiterte das Loeschen des Kontos an der Datenbank."""
    # Der ERSTE registrierte Nutzer wird Admin und darf sich nicht selbst
    # loeschen (sonst waere die Installation ohne Nutzerverwaltung) - deshalb
    # hier ein zweites, normales Konto.
    register(client, username="adminkonto")
    register(client, username="wegmit", password="geheim1234")
    vehicle = create_vehicle(client)
    session = client.post(
        "/api/sessions", json={"vehicle_id": vehicle["id"], "start_time": "2026-05-01T08:00:00"}
    ).json()
    client.delete(f"/api/sessions/{session['id']}")

    response = client.request(
        "DELETE", "/api/auth/me", json={"current_password": "geheim1234"}
    )

    assert response.status_code == 204, response.text
