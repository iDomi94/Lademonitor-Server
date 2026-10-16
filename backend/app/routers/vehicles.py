from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..sync import record_deletion
from ..vehicle_costs import ownership_info

# Datumsfelder der Kauf-/Leasingangaben: nur das Datum zaehlt (wie bei den
# Grundgebuehren), eine mitgeschickte Uhrzeit wird verworfen.
_DATE_FIELDS = ("purchase_date", "first_registration", "leasing_start", "leasing_end")
# Felder mit festem Wert: ein mitgeschicktes null laesst sie stehen (aeltere
# Apps kennen die Dienstwagen-Felder nicht und schicken sie gar nicht erst).
_KEEP_ON_NULL = (
    "ownership", "is_company_car", "reimbursement_mode",
    "reimbursement_include_fees", "reimbursement_monthly_mail",
)


def _day(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def vehicle_out(db: Session, vehicle: models.Vehicle) -> schemas.VehicleOut:
    out = schemas.VehicleOut.model_validate(vehicle)
    sessions = []
    if vehicle.ownership == "purchase":
        sessions = (
            db.query(models.ChargingSession)
            .filter(
                models.ChargingSession.vehicle_id == vehicle.id,
                models.ChargingSession.odometer_km.isnot(None),
            )
            .all()
        )
    for key, value in ownership_info(vehicle, sessions, date.today()).items():
        setattr(out, key, value)
    return out

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])


@router.get("", response_model=list[schemas.VehicleOut])
def list_vehicles(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    vehicles = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.user_id == user.id)
        .order_by(models.Vehicle.created_at)
        .all()
    )
    return [vehicle_out(db, v) for v in vehicles]


@router.post("", response_model=schemas.VehicleOut, status_code=201)
def create_vehicle(
    payload: schemas.VehicleCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    existing = (
        db.query(models.Vehicle)
        .filter(
            models.Vehicle.external_id == payload.external_id,
            models.Vehicle.user_id == user.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "Vehicle mit dieser external_id existiert bereits")
    data = payload.model_dump()
    for key in _DATE_FIELDS:
        data[key] = _day(data[key])
    vehicle = models.Vehicle(**data, user_id=user.id)
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle_out(db, vehicle)


@router.get("/{vehicle_id}", response_model=schemas.VehicleOut)
def get_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
    return vehicle_out(db, vehicle)


@router.patch("/{vehicle_id}", response_model=schemas.VehicleOut)
def update_vehicle(
    vehicle_id: str,
    payload: schemas.VehicleUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        # Kauf/Leasing hat immer einen Wert - ein mitgeschicktes null laesst
        # ihn stehen. Die uebrigen Kauf-/Leasingfelder lassen sich dagegen
        # bewusst per null leeren (z. B. den ueberschriebenen Wertverlust).
        if field in _KEEP_ON_NULL and value is None:
            continue
        if field in _DATE_FIELDS:
            value = _day(value)
        setattr(vehicle, field, value)
    db.commit()
    db.refresh(vehicle)
    return vehicle_out(db, vehicle)


@router.delete("/{vehicle_id}", status_code=204)
def delete_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
    # Fahrzeugkosten und Erinnerungen gehen per Kaskade mit - je ein eigener
    # Grabstein, sonst behielten die Apps sie als Waisen.
    for cost in vehicle.costs:
        record_deletion(db, user.id, models.SyncEntityType.VEHICLE_COST, cost.id)
    for reminder in vehicle.reminders:
        record_deletion(db, user.id, models.SyncEntityType.REMINDER, reminder.id)
    for rate in vehicle.reimbursement_rates:
        record_deletion(db, user.id, models.SyncEntityType.REIMBURSEMENT_RATE, rate.id)
    # CarData-Anbindung haengt per Fremdschluessel am Fahrzeug, ist aber
    # reiner Serverzustand (kein Sync, kein Grabstein).
    db.query(models.BmwCarDataLogEntry).filter(
        models.BmwCarDataLogEntry.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    db.query(models.BmwCarDataConfig).filter(
        models.BmwCarDataConfig.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    # Ebenso die Anbindung ans VW-Datenportal.
    db.query(models.VwDataActLogEntry).filter(
        models.VwDataActLogEntry.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    db.query(models.VwDataActConfig).filter(
        models.VwDataActConfig.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    # Ebenso die Anbindung an Renault.
    db.query(models.RenaultLogEntry).filter(
        models.RenaultLogEntry.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    db.query(models.RenaultConfig).filter(
        models.RenaultConfig.vehicle_id == vehicle.id
    ).delete(synchronize_session=False)
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.VEHICLE, vehicle.id)
    db.delete(vehicle)
    db.commit()


def _get_owned(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Vehicle nicht gefunden")
    return vehicle
