"""Erstattungsbericht fuer den Dienstwagen (reimbursement.py,
routers/reimbursement.py).

Die Regeln sind Vertrag: beide Apps rechnen den Bericht lokal nach
(LocalReimbursementCalculator).
"""

import io
import zipfile

from conftest import create_vehicle, register


def _provider(client, name, place="home", **extra):
    response = client.post("/api/providers", json={"name": name, "charging_place": place, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _session(client, vehicle, provider, start, kwh=None, price=None, **extra):
    payload = {
        "vehicle_id": vehicle["id"],
        "provider_id": provider["id"] if provider else None,
        "start_time": start,
        "energy_kwh": kwh,
        "price_total": price,
        **extra,
    }
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _company_car(client, **fields):
    vehicle = create_vehicle(client)
    response = client.patch(f"/api/vehicles/{vehicle['id']}", json={"is_company_car": True, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def _rate(client, vehicle, valid_from, rate):
    response = client.post(
        "/api/reimbursement/rates",
        json={"vehicle_id": vehicle["id"], "valid_from": valid_from, "rate_per_kwh": rate},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _report(client, vehicle, date_from, date_to, fmt="json"):
    response = client.get(
        "/api/reimbursement/report",
        params={"vehicle_id": vehicle["id"], "from": date_from, "to": date_to, "format": fmt},
    )
    assert response.status_code == 200, response.text
    return response.json() if fmt == "json" else response


def test_vehicle_fields_default_off_and_null_keeps_them(client):
    register(client)
    vehicle = create_vehicle(client)
    assert vehicle["is_company_car"] is False
    assert vehicle["reimbursement_mode"] == "flat"
    assert vehicle["reimbursement_monthly_mail"] is False

    changed = client.patch(
        f"/api/vehicles/{vehicle['id']}",
        json={"is_company_car": True, "reimbursement_mode": "actual", "license_plate": "S-AB 123"},
    ).json()
    assert changed["is_company_car"] is True and changed["license_plate"] == "S-AB 123"
    # null laesst Schalter und Art stehen (aeltere Apps), leert aber das Kennzeichen.
    kept = client.patch(
        f"/api/vehicles/{vehicle['id']}",
        json={"is_company_car": None, "reimbursement_mode": None, "license_plate": None},
    ).json()
    assert kept["is_company_car"] is True
    assert kept["reimbursement_mode"] == "actual"
    assert kept["license_plate"] is None


def test_only_home_sessions_of_this_vehicle_in_period(client):
    register(client)
    vehicle = _company_car(client)
    other = create_vehicle(client, external_id="zweit", name="Zweitwagen")
    home = _provider(client, "Wallbox")
    work = _provider(client, "Firma", place="work")
    ionity = _provider(client, "Ionity", place="public")
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)

    _session(client, vehicle, home, "2026-05-01T20:00:00", 30.0)
    _session(client, vehicle, home, "2026-05-31T23:30:00", 10.0)
    _session(client, vehicle, work, "2026-05-02T08:00:00", 20.0)
    _session(client, vehicle, ionity, "2026-05-03T10:00:00", 40.0)
    _session(client, vehicle, None, "2026-05-04T10:00:00", 5.0)
    _session(client, other, home, "2026-05-05T20:00:00", 25.0)
    _session(client, vehicle, home, "2026-06-01T00:10:00", 12.0)

    report = _report(client, vehicle, "2026-05-01", "2026-05-31")
    assert [r["energy_kwh"] for r in report["rows"]] == [30.0, 10.0]
    assert report["total_kwh"] == 40.0
    assert report["total_amount"] == round(30 * 0.34, 2) + round(10 * 0.34, 2)
    assert report["months"] == [
        {"month": "2026-05", "sessions": 2, "energy_kwh": 40.0, "estimated_kwh": 0.0,
         "amount": report["total_amount"]},
    ]


def test_rate_change_mid_year_does_not_touch_earlier_months(client):
    register(client)
    vehicle = _company_car(client)
    home = _provider(client, "Wallbox")
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.30)
    _rate(client, vehicle, "2026-07-01T00:00:00", 0.36)
    _session(client, vehicle, home, "2026-06-30T22:00:00", 10.0)
    _session(client, vehicle, home, "2026-07-01T06:00:00", 10.0)
    # Vor dem ersten Satz: kein Betrag, aber gezaehlt.
    _session(client, vehicle, home, "2025-12-31T20:00:00", 10.0)

    report = _report(client, vehicle, "2025-12-01", "2026-07-31")
    amounts = [r["amount"] for r in report["rows"]]
    assert amounts == [None, 3.0, 3.6]
    assert [r["rate_per_kwh"] for r in report["rows"]] == [None, 0.30, 0.36]
    assert report["missing_amount_sessions"] == 1
    assert report["total_amount"] == 6.6
    assert [m["month"] for m in report["months"]] == ["2025-12", "2026-06", "2026-07"]


def test_actual_costs_with_and_without_base_fee(client):
    register(client)
    vehicle = _company_car(client, reimbursement_mode="actual")
    home = _provider(client, "Wallbox")
    _session(client, vehicle, home, "2026-05-01T20:00:00", 30.0, price=9.0)
    _session(client, vehicle, home, "2026-05-10T20:00:00", 10.0, price=3.0)
    fee = client.post(
        "/api/provider-fees",
        json={"provider_id": home["id"], "amount": 8.0, "interval": "monthly",
              "start_date": "2026-05-01T00:00:00"},
    )
    assert fee.status_code == 201, fee.text

    without = _report(client, vehicle, "2026-05-01", "2026-05-31")
    assert [r["amount"] for r in without["rows"]] == [9.0, 3.0]
    assert [r["rate_per_kwh"] for r in without["rows"]] == [0.3, 0.3]
    assert without["include_fees"] is False

    client.patch(f"/api/vehicles/{vehicle['id']}", json={"reimbursement_include_fees": True})
    with_fees = _report(client, vehicle, "2026-05-01", "2026-05-31")
    # Grundgebuehr nach kWh umgelegt: 30/40 und 10/40 von 8 EUR.
    assert [r["fee_share"] for r in with_fees["rows"]] == [6.0, 2.0]
    assert [r["amount"] for r in with_fees["rows"]] == [15.0, 5.0]
    assert with_fees["total_amount"] == 20.0


def test_actual_falls_back_to_price_per_kwh_and_provider_price(client):
    register(client)
    vehicle = _company_car(client, reimbursement_mode="actual")
    home = _provider(client, "Wallbox")
    _session(client, vehicle, home, "2026-05-01T20:00:00", 20.0, price_per_kwh=0.25)
    client.patch(f"/api/providers/{home['id']}", json={"last_price_ac_per_kwh": 0.31})
    _session(client, vehicle, home, "2026-05-02T20:00:00", 10.0, charging_type="AC")
    report = _report(client, vehicle, "2026-05-01", "2026-05-31")
    amounts = [r["amount"] for r in report["rows"]]
    assert amounts[0] == 5.0
    # Ohne eigenen Preis: der des Heim-Anbieters (bzw. was beim Anlegen
    # daraus uebernommen wurde).
    assert amounts[1] == 3.1


def test_estimated_kwh_are_marked_and_counted(client):
    register(client)
    vehicle = _company_car(client)
    home = _provider(client, "Wallbox", energy_meter="charger")
    car = _provider(client, "Garage", energy_meter="vehicle")
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)
    _session(client, vehicle, home, "2026-05-01T20:00:00", 30.0)
    _session(client, vehicle, car, "2026-05-02T20:00:00", 20.0)
    # Ohne kWh, mit SoC: der Server schaetzt (SoC-Hub x Akkukapazitaet).
    estimated = _session(client, vehicle, home, "2026-05-03T20:00:00", soc_start=40, soc_end=80)
    assert estimated["energy_is_estimated"] is True

    report = _report(client, vehicle, "2026-05-01", "2026-05-31")
    assert [r["kwh_source"] for r in report["rows"]] == ["charger", "vehicle", "estimated"]
    assert report["estimated_sessions"] == 1
    assert report["estimated_kwh"] == estimated["energy_kwh"]
    assert report["months"][0]["estimated_kwh"] == estimated["energy_kwh"]


def test_csv_german_uses_semicolon_and_decimal_comma(client):
    register(client)
    vehicle = _company_car(client, employee_number="4711", license_plate="S-AB 123")
    home = _provider(client, "Wallbox")
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)
    _session(client, vehicle, home, "2026-05-01T20:15:00", 30.5, odometer_km=12345)

    response = _report(client, vehicle, "2026-05-01", "2026-05-31", fmt="csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert "lademonitor-erstattung-2026-05-01_2026-05-31.csv" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    assert "Personalnummer;4711" in text
    assert "S-AB 123" in text
    assert "2026-05-01;20:15;30,50;Wallbox;12345;0,3400;;10,37" in text
    assert "Summe;1;30,50;0,00;10,37" in text


def test_rates_are_per_user_and_leave_tombstones(client):
    register(client)
    vehicle = _company_car(client)
    rate = _rate(client, vehicle, "2026-01-01T12:34:00", 0.34)
    assert rate["valid_from"] == "2026-01-01T00:00:00"
    assert client.patch(f"/api/reimbursement/rates/{rate['id']}", json={"rate_per_kwh": 0.35}).json()[
        "rate_per_kwh"] == 0.35

    register(client, username="fremd")
    assert client.get("/api/reimbursement/rates").json() == []
    assert client.delete(f"/api/reimbursement/rates/{rate['id']}").status_code == 404
    assert client.get("/api/reimbursement/report", params={"vehicle_id": vehicle["id"]}).status_code == 404

    from conftest import login

    login(client, "tester", "geheim1234")
    assert client.delete(f"/api/reimbursement/rates/{rate['id']}").status_code == 204
    deletions = client.get("/api/sync/deletions").json()["deletions"]
    assert {"entity_type": "reimbursement_rate", "entity_id": rate["id"]}.items() <= next(
        d for d in deletions if d["entity_id"] == rate["id"]
    ).items()


def test_backup_roundtrip_keeps_company_car_and_rates(client):
    register(client)
    vehicle = _company_car(client, employee_name="Dominique", reimbursement_monthly_mail=True)
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)
    zip_bytes = client.get("/api/backup/export").content
    names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    assert "reimbursement_rates.csv" in names

    register(client, username="zweiter")
    result = client.post(
        "/api/backup/import", files={"file": ("backup.zip", zip_bytes, "application/zip")}
    ).json()
    assert result["rates_imported"] == 1
    imported = client.get("/api/vehicles").json()[0]
    assert imported["is_company_car"] is True
    assert imported["employee_name"] == "Dominique"
    assert imported["reimbursement_monthly_mail"] is True
    rates = client.get("/api/reimbursement/rates").json()
    assert [r["rate_per_kwh"] for r in rates] == [0.34]
    assert rates[0]["vehicle_id"] == imported["id"]

    again = client.post(
        "/api/backup/import", files={"file": ("backup.zip", zip_bytes, "application/zip")}
    ).json()
    assert again["rates_imported"] == 0


def test_deleting_vehicle_and_user_cleans_up_rates(client):
    register(client)
    vehicle = _company_car(client)
    rate = _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)
    assert client.delete(f"/api/vehicles/{vehicle['id']}").status_code == 204
    ids = {d["entity_id"] for d in client.get("/api/sync/deletions").json()["deletions"]}
    assert rate["id"] in ids

    # Ein Konto mit Saetzen laesst sich loeschen (_purge_owned_data).
    register(client, username="fahrer")
    vehicle = _company_car(client)
    _rate(client, vehicle, "2026-01-01T00:00:00", 0.34)
    from conftest import login

    login(client, "tester", "geheim1234")
    other = next(u for u in client.get("/api/auth/users").json() if u["username"] == "fahrer")
    assert client.delete(f"/api/auth/users/{other['id']}").status_code == 204


