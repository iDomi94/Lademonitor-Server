"""Heimladung mit PV-Anlage/Hausakku: gemessene Kosten und Netzanteil (v0.41.0).

Home Assistant schickt beim automatischen Push optional `grid_energy_kwh`
(davon aus dem Netz) und `price_total` (tatsaechliche Kosten). Geprueft wird
der Vertrag, gegen den die HA-Integration und die Apps gebaut werden:
gemessene Kosten schlagen das Preis-Gedaechtnis, ohne es zu ueberschreiben,
ein Netzanteil allein laesst die Preisfindung wie bisher, und der Netzanteil
ist nie groesser als die geladene Energie.
"""

from conftest import create_vehicle, register


def _home_setup(client):
    """Heim-Anbieter mit 0,30 EUR/kWh im Preis-Gedaechtnis und ein Ladeort,
    dem der Push per GPS zugeordnet wird."""
    register(client)
    create_vehicle(client, external_id="enyaq")
    home = client.post(
        "/api/providers",
        json={"name": "Zuhause", "last_price_ac_per_kwh": 0.30, "charging_place": "home"},
    ).json()
    client.post(
        "/api/locations",
        json={"name": "Garage", "latitude": 48.8, "longitude": 9.0,
              "default_provider_id": home["id"]},
    )
    return home


def _auto_push(client, sid, **extra):
    body = {
        "vehicle_external_id": "enyaq",
        "external_session_id": sid,
        "start_time": "2026-06-01T10:00:00",
        "charging_type": "ac",
        "soc_start": 40, "soc_end": 80,
        "latitude": 48.8, "longitude": 9.0,
    }
    body.update(extra)
    response = client.post("/api/sessions/auto", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _provider(client, provider_id):
    return next(p for p in client.get("/api/providers").json() if p["id"] == provider_id)


def test_measured_price_wins_over_provider_price(client):
    home = _home_setup(client)

    s = _auto_push(client, "pv-1", energy_kwh=20.0, energy_meter="charger",
                   grid_energy_kwh=5.0, price_total=1.5)

    # Anbieter weiterhin ueber den Ladeort aufgeloest ...
    assert s["provider_id"] == home["id"]
    assert s["location_id"] is not None
    # ... aber die Kosten sind die gemessenen, nicht 20 kWh x 0,30.
    assert s["price_total"] == 1.5
    assert s["price_per_kwh"] == 0.075
    assert s["grid_energy_kwh"] == 5.0
    # Der Mischpreis landet NICHT im Preis-Gedaechtnis.
    assert _provider(client, home["id"])["last_price_ac_per_kwh"] == 0.30


def test_reviewing_solar_session_keeps_provider_price(client):
    """Beim spaeteren Pruefen/Speichern (PATCH) darf der Mischpreis ebenso
    wenig ins Preis-Gedaechtnis wandern wie beim Push selbst."""
    home = _home_setup(client)
    s = _auto_push(client, "pv-2", energy_kwh=20.0, grid_energy_kwh=5.0, price_total=1.5)

    response = client.patch(f"/api/sessions/{s['id']}", json={"needs_review": False})
    assert response.status_code == 200, response.text

    assert _provider(client, home["id"])["last_price_ac_per_kwh"] == 0.30


def test_measured_price_zero_for_pure_solar(client):
    _home_setup(client)
    s = _auto_push(client, "pv-3", energy_kwh=10.0, grid_energy_kwh=0, price_total=0)

    assert s["price_total"] == 0
    assert s["price_per_kwh"] == 0
    assert s["grid_energy_kwh"] == 0


def test_measured_price_with_estimated_energy(client):
    """Ohne Wallbox-kWh wird die Energie wie bisher geschaetzt (77 kWh x 40 %),
    der Preis je kWh dann daraus abgeleitet."""
    _home_setup(client)
    s = _auto_push(client, "pv-4", price_total=3.08)

    assert s["energy_is_estimated"] is True
    assert s["energy_kwh"] == 30.8
    assert s["price_total"] == 3.08
    assert s["price_per_kwh"] == 0.1


def test_grid_energy_only_keeps_provider_pricing(client):
    home = _home_setup(client)
    s = _auto_push(client, "pv-5", energy_kwh=20.0, grid_energy_kwh=8.0)

    assert s["provider_id"] == home["id"]
    assert s["price_per_kwh"] == 0.30
    assert s["price_total"] == 6.0
    assert s["grid_energy_kwh"] == 8.0


def test_grid_energy_is_clamped_to_energy(client):
    _home_setup(client)
    s = _auto_push(client, "pv-6", energy_kwh=20.0, grid_energy_kwh=20.4, price_total=6.1)

    assert s["grid_energy_kwh"] == 20.0


def test_grid_energy_not_clamped_against_estimate(client):
    """Eine SoC-Schaetzung liegt ohne Ladeverluste unter dem Netzbezug - die
    Messung wird daran nicht verbogen."""
    _home_setup(client)
    s = _auto_push(client, "pv-7", grid_energy_kwh=33.0)

    assert s["energy_is_estimated"] is True
    assert s["grid_energy_kwh"] == 33.0


def test_unusable_sensor_values_do_not_reject_push(client):
    _home_setup(client)
    s = _auto_push(client, "pv-8", energy_kwh=20.0, grid_energy_kwh="unavailable",
                   price_total=-1)

    assert s["grid_energy_kwh"] is None
    # Dann gilt wie bisher der Anbieterpreis.
    assert s["price_total"] == 6.0


def test_session_out_and_round_trip_via_api(client):
    """Die Apps synchronisieren bidirektional: das Feld muss in SessionOut
    stehen, beim Anlegen/Aendern ankommen und von einem PATCH ohne das Feld
    unangetastet bleiben."""
    register(client)
    vehicle = create_vehicle(client)

    created = client.post(
        "/api/sessions",
        json={"vehicle_id": vehicle["id"], "start_time": "2026-06-02T10:00:00",
              "energy_kwh": 12.0, "grid_energy_kwh": 15.0},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["grid_energy_kwh"] == 12.0  # gekappt

    listed = client.get("/api/sessions").json()
    assert "grid_energy_kwh" in listed[0]

    patched = client.patch(f"/api/sessions/{body['id']}", json={"notes": "x"}).json()
    assert patched["grid_energy_kwh"] == 12.0

    patched = client.patch(f"/api/sessions/{body['id']}", json={"grid_energy_kwh": 4.0}).json()
    assert patched["grid_energy_kwh"] == 4.0

    # Wird die Energie nach unten korrigiert, zieht der Netzanteil mit.
    patched = client.patch(f"/api/sessions/{body['id']}", json={"energy_kwh": 3.0}).json()
    assert patched["grid_energy_kwh"] == 3.0

    assert client.post(
        "/api/sessions",
        json={"vehicle_id": vehicle["id"], "start_time": "2026-06-03T10:00:00",
              "grid_energy_kwh": -1},
    ).status_code == 422


def test_stats_summary_solar_kwh(client):
    _home_setup(client)
    _auto_push(client, "pv-9", energy_kwh=20.0, grid_energy_kwh=5.0, price_total=1.5)
    _auto_push(client, "pv-10", start_time="2026-06-02T10:00:00", energy_kwh=10.0)

    summary = client.get("/api/stats/summary").json()

    # Nur der Vorgang mit gemessenem Netzanteil zaehlt, der andere ist unbekannt.
    assert summary["solar_kwh"] == 15.0
