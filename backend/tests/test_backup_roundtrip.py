"""Backup-Export und -Import (routers/backup.py).

Der Mechanismus ist die letzte Rueckfalllinie bei einem Server-Neuaufbau -
und er hatte 2026-08-31 gleich zwei Fehler, die erst im echten Einsatz
auffielen: ein Import in ein ZWEITES Konto derselben Instanz uebersprang
restlos alles ("0 importiert, 46 uebersprungen"), weil die ID-Pruefung global
statt pro Nutzer lief; der Fix dafuer machte den Import zunaechst nicht mehr
idempotent. Beide Faelle stehen hier als Test.
"""

import io

from conftest import create_vehicle, register


def _export(client):
    response = client.get("/api/backup/export")
    assert response.status_code == 200, response.text
    return response.content


def _import(client, content):
    response = client.post(
        "/api/backup/import",
        files={"file": ("backup.zip", io.BytesIO(content), "application/zip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed(client):
    vehicle = create_vehicle(client)
    provider = client.post("/api/providers", json={"name": "EnBW"}).json()
    client.post(
        "/api/locations",
        json={
            "name": "Zuhause",
            "latitude": 48.8,
            "longitude": 9.0,
            "radius_m": 120,
            "default_provider_id": provider["id"],
        },
    )
    client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle["id"],
            "provider_id": provider["id"],
            "start_time": "2026-04-01T08:00:00",
            "soc_start": 20,
            "soc_end": 80,
            "energy_kwh": 40.0,
            "odometer_km": 12000,
            "price_total": 18.0,
        },
    )
    return vehicle, provider


def test_export_contains_all_four_tables(client):
    import zipfile

    register(client)
    _seed(client)

    names = set(zipfile.ZipFile(io.BytesIO(_export(client))).namelist())

    assert {"vehicles.csv", "providers.csv", "locations.csv", "sessions.csv"} <= names


def test_import_into_a_second_account_copies_the_data(client):
    """Der Fehler von 2026-08-31: die IDs existierten bereits - nur eben beim
    ANDEREN Nutzer -, woraufhin der Import alles uebersprang."""
    register(client, username="erste")
    _seed(client)
    archive = _export(client)

    register(client, username="zweite")
    result = _import(client, archive)

    assert result["vehicles_imported"] == 1
    assert result["sessions_imported"] == 1
    assert len(client.get("/api/sessions").json()) == 1
    # Die Kopie gehoert dem zweiten Konto, das Original bleibt beim ersten.
    assert len(client.get("/api/vehicles").json()) == 1


def test_importing_twice_creates_nothing_new(client):
    """Idempotenz: erkannt wird zusaetzlich an den Fachdaten (Fahrzeug an der
    external_id, Ladevorgang an Fahrzeug + Startzeit), nicht nur an der ID -
    sonst haette der Fix oben bei jedem Lauf neue UUIDs vergeben."""
    register(client, username="erste")
    _seed(client)
    archive = _export(client)

    register(client, username="zweite")
    _import(client, archive)
    second_run = _import(client, archive)

    assert second_run["vehicles_imported"] == 0
    assert second_run["sessions_imported"] == 0
    assert second_run["sessions_skipped"] == 1
    assert len(client.get("/api/sessions").json()) == 1


def test_restore_keeps_the_original_ids_when_they_are_free(client):
    """Restore-Fall (frischer Server): sind die IDs nirgends vergeben, bleiben
    sie erhalten - Verweise aus anderen Quellen zeigen danach weiterhin richtig.

    Nachgebaut, indem die Daten nach dem Export geloescht werden: danach ist die
    Lage dieselbe wie auf einer leeren Installation. Wichtig ist die Abgrenzung
    zum Test darueber: gehoert die ID noch einem ANDEREN Nutzer, bekommt die
    Kopie eine neue - die Primaerschluessel sind global, die Daten aber pro
    Nutzer getrennt."""
    register(client, username="erste")
    vehicle, _ = _seed(client)
    archive = _export(client)

    for session in client.get("/api/sessions").json():
        client.delete(f"/api/sessions/{session['id']}")
    for location in client.get("/api/locations").json():
        client.delete(f"/api/locations/{location['id']}")
    for provider in client.get("/api/providers").json():
        client.delete(f"/api/providers/{provider['id']}")
    client.delete(f"/api/vehicles/{vehicle['id']}")

    _import(client, archive)

    assert client.get("/api/vehicles").json()[0]["id"] == vehicle["id"]


def test_import_rejects_a_non_zip(client):
    register(client)

    response = client.post(
        "/api/backup/import",
        files={"file": ("daten.csv", io.BytesIO(b"kein,zip"), "text/csv")},
    )

    assert response.status_code == 422
