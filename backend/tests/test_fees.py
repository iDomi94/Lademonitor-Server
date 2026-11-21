import pytest
"""Grundgebuehren/Abos der Anbieter (fees.py, routers/provider_fees.py).

Die Regeln hier sind Vertrag: beide Apps rechnen die Umlage lokal nach
(LocalFeeAllocator), weil ihr Dashboard immer lokal entsteht. Aendert sich
hier ein Ergebnis, muss es sich dort genauso aendern - sonst zeigen App und
Web unterschiedliche Gesamtkosten.
"""

import io
import zipfile
from datetime import date, datetime
from types import SimpleNamespace

from conftest import create_vehicle, login, register

from app import fees, models


def _fee(start, interval="monthly", end=None, amount=15.0, provider_id="p1", fee_id="f1"):
    return SimpleNamespace(
        id=fee_id,
        provider_id=provider_id,
        amount=amount,
        interval=models.FeeInterval(interval),
        start_date=datetime.fromisoformat(start),
        end_date=datetime.fromisoformat(end) if end else None,
    )


def _session(sid, start, kwh, provider_id="p1"):
    return SimpleNamespace(
        id=sid, provider_id=provider_id, start_time=datetime.fromisoformat(start), energy_kwh=kwh
    )


# ---------- Perioden ----------


def test_monthly_periods_are_half_open_and_stop_today():
    periods = fees.periods(_fee("2026-05-03"), until=date(2026, 7, 10))

    assert [(p.start, p.end) for p in periods] == [
        (date(2026, 5, 3), date(2026, 6, 3)),
        (date(2026, 6, 3), date(2026, 7, 3)),
        (date(2026, 7, 3), date(2026, 8, 3)),  # angebrochen - zaehlt voll
    ]


def test_month_end_anchor_does_not_drift():
    """Ab dem 31.01.: 28.02., 31.03. - nicht dauerhaft auf den 28. gerutscht."""
    starts = [p.start for p in fees.periods(_fee("2026-01-31"), until=date(2026, 4, 30))]

    assert starts == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_cancelled_fee_keeps_its_last_period_but_clips_it():
    periods = fees.periods(_fee("2026-05-03", end="2026-06-15"), until=date(2026, 12, 31))

    assert [(p.start, p.end) for p in periods] == [
        (date(2026, 5, 3), date(2026, 6, 3)),
        (date(2026, 6, 3), date(2026, 6, 16)),
    ]
    assert all(p.amount == 15.0 for p in periods)


def test_yearly_and_once():
    yearly = fees.periods(_fee("2025-03-01", "yearly"), until=date(2026, 9, 1))
    once = fees.periods(_fee("2026-05-03", "once", end="2026-06-02"), until=date(2026, 9, 1))

    assert [p.start for p in yearly] == [date(2025, 3, 1), date(2026, 3, 1)]
    assert [(p.start, p.end) for p in once] == [(date(2026, 5, 3), date(2026, 6, 3))]


def test_future_fee_has_no_period_yet():
    assert fees.periods(_fee("2026-10-01"), until=date(2026, 9, 22)) == []


# ---------- Umlage ----------


def test_split_by_kwh():
    """Das Beispiel aus der Anfrage: 15 EUR auf 40/20/15 kWh -> 8/4/3 EUR."""
    sessions = [
        _session("a", "2026-05-04T10:00:00", 40),
        _session("b", "2026-05-20T10:00:00", 20),
        _session("c", "2026-06-02T23:59:00", 15),
        _session("d", "2026-06-03T08:00:00", 30),  # naechste Periode
    ]

    result = fees.allocate([_fee("2026-05-03")], sessions, until=date(2026, 6, 10))

    assert result.shares == {"a": 8.0, "b": 4.0, "c": 3.0, "d": 15.0}
    assert result.unallocated == []


def test_rounding_remainder_goes_to_the_biggest_session():
    sessions = [_session(sid, "2026-05-05T10:00:00", 10) for sid in ("a", "b", "c")]
    sessions[1].energy_kwh = 11

    shares = fees.allocate([_fee("2026-05-03", amount=10.0)], sessions, until=date(2026, 5, 31)).shares

    assert round(sum(shares.values()), 2) == 10.0
    assert shares["b"] == max(shares.values())


def test_sessions_without_kwh_split_evenly_only_if_none_has_kwh():
    without = [_session("a", "2026-05-05T10:00:00", None), _session("b", "2026-05-06T10:00:00", None)]
    mixed = [_session("c", "2026-05-05T10:00:00", None), _session("d", "2026-05-06T10:00:00", 30)]

    assert fees.allocate([_fee("2026-05-03")], without, until=date(2026, 5, 31)).shares == {
        "a": 7.5,
        "b": 7.5,
    }
    assert fees.allocate([_fee("2026-05-03")], mixed, until=date(2026, 5, 31)).shares == {
        "c": 0.0,
        "d": 15.0,
    }


