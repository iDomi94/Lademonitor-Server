from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])


@router.get("", response_model=list[schemas.VehicleOut])
def list_vehicles(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.Vehicle)
        .filter(models.Vehicle.user_id == user.id)
        .order_by(models.Vehicle.created_at)
        .all()
    )


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
    vehicle = models.Vehicle(**payload.model_dump(), user_id=user.id)
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


@router.get("/{vehicle_id}", response_model=schemas.VehicleOut)
def get_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
    return vehicle


@router.patch("/{vehicle_id}", response_model=schemas.VehicleOut)
def update_vehicle(
    vehicle_id: str,
    payload: schemas.VehicleUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(vehicle, field, value)
    db.commit()
    db.refresh(vehicle)
    return vehicle


@router.delete("/{vehicle_id}", status_code=204)
def delete_vehicle(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned(db, user, vehicle_id)
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
