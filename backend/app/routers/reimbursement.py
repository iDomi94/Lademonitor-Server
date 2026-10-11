"""Erstattungsbericht fuer den Dienstwagen (siehe reimbursement.py).

`/api/reimbursement/rates` ist die flache Liste der Saetze, die die Apps
spiegeln (wie /api/vehicle-costs); `/api/reimbursement/report` liefert den
Bericht als JSON oder CSV.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..i18n import language_context
from ..reimbursement import build_report, to_csv
from ..sync import record_deletion

router = APIRouter(prefix="/api/reimbursement", tags=["reimbursement"])


def _day(value: datetime) -> datetime:
    """Nur das Datum zaehlt (siehe routers/provider_fees.py)."""
    return datetime(value.year, value.month, value.day)


def _own_vehicle(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return vehicle


def _get_rate(db: Session, user: models.User, rate_id: str) -> models.ReimbursementRate:
    rate = (
        db.query(models.ReimbursementRate)
        .filter(models.ReimbursementRate.id == rate_id, models.ReimbursementRate.user_id == user.id)
        .first()
    )
    if not rate:
        raise HTTPException(404, "Erstattungssatz nicht gefunden")
    return rate


@router.get("/rates", response_model=list[schemas.ReimbursementRateOut])
def list_rates(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ReimbursementRate).filter(models.ReimbursementRate.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.ReimbursementRate.vehicle_id == vehicle_id)
    return q.order_by(models.ReimbursementRate.valid_from.desc()).all()


@router.post("/rates", response_model=schemas.ReimbursementRateOut, status_code=201)
def create_rate(
    payload: schemas.ReimbursementRateCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _own_vehicle(db, user, payload.vehicle_id)
    data = payload.model_dump()
    data["valid_from"] = _day(data["valid_from"])
    rate = models.ReimbursementRate(**data, user_id=user.id)
    db.add(rate)
    db.commit()
    db.refresh(rate)
    return rate


@router.patch("/rates/{rate_id}", response_model=schemas.ReimbursementRateOut)
def update_rate(
    rate_id: str,
    payload: schemas.ReimbursementRateUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    rate = _get_rate(db, user, rate_id)
    # Alle Felder sind Pflicht - null heisst unveraendert.
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if "vehicle_id" in fields:
        _own_vehicle(db, user, fields["vehicle_id"])
    if "valid_from" in fields:
        fields["valid_from"] = _day(fields["valid_from"])
    for field, value in fields.items():
        setattr(rate, field, value)
    db.commit()
    db.refresh(rate)
    return rate


@router.delete("/rates/{rate_id}", status_code=204)
def delete_rate(
    rate_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    rate = _get_rate(db, user, rate_id)
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.REIMBURSEMENT_RATE, rate.id)
    db.delete(rate)
    db.commit()


def default_period(today: date) -> tuple[date, date]:
    """Ohne Angabe: der laufende Monat bis heute."""
    return today.replace(day=1), today


def previous_month(today: date) -> tuple[date, date]:
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


@router.get("/report", response_model=schemas.ReimbursementReportOut)
def report(
    vehicle_id: str,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    format: Literal["json", "csv"] = "json",
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _own_vehicle(db, user, vehicle_id)
    start, end = default_period(date.today())
    date_from = date_from or start
    date_to = date_to or end
    if date_to < date_from:
        raise HTTPException(422, "Das Enddatum liegt vor dem Startdatum")
    result = build_report(db, user, vehicle, date_from, date_to)
    if format == "json":
        return result
    with language_context(user.language):
        body = to_csv(result, user.language or "de")
    filename = f"lademonitor-erstattung-{date_from.isoformat()}_{date_to.isoformat()}.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
