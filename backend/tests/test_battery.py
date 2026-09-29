"""Akku-Gesundheit und Ladeverluste (battery.py).

Beide Auswertungen stehen auf derselben Zahl (kWh je SoC-Punkt). Geprueft wird
vor allem, was NICHT hineingehoert: eine geschaetzte Energie ergaebe immer
genau die Nennkapazitaet und saehe aus wie ein verlustfreier, neuer Akku.
"""

from datetime import datetime, timedelta

from app import models
from app.battery import (
    MIN_SOC_DELTA,
    build_health,
    build_losses,
    collect_points,
)
from conftest import create_vehicle, register

BASE = datetime(2025, 1, 1, 8, 0, 0)


def session(idx, *, kwh, soc_start=20, soc_end=80, kind="AC", days=None,
            estimated=False, provider=None, meter=None):
    return models.ChargingSession(
        id=f"s{idx}",
        vehicle_id="v1",
        provider_id=provider,
        start_time=BASE + timedelta(days=idx if days is None else days),
        charging_type=models.ChargingType(kind) if kind else None,
        soc_start=soc_start,
        soc_end=soc_end,
        energy_kwh=kwh,
        energy_is_estimated=estimated,
        energy_meter=meter,
    )


def test_apparent_capacity_and_loss_against_nominal():
    # 60 SoC-Punkte, 50,82 kWh -> 84,7 kWh fuer 100 %, 10 % ueber 77 kWh
    points, _ = collect_points([session(0, kwh=50.82)], 77.0)
    assert points[0].apparent_capacity_kwh == 84.7
    assert points[0].loss_pct == 10.0


def test_estimated_energy_is_never_used():
    points, excluded = collect_points(
        [session(0, kwh=46.2, estimated=True), session(1, kwh=50.0)], 77.0
    )
    assert [p.session_id for p in points] == ["s1"]
    assert excluded.estimated_energy == 1


def test_small_soc_delta_and_missing_values_are_counted():
    points, excluded = collect_points(
        [
            session(0, kwh=10.0, soc_start=60, soc_end=60 + MIN_SOC_DELTA - 1),
            session(1, kwh=None),
            session(2, kwh=50.0, soc_start=None),
        ],
        77.0,
    )
    assert points == []
    assert excluded.small_soc_delta == 1
    assert excluded.missing_values == 2


def test_implausible_values_are_dropped():
    # 60 SoC-Punkte mit 15 kWh waeren 25 kWh Kapazitaet - ein Tippfehler
    points, excluded = collect_points([session(0, kwh=15.0), session(1, kwh=50.0)], 77.0)
    assert [p.session_id for p in points] == ["s1"]
    assert excluded.implausible == 1


def test_losses_are_energy_weighted_per_type():
    points, _ = collect_points(
        [
            # AC: 10 % Verlust, einmal kurz und einmal lang
            session(0, kwh=77 * 0.2 * 1.10, soc_start=60, soc_end=80),
            session(1, kwh=77 * 0.8 * 1.10, soc_start=10, soc_end=90),
            # DC: 4 %
            session(2, kwh=77 * 0.6 * 1.04, kind="DC"),
        ],
        77.0,
    )
    by_type, _ = build_losses(points, 77.0)
    losses = {g.key: g.loss_pct for g in by_type}
    assert losses == {"AC": 10.0, "DC": 4.0}


def test_health_index_follows_capacity_loss_and_ignores_type_mix():
    """Zwei Jahre, Kapazitaet sinkt linear um 3 % pro Jahr. Anfangs wird fast
    nur AC geladen, spaeter fast nur DC - bei gemischter Rechnung saehe der
    Wechsel (kleinere Verluste) wie ein zusaetzlicher Kapazitaetsverlust aus."""
    sessions = []
    for i in range(0, 730, 10):
        factor = 1 - 0.03 * i / 365.25
        kind = "AC" if (i < 365) == (i % 30 != 0) else "DC"
        loss = 1.10 if kind == "AC" else 1.04
        sessions.append(session(i, kwh=77 * factor * 0.6 * loss, kind=kind))
    points, _ = collect_points(sessions, 77.0)
    health = build_health(points)

    assert set(health.baselines) == {"AC", "DC"}
    assert health.trend_pct_per_year is not None
    assert -3.6 < health.trend_pct_per_year < -2.4
    assert health.periods[0].index_pct > health.periods[-1].index_pct
    assert 93 < health.latest_index_pct < 97


def test_no_trend_on_too_short_a_period():
    points, _ = collect_points([session(i, kwh=50.0) for i in range(10)], 77.0)
    assert build_health(points).trend_pct_per_year is None


