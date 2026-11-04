"""Dashboard-Kennzahlen mit Zeitraumfilter.

Fehlermeldung 04.11.2026 (iOS, Filter "Monat bis jetzt"): zwei Ladungen
mit 19,4 und 20,6 kWh/100 km ergaben im Dashboard 29,5 kWh/100 km - die
zweite Ladung (30,7 kWh) geteilt durch die 104 km zwischen beiden, obwohl
sie den Akku weit ueber den Stand der ersten hinaus gefuellt hat.
"""

import pytest

from conftest import create_vehicle, register


def _session(client, vehicle_id, start, soc_start, soc_end, kwh, km, price):
    response = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle_id,
            "start_time": start,
            "charging_type": "AC",
            "soc_start": soc_start,
            "soc_end": soc_end,
            "energy_kwh": kwh,
            "odometer_km": km,
            "price_total": price,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed(client):
    vehicle = create_vehicle(client, capacity=77.0)
    _session(client, vehicle["id"], "2026-10-28T18:00:00", 40, 85, 30.0, 59951, 12.0)
    first = _session(client, vehicle["id"], "2026-11-01T13:02:00", 27, 80, 40.6, 60180, 16.79)
    second = _session(client, vehicle["id"], "2026-11-03T10:14:00", 54, 92, 30.7, 60284, 8.90)
    return vehicle, first, second


def test_month_filter_matches_the_session_values(client):
    register(client)
    _, first, second = _seed(client)
    assert first["consumption_kwh_per_100km"] == pytest.approx(19.4, abs=0.05)
    assert second["consumption_kwh_per_100km"] == pytest.approx(20.6, abs=0.05)

    stats = client.get("/api/stats/summary", params={"start_date": "2026-11-01"}).json()

    # km mit Anker (letzter Stand vor dem Zeitraum), wie im Rueckblick.
    assert stats["total_km_driven"] == 60284 - 59951
    expected = (
        first["consumption_kwh_per_100km"] * (60180 - 59951)
        + second["consumption_kwh_per_100km"] * (60284 - 60180)
    ) / (60284 - 59951)
    assert stats["avg_consumption_kwh_per_100km"] == pytest.approx(expected, abs=0.1)
    assert stats["avg_consumption_kwh_per_100km"] < 21
    # Preis je 100 km = Verbrauch x Ø Preis je kWh, nicht 8,90 EUR / 104 km.
    assert stats["price_per_100km"] == pytest.approx(
        expected * (16.79 + 8.90) / (40.6 + 30.7), abs=0.05
    )


def test_month_filter_matches_the_review(client):
    register(client)
    _seed(client)

    stats = client.get(
        "/api/stats/summary", params={"start_date": "2026-11-01", "end_date": "2026-11-30"}
    ).json()
    review = client.get(
        "/api/stats/review", params={"period": "month", "year": 2026, "month": 11}
    ).json()

    assert stats["total_km_driven"] == review["totals"]["km"]
    assert stats["avg_consumption_kwh_per_100km"] == pytest.approx(
        review["totals"]["avg_consumption_kwh_per_100km"], abs=0.05
    )


def test_without_filter_nothing_changes_for_a_full_soc_chain(client):
    """Ohne Filter ist der km-gewichtete Verbrauch bei lueckenloser SoC-Kette
    eine Teleskopsumme: (kWh ab dem zweiten Vorgang - Zuwachs im Akku) / km."""
    register(client)
    _seed(client)

    stats = client.get("/api/stats/summary").json()

    km = 60284 - 59951
    kwh = 40.6 + 30.7 - 77.0 * (92 - 85) / 100
    assert stats["total_km_driven"] == km
    assert stats["avg_consumption_kwh_per_100km"] == pytest.approx(kwh / km * 100, abs=0.05)


def test_two_vehicles_do_not_subtract_each_others_odometer(client):
    register(client)
    a = create_vehicle(client, external_id="a", name="A", capacity=None)
    b = create_vehicle(client, external_id="b", name="B", capacity=None)
    _session(client, a["id"], "2026-10-01T08:00:00", 20, 80, 40.0, 10000, 10.0)
    _session(client, b["id"], "2026-10-02T08:00:00", 20, 80, 40.0, 80000, 10.0)
    _session(client, a["id"], "2026-10-03T08:00:00", 20, 80, 40.0, 10200, 10.0)
    _session(client, b["id"], "2026-10-04T08:00:00", 20, 80, 40.0, 80200, 10.0)

    stats = client.get("/api/stats/summary").json()

    assert stats["total_km_driven"] == 400
    assert stats["avg_consumption_kwh_per_100km"] == pytest.approx(20.0)
