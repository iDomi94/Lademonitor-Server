"""Fahrzeugkosten, Wertverlust und Leasing (vehicle_costs.py,
routers/vehicle_costs.py, Gesamtkosten in routers/stats.py).

Wie bei den Grundgebuehren sind die Regeln Vertrag: beide Apps rechnen
Perioden und Wertverlust lokal nach (LocalVehicleCostCalculator).
"""

import io
import zipfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import create_vehicle, login, register

from app import vehicle_costs as vc


def _cost(start, interval="once", end=None, amount=100.0, category="insurance", spread=None):
    return SimpleNamespace(
        vehicle_id="v1",
        amount=amount,
        category=category,
        interval=interval,
        start_date=datetime.fromisoformat(start),
        end_date=datetime.fromisoformat(end) if end else None,
        spread_months=spread,
    )


def _vehicle(**fields):
    base = dict(
        id="v1", ownership="purchase", purchase_price=None, purchase_date=None,
        first_registration=None, purchase_odometer_km=None, depreciation_monthly_override=None,
        leasing_rate_monthly=None, leasing_start=None, leasing_end=None, leasing_down_payment=None,
    )
    base.update(fields)
    for key in ("purchase_date", "first_registration", "leasing_start", "leasing_end"):
        if isinstance(base[key], str):
            base[key] = datetime.fromisoformat(base[key])
    return SimpleNamespace(**base)


# ---------- Termine ----------


def test_once_is_a_single_payment():
    assert vc.occurrences(_cost("2026-03-05"), date(2027, 1, 1)) == [date(2026, 3, 5)]
    assert vc.occurrences(_cost("2026-03-05"), date(2026, 3, 4)) == []


def test_yearly_counts_in_the_due_month_and_stops_at_end():
    cost = _cost("2024-02-29", interval="yearly", end="2026-12-31")
    assert vc.occurrences(cost, date(2030, 1, 1)) == [
        date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28),
    ]


def test_monthly_keeps_the_anchor_day():
    cost = _cost("2026-01-31", interval="monthly")
    assert vc.occurrences(cost, date(2026, 4, 30)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
    ]


# ---------- Verteilen auf Monate (seit v0.49.0) ----------


def test_spread_shares_add_up_exactly():
    shares = vc.spread_shares(1000.0, date(2026, 1, 31), 48)
    assert len(shares) == 48
    assert shares[0] == (date(2026, 1, 31), 20.83)
    assert shares[1] == (date(2026, 2, 28), 20.83)
    assert shares[-1] == (date(2029, 12, 31), 20.99)  # Rundungsrest im letzten Anteil
    assert shares[2][0] == date(2026, 3, 31)
    assert round(sum(a for _, a in shares), 2) == 1000.0
    assert vc.spread_shares(100.0, date(2026, 1, 1), None) == [(date(2026, 1, 1), 100.0)]
    assert vc.spread_shares(100.0, date(2026, 1, 1), 1) == [(date(2026, 1, 1), 100.0)]


def test_spread_cost_counts_month_by_month_until_today():
    """Reifen fuer 800 EUR auf 4 Jahre: bis heute zaehlen nur die begonnenen Monate."""
    tires = _cost("2026-01-15", amount=800.0, category="tires", spread=48)
    entries = vc.cost_entries([tires], date(2026, 3, 20))
    assert [e.day for e in entries] == [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]
    assert all(e.category == "tires" for e in entries)
    assert sum(e.amount for e in entries) == pytest.approx(3 * 16.67)
    # Nach Ablauf der Verteilung ist alles drin, nicht mehr.
    done = vc.cost_entries([tires], date(2031, 1, 1))
    assert round(sum(e.amount for e in done), 2) == 800.0


def test_spread_yearly_payment_over_twelve_months():
    """Jahresversicherung je Zahlung auf 12 Monate: ein gleichmaessiger Monatsbetrag."""
    insurance = _cost("2025-01-01", interval="yearly", amount=600.0, spread=12)
    entries = vc.cost_entries([insurance], date(2026, 6, 30))
    assert len(entries) == 18
    assert {e.amount for e in entries} == {50.0}


