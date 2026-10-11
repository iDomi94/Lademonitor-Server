"""Jahres- und Monatsrueckblick (review.py, GET /api/stats/review) samt
Vergleichsfahrzeug und Jahresmail.

Die Regeln stehen in `ideen/schritt-11-vertrag.md` - die Apps rechnen sie
lokal nach. Jeder Test hier haelt eine davon fest.
"""

from datetime import date, timedelta

import pytest

from conftest import create_vehicle, register


def _session(client, vehicle, start, *, kwh=None, price=None, km=None, provider=None,
             location=None, place=None, temp=None, ctype=None):
    payload = {"vehicle_id": vehicle["id"], "start_time": start}
    for key, value in (
        ("energy_kwh", kwh), ("price_total", price), ("odometer_km", km),
        ("provider_id", provider["id"] if provider else None),
        ("location_id", location["id"] if location else None),
        ("geocoded_place", place), ("outside_temp_c", temp), ("charging_type", ctype),
    ):
        if value is not None:
            payload[key] = value
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _review(client, **params):
    response = client.get("/api/stats/review", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- Zeitraum und grosse Zahlen ----------


def test_km_count_from_the_last_reading_before_the_period(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2025-12-20T10:00:00", kwh=20, price=6, km=10000)
    _session(client, car, "2026-01-10T10:00:00", kwh=30, price=9, km=10400)
    _session(client, car, "2026-12-31T23:00:00", kwh=10, price=3, km=12000)

    r = _review(client, period="year", year=2026)

    # Anker ist der Stand vom Dezember, beide Enden des Jahres inklusive.
    assert r["totals"]["km"] == 2000
    assert r["totals"]["sessions"] == 2
    assert r["totals"]["kwh"] == 40
    assert r["start_date"] == "2026-01-01" and r["end_date"] == "2026-12-31"
    assert r["totals"]["price_per_100km"] == pytest.approx(12 / 2000 * 100)
    assert r["totals"]["avg_price_per_kwh"] == pytest.approx(12 / 40)


def test_km_without_anchor_uses_the_first_reading_and_sum_over_vehicles(client):
    register(client)
    a = create_vehicle(client, external_id="a", name="A")
    b = create_vehicle(client, external_id="b", name="B")
    _session(client, a, "2026-03-01T10:00:00", kwh=10, km=5000)
    _session(client, a, "2026-04-01T10:00:00", kwh=10, km=5600)
    _session(client, b, "2026-03-05T10:00:00", kwh=10, km=100)
    _session(client, b, "2026-03-25T10:00:00", kwh=10, km=400)
    # Zahlendreher: fallender Stand wird 0, nicht negativ.
    c = create_vehicle(client, external_id="c", name="C")
    _session(client, c, "2026-03-05T10:00:00", kwh=10, km=9000)
    _session(client, c, "2026-03-06T10:00:00", kwh=10, km=8000)

    r = _review(client, period="year", year=2026)
    assert r["totals"]["km"] == 600 + 300

    only_b = _review(client, period="year", year=2026, vehicle_id=b["id"])
    assert only_b["totals"]["km"] == 300
    assert only_b["vehicle_id"] == b["id"]


def test_cost_includes_fee_share_and_unallocated_periods_only_without_filter(client):
    register(client)
    car = create_vehicle(client)
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    _session(client, car, "2026-05-10T10:00:00", kwh=40, price=20, provider=ionity, km=1000)
    # Monatsabo ab 01.05.: Mai hat einen Vorgang (Anteil 10), Juni keinen
    # (nicht umgelegt) - beides zaehlt im Jahr, Juni nur ohne Fahrzeugfilter.
    fee = client.post("/api/provider-fees", json={
        "provider_id": ionity["id"], "amount": 10.0, "interval": "once",
        "start_date": "2026-05-01", "end_date": "2026-05-31",
    })
    assert fee.status_code == 201, fee.text
    fee = client.post("/api/provider-fees", json={
        "provider_id": ionity["id"], "amount": 7.0, "interval": "once",
        "start_date": "2026-06-01", "end_date": "2026-06-30",
    })
    assert fee.status_code == 201, fee.text

    r = _review(client, period="year", year=2026)
    assert r["totals"]["cost"] == pytest.approx(20 + 10 + 7)
    june = next(m for m in r["monthly"] if m["month"] == 6)
    assert june["cost"] == pytest.approx(7) and june["sessions"] == 0

    filtered = _review(client, period="year", year=2026, vehicle_id=car["id"])
    assert filtered["totals"]["cost"] == pytest.approx(30)

    month = _review(client, period="month", year=2026, month=6)
    assert month["totals"]["cost"] == pytest.approx(7)
    assert month["totals"]["avg_price_per_kwh"] is None


def test_previous_is_null_without_sessions_and_filled_otherwise(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-03-15T10:00:00", kwh=10, price=4)

    assert _review(client, period="year", year=2026)["previous"] is None
    assert _review(client, period="month", year=2026, month=3)["previous"] is None

    _session(client, car, "2026-02-28T10:00:00", kwh=20, price=6)
    march = _review(client, period="month", year=2026, month=3)
    assert march["previous"]["sessions"] == 1
    assert march["previous"]["kwh"] == 20

    # Januar: Vormonat ist der Dezember des Vorjahres.
    _session(client, car, "2025-12-31T10:00:00", kwh=5, price=2)
    january = _review(client, period="month", year=2026, month=1)
    assert january["previous"]["kwh"] == 5
    assert _review(client, period="year", year=2026)["previous"]["kwh"] == 5


def test_avg_consumption_is_km_weighted_over_the_full_chain(client):
    """Der erste Vorgang im Zeitraum bekommt seinen Vorgaenger aus dem
    Vorjahr - der Verbrauch wird ueber die ganze Historie gerechnet."""
    register(client)
    car = create_vehicle(client, capacity=None)
    _session(client, car, "2025-12-01T10:00:00", kwh=10, km=1000)
    _session(client, car, "2026-01-05T10:00:00", kwh=20, km=1100)  # 20 kWh/100 km
    _session(client, car, "2026-02-05T10:00:00", kwh=30, km=1400)  # 10 kWh/100 km

    r = _review(client, period="year", year=2026)
    assert r["totals"]["avg_consumption_kwh_per_100km"] == pytest.approx((20 * 100 + 10 * 300) / 400)
    months = {m["month"]: m["avg_consumption_kwh_per_100km"] for m in r["monthly"]}
    assert months[1] == pytest.approx(20) and months[2] == pytest.approx(10) and months[3] is None


def test_monthly_has_twelve_rows_only_for_the_year(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-07-01T10:00:00", kwh=10, price=3)

    year = _review(client, period="year", year=2026)
    assert [m["month"] for m in year["monthly"]] == list(range(1, 13))
    assert sum(m["sessions"] for m in year["monthly"]) == 1
    assert year["month"] is None

    month = _review(client, period="month", year=2026, month=7)
    assert month["monthly"] == [] and month["month"] == 7
    assert month["records"]["lowest_consumption_month"] is None


# ---------- Ersparnis ----------


def test_savings_math_with_defaults_and_null_without_km(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-03-01T10:00:00", kwh=10, price=5, km=1000)
    assert _review(client, period="month", year=2026, month=3)["savings"] is None

    _session(client, car, "2026-03-20T10:00:00", kwh=150, price=45, km=2000)
    s = _review(client, period="month", year=2026, month=3)["savings"]
    liters = 1000 * 7.0 / 100
    assert s["configured"] is False and s["fuel_type"] == "petrol"
    assert s["fuel_liters"] == pytest.approx(liters)
    assert s["fuel_cost"] == pytest.approx(liters * 1.75)
    assert s["charging_cost"] == pytest.approx(50)
    assert s["saved_eur"] == pytest.approx(liters * 1.75 - 50)
    assert s["co2_combustion_kg"] == pytest.approx(liters * 2.37)
    assert s["co2_ev_kg"] == pytest.approx(160 * 0.36)
    assert s["saved_co2_kg"] == pytest.approx(liters * 2.37 - 160 * 0.36)


def test_savings_use_the_saved_comparison_vehicle_and_may_be_negative(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-03-01T10:00:00", kwh=10, km=1000)
    _session(client, car, "2026-03-20T10:00:00", kwh=20, price=500, km=1100)
    client.put("/api/combustion-vehicle", json={
        "fuel_type": "diesel", "consumption_l_per_100km": 5.0,
        "fuel_price_per_l": 2.0, "grid_co2_kg_per_kwh": 0.5,
    })

    s = _review(client, period="month", year=2026, month=3)["savings"]
    assert s["configured"] is True and s["fuel_type"] == "diesel"
    assert s["fuel_cost"] == pytest.approx(5 * 2.0)
    assert s["saved_eur"] == pytest.approx(10 - 500)
    assert s["co2_combustion_kg"] == pytest.approx(5 * 2.65)
    assert s["co2_ev_kg"] == pytest.approx(30 * 0.5)


# ---------- Rekorde ----------


def test_price_records_need_two_candidates_and_ignore_fees_and_small_charges(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-04-01T10:00:00", kwh=20, price=6)       # 0,30
    # Unter 1 kWh zaehlt nicht, ohne Preis auch nicht.
    _session(client, car, "2026-04-02T10:00:00", kwh=0.5, price=5)
    _session(client, car, "2026-04-03T10:00:00", kwh=30)
    r = _review(client, period="month", year=2026, month=4)["records"]
    assert r["cheapest_per_kwh"] is None and r["most_expensive_per_kwh"] is None

    _session(client, car, "2026-04-04T10:00:00", kwh=10, price=7, place="Raststaette")  # 0,70
    r = _review(client, period="month", year=2026, month=4)["records"]
    assert r["cheapest_per_kwh"]["value"] == pytest.approx(0.30)
    assert r["most_expensive_per_kwh"]["value"] == pytest.approx(0.70)
    assert r["most_expensive_per_kwh"]["place"] == "Raststaette"
    assert r["most_expensive_per_kwh"]["vehicle_name"] == "Enyaq"


def test_session_record_ties_go_to_the_earlier_session(client):
    register(client)
    car = create_vehicle(client)
    first = _session(client, car, "2026-04-01T10:00:00", kwh=40, price=12, temp=5)
    _session(client, car, "2026-04-10T10:00:00", kwh=40, price=12, temp=5)

    r = _review(client, period="month", year=2026, month=4)["records"]
    assert r["biggest_charge"]["session_id"] == first["id"]
    assert r["cheapest_per_kwh"]["session_id"] == first["id"]
    assert r["most_expensive_per_kwh"]["session_id"] == first["id"]
    assert r["coldest"]["session_id"] == first["id"]
    assert r["warmest"]["session_id"] == first["id"]


def test_temperature_records_need_two_values(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-01-05T10:00:00", kwh=10, temp=-4.5)
    _session(client, car, "2026-01-06T10:00:00", kwh=10)
    r = _review(client, period="year", year=2026)["records"]
    assert r["coldest"] is None and r["warmest"] is None

    _session(client, car, "2026-07-06T10:00:00", kwh=10, temp=31)
    r = _review(client, period="year", year=2026)["records"]
    assert r["coldest"]["value"] == -4.5
    assert r["warmest"]["value"] == 31
    assert r["biggest_charge"]["value"] == 10


def test_longest_distance_may_start_before_the_period(client):
    register(client)
    a = create_vehicle(client, external_id="a", name="A")
    b = create_vehicle(client, external_id="b", name="B")
    _session(client, a, "2025-12-28T10:00:00", kwh=10, km=1000)
    _session(client, b, "2026-01-02T10:00:00", kwh=10, km=50000)
    _session(client, a, "2026-01-03T10:00:00", kwh=10)  # ohne Stand: uebersprungen
    _session(client, a, "2026-01-04T10:00:00", kwh=10, km=1450)
    _session(client, a, "2026-01-20T10:00:00", kwh=10, km=1700)

    r = _review(client, period="year", year=2026)["records"]["longest_distance"]
    assert r["value"] == 450
    assert r["previous_start_time"].startswith("2025-12-28")
    assert r["vehicle_name"] == "A"


def test_top_location_and_provider_with_tie_breaks(client):
    register(client)
    car = create_vehicle(client)
    home = client.post("/api/locations", json={"name": "Zuhause", "latitude": 48.0, "longitude": 9.0}).json()
    wallbox = client.post("/api/providers", json={"name": "Wallbox"}).json()
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    # Zwei Vorgaenge "Zuhause" (Ladeort), zwei "Leonberg" (geocoded_place)
    # mit mehr kWh, einer ohne beides (zaehlt nicht).
    _session(client, car, "2026-02-01T10:00:00", kwh=10, location=home, provider=wallbox)
    _session(client, car, "2026-02-02T10:00:00", kwh=10, location=home, provider=wallbox)
    _session(client, car, "2026-02-03T10:00:00", kwh=40, place="Leonberg", provider=ionity)
    _session(client, car, "2026-02-04T10:00:00", kwh=40, place="Leonberg", provider=ionity)
    _session(client, car, "2026-02-05T10:00:00", kwh=99)

    r = _review(client, period="month", year=2026, month=2)["records"]
    assert r["top_location"] == {"name": "Leonberg", "sessions": 2, "kwh": 80}
    assert r["top_provider"] == {"name": "Ionity", "sessions": 2, "kwh": 80}
    # Ort im Vorgangs-Rekord: Ladeort-Name vor allem anderen.
    assert r["biggest_charge"]["place"] is None


def test_rank_tie_with_equal_kwh_is_alphabetical(client):
    register(client)
    car = create_vehicle(client)
    _session(client, car, "2026-02-01T10:00:00", kwh=10, place="Stuttgart")
    _session(client, car, "2026-02-02T10:00:00", kwh=10, place="Aalen")
    assert _review(client, period="month", year=2026, month=2)["records"]["top_location"]["name"] == "Aalen"


def test_consumption_month_records_need_two_months(client):
    register(client)
    car = create_vehicle(client, capacity=None)
    _session(client, car, "2026-01-01T10:00:00", kwh=10, km=1000)
    _session(client, car, "2026-01-20T10:00:00", kwh=20, km=1100)  # Jan 20
    r = _review(client, period="year", year=2026)["records"]
    assert r["lowest_consumption_month"] is None

    _session(client, car, "2026-03-20T10:00:00", kwh=15, km=1200)  # Mrz 15
    _session(client, car, "2026-05-20T10:00:00", kwh=15, km=1300)  # Mai 15 (Gleichstand)
    r = _review(client, period="year", year=2026)["records"]
    assert r["highest_consumption_month"] == {"month": 1, "value": pytest.approx(20)}
    assert r["lowest_consumption_month"] == {"month": 3, "value": pytest.approx(15)}


# ---------- Verteilung ----------


def test_split_acdc_and_by_place_rule(client):
    register(client)
    car = create_vehicle(client)
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    _session(client, car, "2026-06-01T10:00:00", kwh=30, price=15, provider=ionity, ctype="DC")
    _session(client, car, "2026-06-02T10:00:00", kwh=10, price=3, ctype="AC")
    _session(client, car, "2026-06-03T10:00:00", kwh=99, price=3)  # ohne Lade-Art

    split = _review(client, period="month", year=2026, month=6)["split"]
    assert split["ac_kwh"] == 10 and split["dc_kwh"] == 30
    assert split["ac_share_pct"] == pytest.approx(25)
    assert split["dc_share_pct"] == pytest.approx(75)
    assert split["by_place"] == []

    home = client.post("/api/providers", json={"name": "Wallbox", "charging_place": "home"}).json()
    _session(client, car, "2026-06-04T10:00:00", kwh=20, price=6, provider=home, ctype="AC")
    places = {p["place"]: p for p in _review(client, period="month", year=2026, month=6)["split"]["by_place"]}
    assert places["home"]["total_kwh"] == 20
    assert places["public"]["session_count"] == 3


def test_empty_period(client):
    register(client)
    r = _review(client, period="year", year=2026)
    assert r["totals"]["sessions"] == 0
    assert r["totals"]["avg_consumption_kwh_per_100km"] is None
    assert r["savings"] is None and r["previous"] is None
    assert r["split"]["ac_share_pct"] is None
    assert all(v is None for v in r["records"].values())


# ---------- Vergleichsfahrzeug ----------


def test_combustion_vehicle_defaults_and_put(client):
    register(client)
    default = client.get("/api/combustion-vehicle").json()
    assert default == {
        "fuel_type": "petrol", "consumption_l_per_100km": 7.0, "fuel_price_per_l": 1.75,
        "grid_co2_kg_per_kwh": 0.36, "configured": False, "updated_at": None,
    }

    saved = client.put("/api/combustion-vehicle", json={
        "fuel_type": "diesel", "consumption_l_per_100km": 5.8,
        "fuel_price_per_l": 1.65, "grid_co2_kg_per_kwh": 0.4,
    })
    assert saved.status_code == 200, saved.text
    body = client.get("/api/combustion-vehicle").json()
    assert body["configured"] is True and body["fuel_type"] == "diesel"
    assert body["grid_co2_kg_per_kwh"] == 0.4 and body["updated_at"] is not None


@pytest.mark.parametrize("payload", [
    {"fuel_type": "petrol", "consumption_l_per_100km": 7.0, "fuel_price_per_l": 1.75},  # Feld fehlt
    {"fuel_type": "lpg", "consumption_l_per_100km": 7.0, "fuel_price_per_l": 1.75, "grid_co2_kg_per_kwh": 0.3},
    {"fuel_type": "petrol", "consumption_l_per_100km": 31, "fuel_price_per_l": 1.75, "grid_co2_kg_per_kwh": 0.3},
    {"fuel_type": "petrol", "consumption_l_per_100km": 7, "fuel_price_per_l": 0.4, "grid_co2_kg_per_kwh": 0.3},
    {"fuel_type": "petrol", "consumption_l_per_100km": 7, "fuel_price_per_l": 1.7, "grid_co2_kg_per_kwh": 1.6},
])
def test_combustion_vehicle_put_validates(client, payload):
    register(client)
    assert client.put("/api/combustion-vehicle", json=payload).status_code == 422


def test_combustion_vehicle_requires_login(client):
    assert client.get("/api/combustion-vehicle").status_code == 401


# ---------- Benachrichtigungen ----------


def _notification_body(**extra):
    body = {
        "notify_backup_failed": True, "notify_myskoda_error": True, "notify_monthly_report": False,
        "notify_new_registration": True, "review_digest": "off",
    }
    body.update(extra)
    return body


def test_yearly_review_toggle_defaults_on_and_is_optional(client):
    register(client)
    assert client.get("/api/auth/me").json()["notify_yearly_review"] is True

    client.put("/api/auth/notifications", json=_notification_body(notify_yearly_review=False))
    assert client.get("/api/auth/me").json()["notify_yearly_review"] is False
    # Aeltere Clients schicken das Feld nicht - der Wert bleibt.
    client.put("/api/auth/notifications", json=_notification_body())
    assert client.get("/api/auth/me").json()["notify_yearly_review"] is False


def test_yearly_mail_sent_once_and_marker_written(client, db_session, monkeypatch):
    from app import mailer, models, notifications

    register(client, email="a@example.org")
    car = create_vehicle(client)
    _session(client, car, "2025-05-01T10:00:00", kwh=40, price=12, km=1000)
    _session(client, car, "2025-06-01T10:00:00", kwh=30, price=9, km=1300)

    sent = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: sent.append(kw) or True)
    notifications._run_yearly_reviews(db_session, today=date(2026, 1, 1))
    notifications._run_yearly_reviews(db_session, today=date(2026, 1, 1))

    assert len(sent) == 1 and sent[0]["kind"] == "yearly_review"
    assert "2025" in sent[0]["mail"].subject
    labels = [label for label, _ in sent[0]["mail"].rows]
    assert "Größte Ladung" in labels
    html, text = mailer.render(sent[0]["mail"], "de")
    assert "Jahresrückblick 2025" in html and "Gespart" in text
    user = db_session.query(models.User).one()
    db_session.refresh(user)
    assert user.last_yearly_review_year == 2025


def test_yearly_mail_skips_empty_years_and_late_installs(client, db_session, monkeypatch):
    from app import mailer, models, notifications

    register(client, email="a@example.org")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: sent.append(kw) or True)

    # Kein Vorgang im Vorjahr: nichts verschickt, Merker trotzdem gesetzt.
    notifications._run_yearly_reviews(db_session, today=date(2026, 1, 2))
    user = db_session.query(models.User).one()
    db_session.refresh(user)
    assert sent == [] and user.last_yearly_review_year == 2025

    # Update im Oktober: kein Rueckblick auf ein laengst vergangenes Jahr.
    car = create_vehicle(client)
    _session(client, car, "2026-05-01T10:00:00", kwh=40, price=12)
    notifications._run_yearly_reviews(db_session, today=date(2027, 10, 11))
    db_session.refresh(user)
    assert sent == [] and user.last_yearly_review_year == 2026


def test_monthly_report_contains_review_content(client, db_session, monkeypatch):
    from app import mailer, models, notifications

    register(client, email="a@example.org")
    me = db_session.query(models.User).one()
    me.notify_monthly_report = True
    db_session.commit()
    car = create_vehicle(client)
    today = date.today()
    first = today.replace(day=1)
    prev_month_end = first - timedelta(days=1)
    two_back = prev_month_end.replace(day=1) - timedelta(days=1)
    _session(client, car, f"{two_back.isoformat()}T10:00:00", kwh=20, price=6, km=1000)
    _session(client, car, f"{prev_month_end.replace(day=1).isoformat()}T10:00:00", kwh=40, price=12, km=1300)
    _session(client, car, f"{prev_month_end.isoformat()}T09:00:00", kwh=10, price=5, km=1400)

    sent = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: sent.append(kw) or True)
    notifications._run_monthly_reports(db_session)

    assert len(sent) == 1
    values = dict(sent[0]["mail"].rows)
    # Vormonat: 20 kWh, dieser Monat 50 kWh -> +150 %.
    assert any("+150 % ggü. Vormonat" in v for v in values.values())
    assert "Gespart ggü. Verbrenner" in values
    assert "Größte Ladung" in values


# ---------- Web ----------


def test_review_page_renders_with_only_relative_paths(client):
    register(client)

    response = client.get("/review")

    assert response.status_code == 200
    assert "api/stats/review" in response.text
    assert "'/api/" not in response.text and '"/api/' not in response.text
    assert "review." not in response.text.replace("api/stats/review", "")


def test_review_page_requires_login(client):
    response = client.get("/review", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "login"


def test_dashboard_has_the_review_tile_and_settings_the_new_panels(client):
    register(client)

    assert 'id="review-tile"' in client.get("/").text
    settings = client.get("/settings").text
    assert 'id="combustion-panel"' in settings
    assert 'id="n-yearly"' in settings
    assert "api/combustion-vehicle" in settings
