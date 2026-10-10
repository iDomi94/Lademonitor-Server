"""Erinnerungen (reminders.py, routers/reminders.py, Mail in notifications.py).

Wie bei den Fahrzeugkosten sind die Regeln Vertrag: beide Apps rechnen Status,
Prognose und "Erledigt" lokal nach (LocalReminderCalculator).
"""

import io
import zipfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from conftest import create_vehicle, register

from app import mailer, models, notifications
from app import reminders as rm

TODAY = date(2026, 10, 10)


def _reminder(**fields):
    base = dict(
        kind="custom", due_date=None, due_km=None, repeat_months=None, repeat_km=None,
        lead_days=30, lead_km=1000, last_done_date=None,
    )
    base.update(fields)
    for key in ("due_date", "last_done_date"):
        if isinstance(base[key], str):
            base[key] = datetime.fromisoformat(base[key])
    return SimpleNamespace(**base)


def _session(day, km):
    return SimpleNamespace(start_time=datetime.fromisoformat(day), odometer_km=km)


NO_KM = rm.Mileage(None, None)


# ---------- Status ----------


def test_status_by_date():
    assert rm.evaluate(_reminder(due_date="2026-12-01"), NO_KM, TODAY).status == "ok"
    assert rm.evaluate(_reminder(due_date="2026-11-09"), NO_KM, TODAY).status == "due_soon"
    # Der Faelligkeitstag selbst zaehlt noch als "bald faellig".
    assert rm.evaluate(_reminder(due_date="2026-10-10"), NO_KM, TODAY).status == "due_soon"
    state = rm.evaluate(_reminder(due_date="2026-10-09"), NO_KM, TODAY)
    assert state.status == "overdue" and state.days_remaining == -1


def test_status_by_km_and_whichever_comes_first():
    m = rm.Mileage(current_km=29500, km_per_day=40.0)
    assert rm.evaluate(_reminder(due_km=32000), m, TODAY).status == "ok"
    assert rm.evaluate(_reminder(due_km=30400), m, TODAY).status == "due_soon"
    assert rm.evaluate(_reminder(due_km=29500), m, TODAY).status == "overdue"
    # Datum weit weg, km schon erreicht: ueberfaellig.
    both = _reminder(due_date="2027-06-01", due_km=29000)
    assert rm.evaluate(both, m, TODAY).status == "overdue"


def test_km_forecast_and_next_due_date():
    m = rm.Mileage(current_km=29000, km_per_day=50.0)
    state = rm.evaluate(_reminder(due_km=30000, due_date="2027-01-01"), m, TODAY)
    assert state.km_remaining == 1000
    assert state.km_forecast_date == TODAY + timedelta(days=20)
    # Das Fruehere aus Datum und Prognose.
    assert state.next_due_date == TODAY + timedelta(days=20)


def test_without_due_it_is_ok_or_done():
    assert rm.evaluate(_reminder(), NO_KM, TODAY).status == "ok"
    done = _reminder(last_done_date="2026-09-01")
    assert rm.evaluate(done, NO_KM, TODAY).status == "done"


# ---------- Fahrleistung ----------


def test_mileage_uses_last_90_days_with_anchor_before():
    sessions = [
        _session("2026-01-01T08:00:00", 10000),
        _session("2026-06-01T08:00:00", 20000),
        _session("2026-09-10T08:00:00", 23000),
    ]
    m = rm.mileage(sessions, TODAY)
    assert m.current_km == 23000
    # Anker ist der Stand VOR dem 90-Tage-Fenster (01.06.), 101 Tage.
    assert abs(m.km_per_day - 3000 / 101) < 1e-9


def test_mileage_needs_14_days():
    sessions = [_session("2026-10-01T08:00:00", 1000), _session("2026-10-05T08:00:00", 1200)]
    m = rm.mileage(sessions, TODAY)
    assert m.current_km == 1200 and m.km_per_day is None
    assert rm.mileage([], TODAY) == rm.Mileage(None, None)


# ---------- Erledigt ----------


def test_complete_from_done_date_for_hu():
    r = _reminder(kind="hu", due_date="2026-10-01", repeat_months=24)
    assert rm.next_due(r, date(2026, 10, 20), None) == (date(2028, 10, 20), None)