# ---------- Restwertkurve ----------


def test_residual_curve():
    assert vc.residual_fraction(0) == 1.0
    assert vc.residual_fraction(1) == pytest.approx(0.75)
    assert vc.residual_fraction(3) == pytest.approx(0.75 * 0.87 ** 2)
    assert vc.residual_fraction(5) == pytest.approx(0.43, abs=0.005)


def test_mileage_ages_the_car():
    """60.000 km nach zwei Jahren werden wie ein Vierjaehriger bewertet."""
    v = _vehicle(purchase_price=40000, purchase_date="2024-01-01", purchase_odometer_km=0)
    curve = vc.Depreciation(v, [(date(2026, 1, 1), 60000)], date(2026, 1, 1))
    assert curve.effective_age(date(2026, 1, 1)) == pytest.approx(4.0)
    assert curve.value_at(date(2026, 1, 1)) == pytest.approx(40000 * vc.residual_fraction(4))


def test_used_car_is_scaled_from_the_value_at_purchase():
    """Der Kaufpreis ist der Wert beim Kauf, nicht der Neupreis."""
    v = _vehicle(
        purchase_price=30000, purchase_date="2026-01-01", first_registration="2024-01-01",
        purchase_odometer_km=20000,
    )
    curve = vc.Depreciation(v, [], date(2026, 1, 1))
    assert curve.value_at(date(2026, 1, 1)) == pytest.approx(30000)
    assert curve.value_at(date(2027, 1, 1)) < 30000


def test_depreciation_continues_with_recent_mileage():
    """Nach dem letzten km-Stand wird mit der Fahrleistung fortgeschrieben,
    sonst stuende der Wertverlust bei Ladepausen still."""
    v = _vehicle(purchase_price=40000, purchase_date="2025-01-01", purchase_odometer_km=0)
    points = [(date(2025, 7, 1), 15000), (date(2026, 1, 1), 30000)]
    curve = vc.Depreciation(v, points, date(2026, 3, 1))
    assert curve.km_per_year == pytest.approx(30000, rel=0.02)
    assert curve.km_at(date(2026, 7, 2)) == pytest.approx(45000, rel=0.02)


def test_override_replaces_the_curve():
    v = _vehicle(purchase_price=40000, purchase_date="2026-01-15", depreciation_monthly_override=350)
    entries = vc.ownership_entries(v, [], date(2026, 4, 1))
    assert [(e.day, e.amount, e.category) for e in entries] == [
        (date(2026, 1, 15), 350, "depreciation"),
        (date(2026, 2, 15), 350, "depreciation"),
        (date(2026, 3, 15), 350, "depreciation"),
    ]


def test_curve_entries_add_up_to_the_value_loss():
    v = _vehicle(purchase_price=40000, purchase_date="2026-01-01", purchase_odometer_km=0)
    entries = vc.ownership_entries(v, [], date(2026, 12, 31))
    curve = vc.Depreciation(v, [], date(2026, 12, 31))
    assert len(entries) == 12
    assert sum(e.amount for e in entries) == pytest.approx(
        40000 - curve.value_at(date(2027, 1, 1)), abs=0.1
    )


# ---------- Leasing ----------


def test_down_payment_is_spread_over_the_term():
    v = _vehicle(
        ownership="leasing", leasing_rate_monthly=300, leasing_down_payment=2400,
        leasing_start="2026-01-01", leasing_end="2027-12-31",
    )
    assert vc.leasing_term_months(v) == 24
    entries = vc.ownership_entries(v, [], date(2030, 1, 1))
    assert len(entries) == 24
    assert {e.amount for e in entries} == {400}
    assert vc.ownership_info(v, [], date(2026, 5, 1))["ownership_monthly"] == 400


def test_down_payment_without_end_counts_in_the_first_month():
    v = _vehicle(
        ownership="leasing", leasing_rate_monthly=300, leasing_down_payment=2400,
        leasing_start="2026-01-01",
    )
    amounts = [e.amount for e in vc.ownership_entries(v, [], date(2026, 3, 1))]
    assert amounts == [2700, 300, 300]


# ---------- API ----------