def test_period_without_sessions_is_unallocated():
    result = fees.allocate(
        [_fee("2026-05-03")], [_session("a", "2026-06-10T10:00:00", 20)], until=date(2026, 6, 20)
    )

    assert result.shares == {"a": 15.0}
    assert [p.start for p in result.unallocated] == [date(2026, 5, 3)]


def test_other_providers_are_untouched():
    sessions = [
        _session("ionity", "2026-05-05T10:00:00", 30),
        _session("enbw", "2026-05-06T10:00:00", 30, provider_id="p2"),
    ]

    shares = fees.allocate([_fee("2026-05-03")], sessions, until=date(2026, 5, 31)).shares

    assert shares == {"ionity": 15.0}


# ---------- API und Statistik ----------


def _seed(client):
    vehicle = create_vehicle(client)
    ionity = client.post("/api/providers", json={"name": "Ionity"}).json()
    enbw = client.post("/api/providers", json={"name": "EnBW"}).json()
    for start, kwh, km, provider, price in (
        ("2026-05-01T08:00:00", 10.0, 10000, enbw, 5.0),
        ("2026-05-04T10:00:00", 40.0, 10300, ionity, 15.6),
        ("2026-05-20T10:00:00", 20.0, 10500, ionity, 7.8),
        ("2026-06-01T10:00:00", 15.0, 10600, ionity, 5.85),
    ):
        response = client.post(
            "/api/sessions",
            json={
                "vehicle_id": vehicle["id"],
                "provider_id": provider["id"],
                "start_time": start,
                "energy_kwh": kwh,
                "odometer_km": km,
                "price_total": price,
            },
        )
        assert response.status_code == 201, response.text
    fee = client.post(
        "/api/provider-fees",
        json={
            "provider_id": ionity["id"],
            "amount": 15.0,
            "interval": "once",
            "start_date": "2026-05-03",
            "end_date": "2026-06-02",
            "label": "Powerpass",
        },
    )
    assert fee.status_code == 201, fee.text
    return vehicle, ionity, enbw, fee.json()


def test_sessions_carry_their_share_and_keep_the_charger_price(client):
    register(client)
    _seed(client)

    by_start = {s["start_time"][:10]: s for s in client.get("/api/sessions").json()}

    assert by_start["2026-05-04"]["fee_share"] == 8.0
    assert by_start["2026-05-04"]["price_total"] == 15.6
    assert by_start["2026-05-20"]["fee_share"] == 4.0
    assert by_start["2026-06-01"]["fee_share"] == 3.0
    assert by_start["2026-05-01"]["fee_share"] is None


def test_stats_include_fees_everywhere(client):
    register(client)
    _seed(client)

    stats = client.get("/api/stats/summary").json()

    charger = 5.0 + 15.6 + 7.8 + 5.85
    assert stats["total_fees"] == 15.0
    assert stats["unallocated_fees"] == 0
    assert stats["total_cost"] == round(charger + 15.0, 2)
    assert stats["avg_price_per_kwh"] == round((charger + 15.0) / 85.0, 4)
    # Preis je 100 km = Verbrauch x Ø Preis je kWh, die Gebuehren stecken
    # also ueber den Ø Preis mit drin (seit 0.49.1, siehe routers/stats.py).
    assert stats["price_per_100km"] == pytest.approx(
        stats["avg_consumption_kwh_per_100km"] * stats["avg_price_per_kwh"], abs=0.02
    )
    ionity = next(p for p in stats["by_provider"] if p["provider_name"] == "Ionity")
    assert ionity["total_cost"] == round(15.6 + 7.8 + 5.85 + 15.0, 2)
    assert ionity["total_fees"] == 15.0
    months = {m["month"]: m for m in stats["monthly"]}
    assert months["2026-05"]["total_fees"] == 12.0
    assert months["2026-06"]["total_fees"] == 3.0


def test_date_filter_applies_after_the_split(client):
    """Ein Filter auf Juni darf dem Juni-Vorgang nicht die ganze Gebuehr
    zuschieben - er behaelt seine 3 EUR."""
    register(client)
    _seed(client)

    stats = client.get("/api/stats/summary?start_date=2026-06-01").json()

    assert stats["total_fees"] == 3.0
    assert stats["total_cost"] == round(5.85 + 3.0, 2)


