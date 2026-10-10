"""Zuhause/Arbeit/Unterwegs am Anbieter (models.Provider.charging_place) und
die Aufteilung im Dashboard (StatsSummary.by_place)."""

import io

from conftest import create_vehicle, register


def _session(client, vehicle, provider, start, kwh, price):
    response = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle["id"],
            "provider_id": provider["id"] if provider else None,
            "start_time": start,
            "energy_kwh": kwh,
            "price_total": price,
        },
    )
    assert response.status_code == 201, response.text


def test_new_provider_is_public_and_can_be_changed(client):
    register(client)
    provider = client.post("/api/providers", json={"name": "Wallbox"}).json()
    assert provider["charging_place"] == "public"

    changed = client.patch(f"/api/providers/{provider['id']}", json={"charging_place": "home"})
    assert changed.json()["charging_place"] == "home"
    # Ein mitgeschicktes null leert das Feld nicht.
    kept = client.patch(f"/api/providers/{provider['id']}", json={"charging_place": None})
    assert kept.json()["charging_place"] == "home"
    invalid = client.patch(f"/api/providers/{provider['id']}", json={"charging_place": "garage"})
    assert invalid.status_code == 422


def test_by_place_stays_empty_without_home_or_work(client):
    """Nur "Unterwegs" ist keine Aufteilung - das Dashboard blendet sie aus."""
    register(client)
    vehicle = create_vehicle(client)
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    _session(client, vehicle, ionity, "2026-05-01T10:00:00", 40.0, 20.0)

    assert client.get("/api/stats/summary").json()["by_place"] == []


def test_by_place_splits_kwh_cost_and_price(client):
    register(client)
    vehicle = create_vehicle(client)
    home = client.post("/api/providers", json={"name": "Wallbox", "charging_place": "home"}).json()
    work = client.post("/api/providers", json={"name": "Firma", "charging_place": "work"}).json()
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    _session(client, vehicle, home, "2026-05-01T20:00:00", 30.0, 9.0)
    _session(client, vehicle, home, "2026-05-05T20:00:00", 20.0, 6.0)
    _session(client, vehicle, work, "2026-05-06T08:00:00", 10.0, 0.0)
    _session(client, vehicle, ionity, "2026-05-10T10:00:00", 40.0, 20.0)
    # Ohne Anbieter zaehlt als unterwegs.
    _session(client, vehicle, None, "2026-05-12T10:00:00", 10.0, 6.0)

    places = {p["place"]: p for p in client.get("/api/stats/summary").json()["by_place"]}

    assert list(places) == ["home", "work", "public"]
    assert places["home"] == {
        "place": "home", "session_count": 2, "total_kwh": 50.0,
        "total_cost": 15.0, "avg_price_per_kwh": 0.3,
    }
    assert places["work"]["avg_price_per_kwh"] == 0.0
    assert places["public"]["session_count"] == 2
    assert places["public"]["total_cost"] == 26.0
    assert places["public"]["avg_price_per_kwh"] == round(26.0 / 50.0, 4)


def test_by_place_follows_the_filters_and_includes_fees(client):
    register(client)
    vehicle = create_vehicle(client)
    home = client.post("/api/providers", json={"name": "Wallbox", "charging_place": "home"}).json()
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    _session(client, vehicle, home, "2026-04-01T20:00:00", 30.0, 9.0)
    _session(client, vehicle, ionity, "2026-05-10T10:00:00", 40.0, 20.0)
    client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 10.0, "interval": "once",
              "start_date": "2026-05-01", "end_date": "2026-05-31"},
    )

    all_time = {p["place"]: p for p in client.get("/api/stats/summary").json()["by_place"]}
    assert all_time["public"]["total_cost"] == 30.0
    # Im Mai gab es keine Heimladung - die Aufteilung verschwindet.
    may = client.get("/api/stats/summary?start_date=2026-05-01").json()
    assert may["by_place"] == []


def test_backup_keeps_charging_place(client):
    register(client, username="erste")
    client.post("/api/providers", json={"name": "Wallbox", "charging_place": "home"})
    archive = client.get("/api/backup/export").content

    register(client, username="zweite")
    response = client.post(
        "/api/backup/import",
        files={"file": ("backup.zip", io.BytesIO(archive), "application/zip")},
    )
    assert response.status_code == 200, response.text
    providers = client.get("/api/providers").json()
    assert [(p["name"], p["charging_place"]) for p in providers] == [("Wallbox", "home")]