def test_cost_crud_and_tombstone(client):
    register(client)
    vehicle = create_vehicle(client)
    created = client.post(
        "/api/vehicle-costs",
        json={
            "vehicle_id": vehicle["id"], "amount": 600, "category": "insurance",
            "interval": "yearly", "start_date": "2025-03-01T10:30:00", "label": "Vollkasko",
        },
    )
    assert created.status_code == 201, created.text
    cost = created.json()
    assert cost["start_date"].startswith("2025-03-01T00:00")
    assert cost["paid_to_date"] >= 1200

    patched = client.patch(f"/api/vehicle-costs/{cost['id']}", json={"amount": 650, "category": None})
    assert patched.json()["amount"] == 650
    assert patched.json()["category"] == "insurance"

    once = client.patch(
        f"/api/vehicle-costs/{cost['id']}", json={"interval": "once", "end_date": "2026-01-01"}
    )
    assert once.json()["end_date"] is None

    assert client.delete(f"/api/vehicle-costs/{cost['id']}").status_code == 204
    deletions = client.get("/api/sync/deletions").json()["deletions"]
    assert ("vehicle_cost", cost["id"]) in {(d["entity_type"], d["entity_id"]) for d in deletions}


def test_spread_months_via_api_and_stats(client):
    register(client)
    vehicle = create_vehicle(client)
    today = date.today()
    start = date(today.year - 1, today.month, 1)
    created = client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 960, "category": "tires",
              "start_date": start.isoformat(), "spread_months": 48},
    ).json()
    assert created["spread_months"] == 48
    assert created["paid_to_date"] == 960
    assert created["spread_monthly"] == 20
    assert created["spread_to_date"] == 13 * 20  # 13 begonnene Monate

    stats = client.get(f"/api/stats/summary?start_date={today.year}-01-01").json()
    assert stats["vehicle_costs"]["other_cost"] == today.month * 20
    years = {y["year"]: y["total"] for y in client.get("/api/vehicle-costs/summary").json()}
    assert years[today.year - 1] == (13 - today.month) * 20

    # 1 Monat = nicht verteilen, null leert die Verteilung.
    one = client.patch(f"/api/vehicle-costs/{created['id']}", json={"spread_months": 1}).json()
    assert one["spread_months"] is None and one["spread_monthly"] is None
    client.patch(f"/api/vehicle-costs/{created['id']}", json={"spread_months": 24})
    cleared = client.patch(f"/api/vehicle-costs/{created['id']}", json={"spread_months": None}).json()
    assert cleared["spread_months"] is None
    assert cleared["spread_to_date"] == 960
    assert client.patch(f"/api/vehicle-costs/{created['id']}", json={"spread_months": 601}).status_code == 422


# ---------- THG-Quote (seit v0.50.0) ----------


def _thg(start, interval="once", amount=300.0, vehicle=None):
    cost = _cost(start, interval=interval, amount=amount, category="thg")
    cost.vehicle = vehicle
    return cost


def test_thg_counts_negative_over_its_calendar_year():
    entries = vc.cost_entries([_thg("2026-05-20")], date(2026, 12, 31))
    assert [e.day for e in entries] == [date(2026, m, 1) for m in range(1, 13)]
    assert all(e.amount == -25 for e in entries)
    assert all(e.category == "thg" for e in entries)


def test_thg_only_counts_started_months_even_before_its_date():
    # Datum im Mai, Stichtag Maerz: Januar bis Maerz zaehlen schon.
    entries = vc.cost_entries([_thg("2026-05-20")], date(2026, 3, 10))
    assert sum(e.amount for e in entries) == -75


def test_thg_is_spread_from_purchase_in_that_year():
    car = _vehicle(purchase_date="2026-07-15")
    entries = vc.cost_entries([_thg("2026-03-01", vehicle=car)], date(2027, 1, 1))
    assert [e.day for e in entries] == [date(2026, m, 15) for m in range(7, 13)]
    assert sum(e.amount for e in entries) == -300
    assert entries[0].amount == -50
    # Ein Kauf in einem frueheren Jahr verteilt wieder auf zwoelf Monate.
    old = _vehicle(purchase_date="2025-07-15")
    assert len(vc.cost_entries([_thg("2026-03-01", vehicle=old)], date(2027, 1, 1))) == 12
    # Leasingbeginn zaehlt genauso.
    leased = _vehicle(ownership="leasing", leasing_start="2026-10-01")
    assert len(vc.cost_entries([_thg("2026-01-01", vehicle=leased)], date(2027, 1, 1))) == 3