def test_period_without_sessions_still_counts(client):
    register(client)
    _, ionity, _, _ = _seed(client)
    client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 11.99, "interval": "once",
              "start_date": "2026-08-01", "end_date": "2026-08-31"},
    )

    stats = client.get("/api/stats/summary").json()
    vehicle_filtered = client.get(f"/api/stats/summary?vehicle_id={_['id']}").json()

    assert stats["unallocated_fees"] == 11.99
    assert stats["total_fees"] == round(15.0 + 11.99, 2)
    assert {m["month"]: m["total_fees"] for m in stats["monthly"]}["2026-08"] == 11.99
    # Gehoert zu keinem Fahrzeug - ein Fahrzeugfilter laesst sie weg.
    assert vehicle_filtered["unallocated_fees"] == 0


def test_validation(client):
    register(client)
    _, ionity, _, fee = _seed(client)
    other = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 5, "interval": "once", "start_date": "2026-05-03"},
    )
    backwards = client.patch(f"/api/provider-fees/{fee['id']}", json={"end_date": "2026-01-01"})
    zero = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 0, "start_date": "2026-05-03"},
    )

    assert other.status_code == 422
    assert backwards.status_code == 422
    # Negativ ist seit v0.56.0 eine Gutschrift, 0 bleibt sinnlos.
    assert zero.status_code == 422
    assert client.get("/api/provider-fees").json()[0]["end_date"].startswith("2026-06-02")


def test_time_of_day_is_dropped(client):
    register(client)
    _, ionity, _, _ = _seed(client)

    created = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 5, "start_date": "2026-07-01T13:45:00"},
    ).json()

    assert created["start_date"] == "2026-07-01T00:00:00"


def test_fees_are_per_user(client):
    register(client, username="erste")
    _, ionity, _, fee = _seed(client)
    register(client, username="zweite")

    assert client.get("/api/provider-fees").json() == []
    assert client.delete(f"/api/provider-fees/{fee['id']}").status_code == 404
    foreign = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": 5, "start_date": "2026-05-03"},
    )
    assert foreign.status_code == 404


def test_deleting_fee_or_provider_leaves_tombstones(client):
    register(client)
    _, ionity, enbw, fee = _seed(client)
    second = client.post(
        "/api/provider-fees",
        json={"provider_id": enbw["id"], "amount": 4.99, "start_date": "2026-05-01"},
    ).json()

    assert client.delete(f"/api/provider-fees/{fee['id']}").status_code == 204
    assert client.delete(f"/api/providers/{enbw['id']}").status_code == 204

    deletions = {
        (d["entity_type"], d["entity_id"]) for d in client.get("/api/sync/deletions").json()["deletions"]
    }
    assert ("provider_fee", fee["id"]) in deletions
    assert ("provider_fee", second["id"]) in deletions
    assert client.get("/api/provider-fees").json() == []


def test_deleting_a_user_removes_their_fees(client):
    """Derselbe Fehlertyp wie 2026-09-09: eine vergessene Tabelle liesse das
    Loeschen des Kontos am Fremdschluessel scheitern (conftest schaltet die
    FK-Pruefung in SQLite ein)."""
    register(client, username="admin")
    register(client, username="opfer")
    _seed(client)
    login(client, "admin", "geheim1234")
    victim = next(u for u in client.get("/api/auth/users").json() if u["username"] == "opfer")

    assert client.delete(f"/api/auth/users/{victim['id']}").status_code == 204


# ---------- Backup ----------