def test_page_is_served_with_relative_paths(client):
    register(client)
    html = client.get("/reimbursement").text
    assert 'href="vehicle"' in html
    assert "fetch('/api" not in html and 'fetch("/api' not in html


def test_monthly_mail_once_with_csv_attachment(client, db_session, monkeypatch):
    from datetime import date, timedelta

    from app import mailer, models, notifications

    register(client, email="d@example.org")
    vehicle = _company_car(client)
    home = _provider(client, "Wallbox")
    _rate(client, vehicle, "2020-01-01T00:00:00", 0.34)
    last_month = date.today().replace(day=1) - timedelta(days=1)
    _session(client, vehicle, home, f"{last_month.isoformat()}T20:00:00", 10.0)

    sent = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: sent.append(kw) or True)

    # Ohne Schalter: nichts.
    notifications._run_reimbursement_reports(db_session)
    assert sent == []

    client.patch(f"/api/vehicles/{vehicle['id']}", json={"reimbursement_monthly_mail": True})
    db_session.expire_all()
    notifications._run_reimbursement_reports(db_session)
    notifications._run_reimbursement_reports(db_session)
    assert len(sent) == 1
    filename, content, mime = sent[0]["attachments"][0]
    assert filename == f"lademonitor-erstattung-{last_month.strftime('%Y-%m')}.csv"
    assert mime == "text/csv"
    assert "3,40" in content.decode("utf-8-sig")
    assert db_session.get(models.Vehicle, vehicle["id"]).reimbursement_last_mailed == last_month.strftime("%Y-%m")


def test_mailer_attaches_files(client, monkeypatch, db_session):
    from app import mailer

    config = mailer.get_config(db_session)
    config.enabled, config.host, config.from_address = True, "smtp.example.org", "lm@example.org"
    db_session.commit()
    delivered = []
    monkeypatch.setattr(mailer, "_deliver", lambda cfg, message: delivered.append(message))
    ok = mailer.send(
        db_session, to_address="d@example.org", mail=mailer.Mail(subject="S", heading="H"),
        kind="test", attachments=[("bericht.csv", b"a;b\r\n", "text/csv")],
    )
    assert ok
    message = delivered[0]
    assert message.get_content_type() == "multipart/mixed"
    parts = [p for p in message.iter_attachments()]
    assert parts[0].get_filename() == "bericht.csv"
    assert parts[0].get_content() == "a;b\r\n" or parts[0].get_payload(decode=True) == b"a;b\r\n"
