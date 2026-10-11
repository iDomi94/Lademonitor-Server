"""Erinnerungen je Fahrzeug (siehe models.Reminder, reminders.py).

Flach unter /api/reminders wie die Fahrzeugkosten: die Apps spiegeln die
Tabelle ueber eine einfache Liste. Status und Prognose rechnet jede Antwort
frisch aus den Ladevorgaengen.
"""

from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..reminders import (
    COST_CATEGORY,
    STATUS_RANK,
    TIRE_COMPLETE_WINDOW_DAYS,
    TIRE_KIND_REMINDER,
    Mileage,
    evaluate,
    mileage,
    next_due,
)
from ..sync import record_deletion

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


def _day(value: datetime | None) -> datetime | None:
    """Nur das Datum zaehlt (siehe routers/provider_fees.py)."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def mileage_by_vehicle(db: Session, vehicle_ids: list[str], today: date) -> dict[str, Mileage]:
    sessions = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.vehicle_id.in_(vehicle_ids),
            models.ChargingSession.odometer_km.isnot(None),
        )
        .all()
    )
    grouped = defaultdict(list)
    for s in sessions:
        grouped[s.vehicle_id].append(s)
    return {vid: mileage(grouped.get(vid, []), today) for vid in vehicle_ids}


def reminder_out(reminder: models.Reminder, m: Mileage, today: date) -> schemas.ReminderOut:
    out = schemas.ReminderOut.model_validate(reminder)
    state = evaluate(reminder, m, today)
    out.status = state.status
    out.current_km = m.current_km
    out.km_remaining = state.km_remaining
    out.km_forecast_date = state.km_forecast_date
    out.days_remaining = state.days_remaining
    out.next_due_date = state.next_due_date
    out.cost_category = COST_CATEGORY.get(reminder.kind)
    return out


def list_with_state(
    db: Session, user: models.User, vehicle_id: str | None = None, today: date | None = None
) -> list[schemas.ReminderOut]:
    """Alle Erinnerungen mit Status, dringendste zuerst."""
    today = today or date.today()
    q = db.query(models.Reminder).filter(models.Reminder.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.Reminder.vehicle_id == vehicle_id)
    reminders = q.all()
    if not reminders:
        return []
    by_vehicle = mileage_by_vehicle(db, list({r.vehicle_id for r in reminders}), today)
    result = [reminder_out(r, by_vehicle[r.vehicle_id], today) for r in reminders]
    result.sort(key=lambda r: (
        -STATUS_RANK[r.status],
        r.next_due_date is None,
        r.next_due_date or date.max,
    ))
    return result


def _require_own_vehicle(db: Session, user: models.User, vehicle_id: str) -> None:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden")


def _get_owned(db: Session, user: models.User, reminder_id: str) -> models.Reminder:
    reminder = (
        db.query(models.Reminder)
        .filter(models.Reminder.id == reminder_id, models.Reminder.user_id == user.id)
        .first()
    )
    if not reminder:
        raise HTTPException(404, "Erinnerung nicht gefunden")
    return reminder


def _single_out(db: Session, reminder: models.Reminder) -> schemas.ReminderOut:
    today = date.today()
    m = mileage_by_vehicle(db, [reminder.vehicle_id], today)[reminder.vehicle_id]
    return reminder_out(reminder, m, today)


def complete(reminder: models.Reminder, done_day: date, done_km: int | None) -> None:
    """Erledigt: Merker setzen und die naechste Faelligkeit fortschreiben."""
    next_day, next_km = next_due(reminder, done_day, done_km)
    reminder.last_done_date = datetime(done_day.year, done_day.month, done_day.day)
    reminder.last_done_km = done_km
    reminder.due_date = (
        datetime(next_day.year, next_day.month, next_day.day) if next_day else None
    )
    reminder.due_km = next_km
    reminder.notified_stage = None


def complete_for_tire_change(db: Session, tire_set: models.TireSet) -> None:
    """Ein eingetragener Reifenwechsel hakt die passende Erinnerung ab
    (Winterreifen -> "Reifenwechsel Winter"), sofern er nicht weit VOR ihrer
    Faelligkeit liegt (siehe reminders.TIRE_COMPLETE_WINDOW_DAYS). Ohne
    Commit - laeuft im Commit des Reifenwechsels mit."""
    kind_value = getattr(tire_set.kind, "value", tire_set.kind)
    reminder_kind = TIRE_KIND_REMINDER.get(kind_value)
    if not reminder_kind:
        return
    installed = tire_set.installed_on.date() if isinstance(tire_set.installed_on, datetime) else tire_set.installed_on
    candidates = (
        db.query(models.Reminder)
        .filter(
            models.Reminder.user_id == tire_set.user_id,
            models.Reminder.vehicle_id == tire_set.vehicle_id,
            models.Reminder.kind == reminder_kind,
            models.Reminder.due_date.isnot(None),
        )
        .all()
    )
    for reminder in candidates:
        if (reminder.due_date.date() - installed).days <= TIRE_COMPLETE_WINDOW_DAYS:
            complete(reminder, installed, tire_set.odometer_km)


@router.get("", response_model=list[schemas.ReminderOut])
def list_reminders(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return list_with_state(db, user, vehicle_id)


@router.post("", response_model=schemas.ReminderOut, status_code=201)
def create_reminder(
    payload: schemas.ReminderCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_own_vehicle(db, user, payload.vehicle_id)
    data = payload.model_dump()
    for key in ("due_date", "last_done_date"):
        data[key] = _day(data[key])
    reminder = models.Reminder(**data, user_id=user.id)
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return _single_out(db, reminder)


@router.patch("/{reminder_id}", response_model=schemas.ReminderOut)
def update_reminder(
    reminder_id: str,
    payload: schemas.ReminderUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reminder = _get_owned(db, user, reminder_id)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("vehicle_id"):
        _require_own_vehicle(db, user, fields["vehicle_id"])
    # null = unveraendert bei Pflichtfeldern; leeren lassen sich alle
    # Faelligkeits-, Wiederholungs- und Freitextfelder.
    for key in ("vehicle_id", "kind", "lead_days", "lead_km"):
        if key in fields and fields[key] is None:
            fields.pop(key)
    for key in ("due_date", "last_done_date"):
        if key in fields:
            fields[key] = _day(fields[key])
    for field, value in fields.items():
        setattr(reminder, field, value)
    # Geaenderte Faelligkeit = neue Lage, die Mail-Stufen beginnen von vorn.
    reminder.notified_stage = None
    db.commit()
    db.refresh(reminder)
    return _single_out(db, reminder)


@router.post("/{reminder_id}/complete", response_model=schemas.ReminderOut)
def complete_reminder(
    reminder_id: str,
    payload: schemas.ReminderComplete,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reminder = _get_owned(db, user, reminder_id)
    today = date.today()
    done_day = payload.done_date.date() if payload.done_date else today
    done_km = payload.done_km
    if done_km is None:
        done_km = mileage_by_vehicle(db, [reminder.vehicle_id], today)[reminder.vehicle_id].current_km
    complete(reminder, done_day, done_km)
    db.commit()
    db.refresh(reminder)
    return _single_out(db, reminder)


@router.delete("/{reminder_id}", status_code=204)
def delete_reminder(
    reminder_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reminder = _get_owned(db, user, reminder_id)
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.REMINDER, reminder.id)
    db.delete(reminder)
    db.commit()