def _import(client, content):
    response = client.post(
        "/api/backup/import",
        files={"file": ("backup.zip", io.BytesIO(content), "application/zip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_backup_roundtrip_with_fees(client):
    register(client, username="erste")
    _seed(client)
    archive = client.get("/api/backup/export").content
    assert "fees.csv" in zipfile.ZipFile(io.BytesIO(archive)).namelist()

    register(client, username="zweite")
    first = _import(client, archive)
    second = _import(client, archive)

    assert first["fees_imported"] == 1
    assert second["fees_imported"] == 0
    assert second["fees_skipped"] == 1
    fees_after = client.get("/api/provider-fees").json()
    providers = {p["id"]: p["name"] for p in client.get("/api/providers").json()}
    assert len(fees_after) == 1
    assert providers[fees_after[0]["provider_id"]] == "Ionity"
    assert fees_after[0]["label"] == "Powerpass"


def test_backup_without_fees_csv_still_imports(client):
    """Jede ZIP vor v0.27.0 hat keine fees.csv."""
    register(client, username="erste")
    _seed(client)
    archive = client.get("/api/backup/export").content
    source = zipfile.ZipFile(io.BytesIO(archive))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as old:
        for name in source.namelist():
            if name != "fees.csv":
                old.writestr(name, source.read(name))

    register(client, username="zweite")
    result = _import(client, buf.getvalue())

    assert result["sessions_imported"] == 4
    assert result["fees_imported"] == 0


# ---------- Gutschriften (Rabatt, Gutschein, seit v0.56.0) ----------


def test_credit_over_a_period_is_split_like_a_fee():
    result = fees.allocate(
        [_fee("2026-05-03", "once", end="2026-06-02", amount=-5.0)],
        [_session("a", "2026-05-04T10:00:00", 30), _session("b", "2026-05-20T10:00:00", 20)],
        until=date(2026, 6, 20),
    )

    assert result.shares == {"a": -3.0, "b": -2.0}


def test_single_session_goes_to_the_first_charge_from_the_start_day():
    fee = _fee("2026-05-10", "once", amount=-5.0)
    fee.single_session = True
    sessions = [
        _session("before", "2026-05-09T10:00:00", 30),
        _session("later", "2026-05-20T10:00:00", 20),
        _session("first", "2026-05-12T10:00:00", 40),
        _session("other", "2026-05-11T10:00:00", 40, provider_id="p2"),
    ]

    result = fees.allocate([fee], sessions, until=date(2026, 6, 20))

    assert result.shares == {"first": -5.0}
    assert result.unallocated == []


def test_single_session_without_a_charge_yet_counts_on_its_day():
    fee = _fee("2026-05-10", "once", amount=-5.0)
    fee.single_session = True

    result = fees.allocate([fee], [_session("before", "2026-05-09T10:00:00", 30)], until=date(2026, 6, 20))

    assert result.shares == {}
    assert [(p.start, p.amount) for p in result.unallocated] == [(date(2026, 5, 10), -5.0)]


def test_credit_api_months_and_single_session(client):
    register(client)
    vehicle, ionity, enbw, _ = _seed(client)

    months = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": -5.0, "interval": "once",
              "start_date": "2026-05-03", "spread_months": 1, "label": "Gutschein"},
    )
    session = client.post(
        "/api/provider-fees",
        json={"provider_id": enbw["id"], "amount": -2.0, "interval": "once",
              "start_date": "2026-04-20", "single_session": True},
    )
    assert months.status_code == 201, months.text
    assert session.status_code == 201, session.text
    # Monate -> Enddatum vom Server, damit aeltere Apps denselben Zeitraum sehen.
    assert months.json()["end_date"].startswith("2026-06-02")
    assert months.json()["spread_months"] == 1
    assert session.json()["end_date"] is None
    assert session.json()["single_session"] is True
    assert session.json()["charged_to_date"] == -2.0

    by_start = {s["start_time"][:10]: s for s in client.get("/api/sessions").json()}
    # Powerpass 15 EUR minus Gutschein 5 EUR, beide nach kWh 40/20/15.
    assert by_start["2026-05-04"]["fee_share"] == 8.0 - 2.67
    assert by_start["2026-05-01"]["fee_share"] == -2.0

    stats = client.get("/api/stats/summary").json()
    assert stats["total_fees"] == 15.0 - 5.0 - 2.0
    enbw_stat = next(p for p in stats["by_provider"] if p["provider_name"] == "EnBW")
    assert enbw_stat["total_cost"] == 3.0


def test_credit_patch_from_an_older_app_keeps_or_clears_the_new_fields(client):
    register(client)
    _, ionity, _, _ = _seed(client)
    fee = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": -5.0, "interval": "once",
              "start_date": "2026-05-03", "spread_months": 2},
    ).json()
    assert fee["end_date"].startswith("2026-07-02")

    # Aeltere App kennt die Felder nicht, schickt aber das Enddatum mit:
    # dann gilt dieses, die Monatsangabe faellt weg.
    changed = client.patch(
        f"/api/provider-fees/{fee['id']}",
        json={"amount": -6.0, "interval": "once", "start_date": "2026-05-03", "end_date": "2026-05-31"},
    ).json()
    assert changed["spread_months"] is None
    assert changed["end_date"].startswith("2026-05-31")

    single = client.post(
        "/api/provider-fees",
        json={"provider_id": ionity["id"], "amount": -3.0, "interval": "once",
              "start_date": "2026-05-03", "single_session": True},
    ).json()
    kept = client.patch(
        f"/api/provider-fees/{single['id']}",
        json={"amount": -4.0, "interval": "once", "start_date": "2026-05-03", "end_date": None},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["single_session"] is True

    # Nicht einmalig: die Zusatzfelder gelten nicht.
    monthly = client.patch(f"/api/provider-fees/{single['id']}", json={"interval": "monthly"}).json()
    assert monthly["single_session"] is False