def test_endpoint_groups_by_vehicle_and_provider(client):
    register(client)
    vehicle = create_vehicle(client)
    provider = client.post("/api/providers", json={"name": "Ionity"}).json()
    for i in range(3):
        response = client.post(
            "/api/sessions",
            json={
                "vehicle_id": vehicle["id"],
                "provider_id": provider["id"],
                "start_time": (BASE + timedelta(days=i)).isoformat(),
                "charging_type": "DC",
                "soc_start": 10,
                "soc_end": 80,
                "energy_kwh": 56.06,
            },
        )
        assert response.status_code == 201, response.text
    # Ohne kWh: wird geschaetzt und darf nicht auftauchen
    client.post(
        "/api/sessions",
        json={"vehicle_id": vehicle["id"], "start_time": BASE.isoformat(),
              "soc_start": 20, "soc_end": 80},
    )

    body = client.get("/api/stats/battery").json()

    [entry] = body["vehicles"]
    assert entry["nominal_capacity_kwh"] == 77.0
    assert len(entry["points"]) == 3
    assert entry["excluded"]["estimated_energy"] == 1
    assert entry["losses_by_provider"][0]["key"] == "Ionity"
    assert entry["losses_by_type"][0]["loss_pct"] == 4.0


# ---------- Messort der kWh (Ladesaeule oder Fahrzeug, ab v0.29.0) ----------


def test_vehicle_measured_sessions_stay_out_of_the_losses():
    """Im Fahrzeug abgelesene kWh enthalten keine Ladeverluste - in der
    Verlustrechnung zoegen sie den Wert Richtung 0."""
    points, excluded = collect_points(
        [
            session(0, kwh=77 * 0.6 * 1.10),
            session(1, kwh=77 * 0.6 * 1.00, provider="home"),
        ],
        77.0,
        {"home": "vehicle"},
    )
    assert [p.energy_meter for p in points] == ["charger", "vehicle"]
    assert points[1].loss_pct is None
    assert excluded.vehicle_measured == 1
    by_type, by_provider = build_losses(points, 77.0)
    assert [(g.key, g.session_count, g.loss_pct) for g in by_type] == [("AC", 1, 10.0)]
    assert [g.key for g in by_provider] == [""]


def test_session_override_beats_the_provider():
    points, _ = collect_points(
        [
            session(0, kwh=50.0, provider="home", meter="charger"),
            session(1, kwh=50.0, provider="ionity", meter="vehicle"),
            session(2, kwh=50.0, provider="ionity"),
        ],
        77.0,
        {"home": "vehicle", "ionity": "charger"},
    )
    assert [p.energy_meter for p in points] == ["charger", "vehicle", "charger"]


def test_health_gets_its_own_baseline_for_vehicle_measured_sessions():
    """Gleicher Akku, einmal an der Wallbox (10 % Verlust), einmal im Auto
    abgelesen: ohne getrennte Anfangswerte saehe ein Wechsel des Messorts wie
    ein Kapazitaetssprung aus."""
    sessions = [session(i, kwh=77 * 0.6 * 1.10) for i in range(5)]
    sessions += [session(i, kwh=77 * 0.6, provider="home") for i in range(5, 10)]
    points, _ = collect_points(sessions, 77.0, {"home": "vehicle"})
    health = build_health(points)
    assert set(health.baselines) == {"AC", "AC/vehicle"}
    assert all(p.index_pct == 100.0 for p in health.periods)


def test_meter_follows_the_provider_and_only_stores_exceptions(client):
    register(client)
    vehicle = create_vehicle(client)
    home = client.post("/api/providers", json={"name": "Privat"}).json()
    assert home["energy_meter"] == "charger"

    def create(meter=None):
        body = {
            "vehicle_id": vehicle["id"],
            "provider_id": home["id"],
            "start_time": BASE.isoformat(),
            "soc_start": 20, "soc_end": 80, "energy_kwh": 46.2,
        }
        if meter:
            body["energy_meter"] = meter
        response = client.post("/api/sessions", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    follows = create()
    prefilled = create("charger")  # vorbefuellter Wert = keine Ausnahme
    exception = create("vehicle")
    assert prefilled["energy_meter"] is None
    assert exception["energy_meter"] == "vehicle"

    # Anbieter umstellen wirkt auf alle, die ihm folgen
    client.patch(f"/api/providers/{home['id']}", json={"energy_meter": "vehicle"})
    by_id = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert by_id[follows["id"]]["energy_meter_effective"] == "vehicle"
    assert by_id[prefilled["id"]]["energy_meter_effective"] == "vehicle"
    assert by_id[exception["id"]]["energy_meter_effective"] == "vehicle"

    # Eine Ausnahme ist wieder moeglich und bleibt stehen
    patched = client.patch(
        f"/api/sessions/{follows['id']}", json={"energy_meter": "charger"}
    ).json()
    assert patched["energy_meter"] == "charger"
    assert patched["energy_meter_effective"] == "charger"
    # ... und eine, die jetzt dem Anbieter entspricht, faellt beim Speichern weg
    patched = client.patch(
        f"/api/sessions/{exception['id']}", json={"notes": "x"}
    ).json()
    assert patched["energy_meter"] is None

    # null am Anbieter leert den Messort nicht
    provider = client.patch(
        f"/api/providers/{home['id']}", json={"energy_meter": None}
    ).json()
    assert provider["energy_meter"] == "vehicle"

    entry = client.get("/api/stats/battery").json()["vehicles"][0]
    assert entry["excluded"]["vehicle_measured"] == 2
    assert {p["energy_meter"] for p in entry["points"]} == {"charger", "vehicle"}