def test_thg_yearly_repeats_per_calendar_year_and_rounds():
    entries = vc.cost_entries([_thg("2025-01-01", interval="yearly", amount=100)], date(2026, 12, 31))
    assert len(entries) == 24
    year = [e.amount for e in entries if e.day.year == 2026]
    assert year[:11] == [-8.33] * 11 and year[-1] == -8.37
    assert round(sum(year), 2) == -100


def test_thg_via_api_and_stats(client):
    register(client)
    vehicle = create_vehicle(client)
    today = date.today()
    client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 600, "category": "insurance",
              "start_date": f"{today.year}-01-01"},
    )
    created = client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 240, "category": "thg",
              "start_date": f"{today.year}-03-01", "spread_months": 24},
    )
    assert created.status_code == 201, created.text
    thg = created.json()
    assert thg["spread_months"] is None  # verteilt sich selbst
    assert thg["spread_monthly"] == 20
    assert thg["spread_to_date"] == -20 * today.month

    stats = client.get(f"/api/stats/summary?start_date={today.year}-01-01").json()
    costs = stats["vehicle_costs"]
    assert costs["other_cost"] == 600 - 20 * today.month
    assert costs["by_category"][-1] == {"category": "thg", "amount": -20 * today.month}
    years = {y["year"]: y["total"] for y in client.get("/api/vehicle-costs/summary").json()}
    assert years[today.year] == 600 - 20 * today.month


def test_cost_validation_and_isolation(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)
    bad = client.post(
        "/api/vehicle-costs",
        json={
            "vehicle_id": vehicle["id"], "amount": 10, "interval": "monthly",
            "start_date": "2026-05-01", "end_date": "2026-04-01",
        },
    )
    assert bad.status_code == 422

    register(client, username="zweite")
    foreign = client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 10, "start_date": "2026-05-01"},
    )
    assert foreign.status_code == 404


def test_vehicle_ownership_fields(client):
    register(client)
    vehicle = create_vehicle(client)
    today = date.today()
    response = client.patch(
        f"/api/vehicles/{vehicle['id']}",
        json={
            "ownership": "purchase", "purchase_price": 40000,
            "purchase_date": (today - timedelta(days=365)).isoformat(),
            "purchase_odometer_km": 0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ownership"] == "purchase"
    assert body["purchase_date"].endswith("T00:00:00")
    assert 25000 < body["estimated_value"] < 40000
    assert body["depreciation_suggested_monthly"] > 0
    assert body["ownership_monthly"] == body["depreciation_suggested_monthly"]

    # ownership=null laesst die Angabe stehen (Pflichtfeld mit Wert).
    keep = client.patch(f"/api/vehicles/{vehicle['id']}", json={"ownership": None, "name": "Neu"})
    assert keep.json()["ownership"] == "purchase"


def test_stats_total_cost_of_ownership(client):
    register(client)
    vehicle = create_vehicle(client)
    assert client.get("/api/stats/summary").json()["vehicle_costs"] is None

    for day, km, price in (("2026-01-10", 10000, 20.0), ("2026-02-10", 11000, 30.0)):
        client.post(
            "/api/sessions",
            json={
                "vehicle_id": vehicle["id"], "start_time": f"{day}T08:00:00",
                "energy_kwh": 40, "odometer_km": km, "price_total": price,
            },
        )
    client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 500, "category": "insurance", "start_date": "2026-01-01"},
    )
    client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 80, "category": "maintenance", "start_date": "2025-06-01"},
    )

    stats = client.get("/api/stats/summary?start_date=2026-01-01").json()
    costs = stats["vehicle_costs"]
    # Die Lade-Kennzahlen bleiben reine Ladekosten.
    assert stats["total_cost"] == 50
    assert costs["charging_cost"] == 50
    assert costs["other_cost"] == 500  # die Wartung von 2025 liegt vor dem Filter
    assert costs["total_cost"] == 550
    assert costs["cost_per_km"] == pytest.approx(0.55)
    assert [c["category"] for c in costs["by_category"]] == ["insurance", "charging"]

    years = client.get("/api/vehicle-costs/summary").json()
    assert [y["year"] for y in years] == [2026, 2025]
    assert years[0]["total"] == 550
    assert years[1]["total"] == 80


