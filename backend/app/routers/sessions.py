from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..consumption import compute_vehicle_consumptions
from ..database import get_db
from ..geocode import reverse_geocode
from .locations import match_location

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def attach_consumption(db: Session, user_id: str, sessions: list[models.ChargingSession]) -> None:
    """Reichert die uebergebenen (bereits geladenen) Sessions in-place mit
    consumption_kwh_per_100km/consumption_method an. Laedt dafuer pro
    betroffenem Fahrzeug die komplette Session-Historie, da die Berechnung
    Vorgaenger und Vollladungs-Intervalle ueber ALLE Sessions braucht - nicht
    nur die evtl. gefilterte/limitierte Ergebnismenge (siehe consumption.py).
    user_id-Filter ist hier defensiv (Vehicle-FK macht Fremdzugriff ueber
    vehicle_id strukturell ohnehin unmoeglich), aber macht die Grenze explizit."""
    vehicle_ids = {s.vehicle_id for s in sessions}
    for vehicle_id in vehicle_ids:
        vehicle = db.get(models.Vehicle, vehicle_id)
        full_history = (
            db.query(models.ChargingSession)
            .filter(
                models.ChargingSession.vehicle_id == vehicle_id,
                models.ChargingSession.user_id == user_id,
            )
            .order_by(models.ChargingSession.start_time)
            .all()
        )
        capacity = vehicle.battery_capacity_kwh if vehicle else None
        results = compute_vehicle_consumptions(full_history, capacity)
        for s in sessions:
            if s.vehicle_id != vehicle_id:
                continue
            result = results.get(s.id)
            s.consumption_kwh_per_100km = result.value if result else None
            s.consumption_method = result.method if result else None


def resolve_location(session: models.ChargingSession, user_id: str, db: Session) -> None:
    """Matcht einen bekannten Ladeort (nur unter den eigenen) per GPS, sonst
    Fallback auf Reverse-Geocoding."""
    if session.latitude is None or session.longitude is None:
        return
    matched = match_location(db, user_id, session.latitude, session.longitude)
    if matched:
        session.location_id = matched.id
    else:
        session.geocoded_place = reverse_geocode(session.latitude, session.longitude)


def estimate_energy_kwh(session: models.ChargingSession, vehicle: models.Vehicle) -> None:
    """Fuellt energy_kwh aus dem SoC-Delta, falls kein echter Wert vorliegt."""
    if session.energy_kwh is not None:
        return
    if (
        session.soc_start is not None
        and session.soc_end is not None
        and vehicle.battery_capacity_kwh
    ):
        delta_pct = max(session.soc_end - session.soc_start, 0)
        session.energy_kwh = round(vehicle.battery_capacity_kwh * delta_pct / 100, 2)
        session.energy_is_estimated = True


def apply_provider_price(session: models.ChargingSession, db: Session) -> None:
    """Schlaegt Anbieter+Preis anhand des gematchten Ladeorts vor, wenn noch nichts gesetzt ist."""
    if session.provider_id or not session.location_id:
        return
    location = db.get(models.ChargingLocation, session.location_id)
    if not location or not location.default_provider_id:
        return
    provider = db.get(models.Provider, location.default_provider_id)
    if not provider:
        return
    session.provider_id = provider.id
    price = (
        provider.last_price_dc_per_kwh
        if session.charging_type == models.ChargingType.DC
        else provider.last_price_ac_per_kwh
    )
    if price is not None:
        session.price_per_kwh = price
        if session.energy_kwh:
            session.price_total = round(price * session.energy_kwh, 2)


def sync_provider_last_price(session: models.ChargingSession, db: Session) -> None:
    """Merkt sich den zuletzt gezahlten Preis beim Anbieter (Preis-Gedaechtnis)."""
    if not session.provider_id or session.price_per_kwh is None:
        return
    provider = db.get(models.Provider, session.provider_id)
    if not provider:
        return
    if session.charging_type == models.ChargingType.DC:
        provider.last_price_dc_per_kwh = session.price_per_kwh
    else:
        provider.last_price_ac_per_kwh = session.price_per_kwh