def test_complete_keeps_calendar_day_for_tire_change():
    r = _reminder(kind="tire_winter", due_date="2026-10-15", repeat_months=12)
    assert rm.next_due(r, date(2026, 10, 20), None)[0] == date(2027, 10, 15)
    # Laenger liegengeblieben: trotzdem der naechste 15.10. NACH dem Erledigen.
    assert rm.next_due(r, date(2027, 11, 2), None)[0] == date(2028, 10, 15)


def test_complete_km_from_odometer():
    r = _reminder(kind="inspection", due_date="2026-10-01", repeat_months=12, repeat_km=30000)
    assert rm.next_due(r, date(2026, 10, 5), 31000) == (date(2027, 10, 5), 61000)
    # Ohne bekannten km-Stand keine km-Faelligkeit.
    assert rm.next_due(r, date(2026, 10, 5), None)[1] is None


def test_first_due_date_for_fixed_day():
    assert rm.first_due_date("tire_winter", date(2026, 10, 10)) == date(2026, 10, 15)
    assert rm.first_due_date("tire_summer", date(2026, 10, 10)) == date(2027, 4, 15)
    assert rm.first_due_date("hu", date(2026, 10, 10)) is None


# ---------- API ----------


def _create(client, vehicle_id, **fields):
    response = client.post("/api/reminders", json={"vehicle_id": vehicle_id, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def test_crud_status_and_ordering(client):
    register(client)
    vehicle = create_vehicle(client)
    today = date.today()
    later = _create(client, vehicle["id"], kind="hu", due_date=(today + timedelta(days=200)).isoformat())
    soon = _create(client, vehicle["id"], kind="tax", due_date=(today + timedelta(days=5)).isoformat())
    assert later["status"] == "ok" and soon["status"] == "due_soon"
    assert soon["cost_category"] == "tax"

    listed = client.get("/api/reminders").json()
    assert [r["id"] for r in listed] == [soon["id"], later["id"]]

    patched = client.patch(f"/api/reminders/{later['id']}", json={"title": "TÜV", "lead_days": None})
    assert patched.json()["title"] == "TÜV" and patched.json()["lead_days"] == 30

    assert client.delete(f"/api/reminders/{later['id']}").status_code == 204
    kinds = {(d["entity_type"], d["entity_id"]) for d in client.get("/api/sync/deletions").json()["deletions"]}
    assert ("reminder", later["id"]) in kinds


def test_km_comes_from_sessions_and_complete(client):
    register(client)
    vehicle = create_vehicle(client)
    today = datetime.combine(date.today(), datetime.min.time())
    for days_ago, km in ((60, 28000), (0, 29600)):
        client.post("/api/sessions", json={
            "vehicle_id": vehicle["id"], "energy_kwh": 30, "odometer_km": km,
            "start_time": (today - timedelta(days=days_ago)).isoformat(),
        })
    r = _create(client, vehicle["id"], kind="inspection", due_km=30000, repeat_months=12, repeat_km=30000)
    assert r["current_km"] == 29600 and r["km_remaining"] == 400
    assert r["status"] == "due_soon"
    assert r["km_forecast_date"] is not None

    done = client.post(f"/api/reminders/{r['id']}/complete", json={}).json()
    assert done["last_done_km"] == 29600
    assert done["due_km"] == 59600
    assert done["due_date"].startswith(date(today.year + 1, today.month, min(today.day, 28)).isoformat()[:7])
    assert done["status"] == "ok"


def test_tire_change_completes_matching_reminder(client):
    register(client)
    vehicle = create_vehicle(client)
    winter = _create(client, vehicle["id"], kind="tire_winter", due_date="2026-10-15", repeat_months=12)
    summer = _create(client, vehicle["id"], kind="tire_summer", due_date="2027-04-15", repeat_months=12)
    # Ein nachgetragener Wechsel von vor einem Jahr hakt nichts ab.
    client.post("/api/tires", json={"vehicle_id": vehicle["id"], "kind": "winter", "installed_on": "2025-10-20T00:00:00"})
    assert client.get("/api/reminders").json()[0]["last_done_date"] is None

    client.post("/api/tires", json={
        "vehicle_id": vehicle["id"], "kind": "winter", "installed_on": "2026-10-12T00:00:00", "odometer_km": 30000,
    })
    by_id = {r["id"]: r for r in client.get("/api/reminders").json()}
    assert by_id[winter["id"]]["due_date"].startswith("2027-10-15")
    assert by_id[winter["id"]]["last_done_km"] == 30000
    assert by_id[summer["id"]]["last_done_date"] is None


def test_vehicle_delete_records_reminder_tombstones(client):
    register(client)
    vehicle = create_vehicle(client)
    r = _create(client, vehicle["id"], kind="hu", due_date="2027-01-01")
    assert client.delete(f"/api/vehicles/{vehicle['id']}").status_code == 204
    kinds = {(d["entity_type"], d["entity_id"]) for d in client.get("/api/sync/deletions").json()["deletions"]}
    assert ("reminder", r["id"]) in kinds


def test_other_users_cannot_see_reminders(client):
    register(client, "alice")
    vehicle = create_vehicle(client)
    r = _create(client, vehicle["id"], kind="hu", due_date="2027-01-01")
    register(client, "bob")
    assert client.get("/api/reminders").json() == []
    assert client.patch(f"/api/reminders/{r['id']}", json={"title": "x"}).status_code == 404
    assert client.post("/api/reminders", json={"vehicle_id": vehicle["id"]}).status_code == 404


def test_backup_roundtrip(client):
    register(client, "alice")
    vehicle = create_vehicle(client)
    _create(client, vehicle["id"], kind="inspection", due_date="2027-01-01", due_km=60000, title="Große Inspektion")
    zip_bytes = client.get("/api/backup/export").content
    assert "reminders.csv" in zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()

    register(client, "bob")
    files = {"file": ("backup.zip", zip_bytes, "application/zip")}
    result = client.post("/api/backup/import", files=files).json()
    assert result["reminders_imported"] == 1
    imported = client.get("/api/reminders").json()
    assert imported[0]["title"] == "Große Inspektion" and imported[0]["due_km"] == 60000
    # Idempotent.
    again = client.post("/api/backup/import", files={"file": ("backup.zip", zip_bytes, "application/zip")}).json()
    assert again["reminders_imported"] == 0 and again["reminders_skipped"] == 1


def test_notify_setting_is_optional_for_old_clients(client):
    register(client)
    body = {
        "notify_backup_failed": True, "notify_myskoda_error": True, "notify_monthly_report": False,
        "notify_new_registration": True, "review_digest": "off",
    }
    client.put("/api/auth/notifications", json={**body, "notify_reminders": False})
    assert client.get("/api/auth/me").json()["notify_reminders"] is False
    # Ein aelterer Client ohne das Feld setzt es nicht zurueck.
    client.put("/api/auth/notifications", json=body)
    assert client.get("/api/auth/me").json()["notify_reminders"] is False


# ---------- Mail ----------


def test_mail_once_per_stage(client, db_session, monkeypatch):
    register(client, email="a@example.org")
    vehicle = create_vehicle(client)
    r = _create(client, vehicle["id"], kind="hu", due_date=(date.today() + timedelta(days=10)).isoformat())

    sent = []
    monkeypatch.setattr(mailer, "send", lambda db, **kw: sent.append(kw) or True)

    notifications._run_reminders(db_session)
    notifications._run_reminders(db_session)
    assert len(sent) == 1 and sent[0]["kind"] == "reminders"

    # Ueberfaellig ist eine neue Stufe.
    row = db_session.get(models.Reminder, r["id"])
    row.due_date = datetime.combine(date.today() - timedelta(days=1), datetime.min.time())
    db_session.commit()
    notifications._run_reminders(db_session)
    notifications._run_reminders(db_session)
    assert len(sent) == 2

    # Abgeschaltet: nichts.
    row.notified_stage = None
    db_session.commit()
    db_session.get(models.User, row.user_id).notify_reminders = False
    db_session.commit()
    notifications._run_reminders(db_session)
    assert len(sent) == 2
