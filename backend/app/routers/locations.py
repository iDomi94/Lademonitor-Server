import math

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/locations", tags=["locations"])


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung zwischen zwei Koordinaten in Metern."""
    r = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def match_location(db: Session, user_id: str, lat: float, lon: float) -> models.ChargingLocation | None:
    """Findet den naechstgelegenen bekannten Ladeort des Nutzers innerhalb seines Radius."""
    best = None
    best_dist = None
    locations = db.query(models.ChargingLocation).filter(models.ChargingLocation.user_id == user_id).all()
    for loc in locations:
        dist = haversine_m(lat, lon, loc.latitude, loc.longitude)
        if dist <= loc.radius_m and (best_dist is None or dist < best_dist):
            best, best_dist = loc, dist
    return best


@router.get("", response_model=list[schemas.LocationOut])
def list_locations(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.ChargingLocation)
        .filter(models.ChargingLocation.user_id == user.id)
        .order_by(models.ChargingLocation.name)
        .all()
    )


@router.post("", response_model=schemas.LocationOut, status_code=201)
def create_location(
    payload: schemas.LocationCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    location = models.ChargingLocation(**payload.model_dump(), user_id=user.id)
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


@router.patch("/{location_id}", response_model=schemas.LocationOut)
def update_location(
    location_id: str,
    payload: schemas.LocationUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    location = _get_owned(db, user, location_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(location, field, value)
    db.commit()
    db.refresh(location)
    return location


@router.delete("/{location_id}", status_code=204)
def delete_location(
    location_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    location = _get_owned(db, user, location_id)
    db.delete(location)
    db.commit()


def _get_owned(db: Session, user: models.User, location_id: str) -> models.ChargingLocation:
    location = (
        db.query(models.ChargingLocation)
        .filter(models.ChargingLocation.id == location_id, models.ChargingLocation.user_id == user.id)
        .first()
    )
    if not location:
        raise HTTPException(404, "Ladeort nicht gefunden")
    return location
