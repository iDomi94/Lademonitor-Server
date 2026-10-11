"""Vergleichsfahrzeug fuer die Ersparnis im Rueckblick (siehe review.py).

Global je Nutzer, gespeichert als Spalten am `User` - Web, Mail und Apps
rechnen damit dieselbe Ersparnis. Solange nichts gespeichert ist, liefert GET
die Standardwerte mit `configured=false`; die Apps erkennen daran, dass sie
ihre lokalen Werte nicht ueberschreiben muessen.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..review import combustion_vehicle

router = APIRouter(prefix="/api/combustion-vehicle", tags=["review"])


@router.get("", response_model=schemas.CombustionVehicleOut)
def get_combustion_vehicle(user: models.User = Depends(get_current_user)):
    return combustion_vehicle(user)


@router.put("", response_model=schemas.CombustionVehicleOut)
def put_combustion_vehicle(
    payload: schemas.CombustionVehicleIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Immer alle vier Felder zusammen - ein halb gespeichertes Fahrzeug
    # (Diesel mit dem Benzinpreis der Vorgabe) waere schwer zu erkennen.
    user.combustion_fuel_type = payload.fuel_type
    user.combustion_consumption_l_per_100km = payload.consumption_l_per_100km
    user.combustion_fuel_price_per_l = payload.fuel_price_per_l
    user.combustion_grid_co2_kg_per_kwh = payload.grid_co2_kg_per_kwh
    user.combustion_updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return combustion_vehicle(user)