def test_ownership_alone_shows_the_section(client):
    register(client)
    vehicle = create_vehicle(client)
    client.patch(
        f"/api/vehicles/{vehicle['id']}",
        json={
            "ownership": "leasing", "leasing_rate_monthly": 299,
            "leasing_start": "2026-01-01", "leasing_end": "2028-12-31",
        },
    )
    costs = client.get("/api/stats/summary?start_date=2026-01-01&end_date=2026-03-31").json()["vehicle_costs"]
    assert costs["ownership_cost"] == pytest.approx(3 * 299)
    assert costs["by_category"][0]["category"] == "leasing"


def test_deleting_vehicle_and_user_removes_costs(client):
    register(client, username="admin")
    register(client, username="opfer")
    vehicle = create_vehicle(client)
    cost = client.post(
        "/api/vehicle-costs", json={"vehicle_id": vehicle["id"], "amount": 10, "start_date": "2026-01-01"}
    ).json()
    assert client.delete(f"/api/vehicles/{vehicle['id']}").status_code == 204
    kinds = {(d["entity_type"], d["entity_id"]) for d in client.get("/api/sync/deletions").json()["deletions"]}
    assert ("vehicle_cost", cost["id"]) in kinds

    second = create_vehicle(client, external_id="zwei")
    client.post("/api/vehicle-costs", json={"vehicle_id": second["id"], "amount": 10, "start_date": "2026-01-01"})
    login(client, "admin", "geheim1234")
    victim = next(u for u in client.get("/api/auth/users").json() if u["username"] == "opfer")
    assert client.delete(f"/api/auth/users/{victim['id']}").status_code == 204


# ---------- Backup ----------


def _import(client, content):
    response = client.post(
        "/api/backup/import", files={"file": ("backup.zip", io.BytesIO(content), "application/zip")}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_backup_roundtrip_with_costs_and_ownership(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)
    client.patch(
        f"/api/vehicles/{vehicle['id']}",
        json={"ownership": "purchase", "purchase_price": 35000, "purchase_date": "2025-04-01"},
    )
    client.post(
        "/api/vehicle-costs",
        json={"vehicle_id": vehicle["id"], "amount": 120, "category": "tax", "interval": "yearly",
              "start_date": "2025-04-01", "label": "Steuer", "spread_months": 12},
    )
    archive = client.get("/api/backup/export").content
    assert "costs.csv" in zipfile.ZipFile(io.BytesIO(archive)).namelist()

    register(client, username="zweite")
    first = _import(client, archive)
    second = _import(client, archive)
    assert first["costs_imported"] == 1
    assert second["costs_imported"] == 0 and second["costs_skipped"] == 1

    vehicles = client.get("/api/vehicles").json()
    assert vehicles[0]["ownership"] == "purchase"
    assert vehicles[0]["purchase_price"] == 35000
    costs = client.get("/api/vehicle-costs").json()
    assert len(costs) == 1
    assert costs[0]["vehicle_id"] == vehicles[0]["id"]
    assert costs[0]["label"] == "Steuer"
    assert costs[0]["spread_months"] == 12


def test_backup_without_costs_csv_still_imports(client):
    register(client, username="erste")
    create_vehicle(client)
    archive = client.get("/api/backup/export").content
    source = zipfile.ZipFile(io.BytesIO(archive))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as old:
        for name in source.namelist():
            if name != "costs.csv":
                old.writestr(name, source.read(name))
    register(client, username="zweite")
    result = _import(client, buf.getvalue())
    assert result["vehicles_imported"] == 1
    assert result["costs_imported"] == 0
    assert client.get("/api/vehicles").json()[0]["ownership"] == "none"