def _get_owned_vehicle(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Vehicle nicht gefunden")
    return vehicle


@router.get("", response_model=list[schemas.SessionOut])
def list_sessions(
    vehicle_id: str | None = None,
    needs_review: bool | None = None,
    # Datumsfilter (inklusive) fuer Web-UI/App - siehe attach_consumption(): die
    # Verbrauchsberechnung selbst laedt unabhaengig davon immer die VOLLSTAENDIGE
    # Fahrzeug-Historie, ist also von diesem Filter nicht betroffen.
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ChargingSession).filter(models.ChargingSession.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.ChargingSession.vehicle_id == vehicle_id)
    if needs_review is not None:
        q = q.filter(models.ChargingSession.needs_review == needs_review)
    if start_date:
        q = q.filter(models.ChargingSession.start_time >= datetime.combine(start_date, time.min))
    if end_date:
        q = q.filter(models.ChargingSession.start_time <= datetime.combine(end_date, time.max))
    sessions = q.order_by(models.ChargingSession.start_time.desc()).limit(limit).all()
    attach_consumption(db, user.id, sessions)
    return sessions


@router.post("", response_model=schemas.SessionOut, status_code=201)
def create_session(
    payload: schemas.SessionCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    vehicle = _get_owned_vehicle(db, user, payload.vehicle_id)

    session = models.ChargingSession(
        **payload.model_dump(), source=models.SessionSource.MANUAL, user_id=user.id
    )
    if not session.location_id:
        resolve_location(session, user.id, db)
    estimate_energy_kwh(session, vehicle)
    if session.price_total and session.energy_kwh and not session.price_per_kwh:
        session.price_per_kwh = round(session.price_total / session.energy_kwh, 4)
    elif session.price_per_kwh and session.energy_kwh and not session.price_total:
        session.price_total = round(session.price_per_kwh * session.energy_kwh, 2)

    sync_provider_last_price(session, db)

    db.add(session)
    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session


@router.patch("/{session_id}", response_model=schemas.SessionOut)
def update_session(
    session_id: str,
    payload: schemas.SessionUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    session = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.id == session_id, models.ChargingSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Ladevorgang nicht gefunden")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(session, field, value)

    vehicle = db.get(models.Vehicle, session.vehicle_id)
    estimate_energy_kwh(session, vehicle)
    if session.price_total and session.energy_kwh and not session.price_per_kwh:
        session.price_per_kwh = round(session.price_total / session.energy_kwh, 4)
    elif session.price_per_kwh and session.energy_kwh:
        session.price_total = round(session.price_per_kwh * session.energy_kwh, 2)

    sync_provider_last_price(session, db)

    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    session = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.id == session_id, models.ChargingSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    db.delete(session)
    db.commit()


@router.post("/auto", response_model=schemas.SessionOut, status_code=201)
def push_auto_session(
    payload: schemas.AutoSessionPush,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Wird von der Home-Assistant-Automation nach Ladeende aufgerufen. Der
    Bearer-Token bestimmt den Nutzer - das Fahrzeug muss unter DESSEN Konto
    angelegt sein (typischerweise der Admin-Account, der die echten Fahrzeuge
    besitzt, nicht ein separates leeres HA-Konto - siehe CLAUDE.md)."""
    vehicle = (
        db.query(models.Vehicle)
        .filter(
            models.Vehicle.external_id == payload.vehicle_external_id,
            models.Vehicle.user_id == user.id,
        )
        .first()
    )
    if not vehicle:
        raise HTTPException(
            404,
            f"Kein Vehicle mit external_id '{payload.vehicle_external_id}' fuer diesen Nutzer",
        )

    # Duplikat-Schutz: gleiche external_session_id nicht zweimal anlegen
    existing = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.external_session_id == payload.external_session_id,
            models.ChargingSession.user_id == user.id,
        )
        .first()
    )
    if existing:
        attach_consumption(db, user.id, [existing])
        return existing

    session = models.ChargingSession(
        vehicle_id=vehicle.id,
        user_id=user.id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        charging_type=payload.charging_type,
        soc_start=payload.soc_start,
        soc_end=payload.soc_end,
        odometer_km=payload.odometer_km,
        latitude=payload.latitude,
        longitude=payload.longitude,
        energy_kwh=payload.energy_kwh,
        external_session_id=payload.external_session_id,
        source=models.SessionSource.AUTOMATIC,
        needs_review=True,
    )

    resolve_location(session, user.id, db)

    estimate_energy_kwh(session, vehicle)
    apply_provider_price(session, db)

    db.add(session)
    db.commit()
    db.refresh(session)
    attach_consumption(db, user.id, [session])
    return session
