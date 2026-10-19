"""Konfiguration, Anmeldung und Protokoll der BMW-CarData-Ladehistorie.

Die eigentliche Uebernahme laeuft im Hintergrund (`bmw_cardata_poller.py`,
Scheduler in `main.py`). Wie bei MyŠkoda vorerst nur ueber die Web-UI
(Einstellungen -> API & Debug), die Endpunkte sind aber regulaerer Teil der
REST-API.

Anmeldung in zwei Schritten (Device Code Flow): `POST .../auth/start` holt bei
BMW einen Code und gibt Adresse und Code zum Bestaetigen zurueck,
`POST .../auth/poll` versucht danach im angegebenen Intervall, ihn
einzuloesen. Client-ID und Tokens werden nie zurueckgegeben.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..bmw_cardata import CarDataError
from ..bmw_cardata_poller import (
    disconnect,
    finish_authorization,
    is_connected,
    poll_vehicle,
    requests_left,
    start_authorization,
)
from ..database import get_db
from ..auto_enable import should_enable
from ..log_export import zip_response

router = APIRouter(prefix="/api/bmw-cardata", tags=["bmw-cardata"])

VIN_LENGTH = 17


def _get_owned_vehicle(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return vehicle


def _find_config(db: Session, user: models.User, vehicle_id: str) -> models.BmwCarDataConfig | None:
    return (
        db.query(models.BmwCarDataConfig)
        .filter(
            models.BmwCarDataConfig.vehicle_id == vehicle_id,
            models.BmwCarDataConfig.user_id == user.id,
        )
        .first()
    )


def _get_or_create_config(
    db: Session, user: models.User, vehicle_id: str
) -> models.BmwCarDataConfig:
    _get_owned_vehicle(db, user, vehicle_id)
    config = _find_config(db, user, vehicle_id)
    if not config:
        config = models.BmwCarDataConfig(user_id=user.id, vehicle_id=vehicle_id)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _to_out(db: Session, config: models.BmwCarDataConfig) -> schemas.BmwCarDataConfigOut:
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    pending = bool(config.pending_device_code)
    used = config.requests_today if config.requests_day == datetime.utcnow().strftime("%Y-%m-%d") else 0
    return schemas.BmwCarDataConfigOut(
        vehicle_id=config.vehicle_id,
        vehicle_name=vehicle.name if vehicle else "?",
        enabled=config.enabled,
        has_client_id=bool(config.client_id),
        vin=config.vin,
        poll_interval_hours=config.poll_interval_hours,
        initial_history_days=config.initial_history_days,
        connected=is_connected(config),
        refresh_expires_at=config.refresh_expires_at,
        pending_user_code=config.pending_user_code if pending else None,
        pending_verification_uri=config.pending_verification_uri if pending else None,
        pending_expires_at=config.pending_expires_at if pending else None,
        pending_interval=config.pending_interval if pending else None,
        history_cursor=config.history_cursor,
        last_poll_at=config.last_poll_at,
        next_poll_at=config.next_poll_at,
        last_status=config.last_status,
        last_error=config.last_error,
        last_imported_count=config.last_imported_count,
        requests_today=used or 0,
        requests_left=requests_left(config),
    )


@router.get("/configs", response_model=list[schemas.BmwCarDataConfigOut])
def list_configs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    configs = (
        db.query(models.BmwCarDataConfig).filter(models.BmwCarDataConfig.user_id == user.id).all()
    )
    return [_to_out(db, c) for c in configs]


@router.get("/configs/{vehicle_id}", response_model=schemas.BmwCarDataConfigOut)
def get_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return _to_out(db, _get_or_create_config(db, user, vehicle_id))


def _is_complete(config: models.BmwCarDataConfig) -> bool:
    return bool(config.client_id and config.vin) and is_connected(config)


@router.put("/configs/{vehicle_id}", response_model=schemas.BmwCarDataConfigOut)
def update_config(
    vehicle_id: str,
    payload: schemas.BmwCarDataConfigIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    was_enabled = config.enabled
    complete_before = _is_complete(config)

    if payload.client_id and payload.client_id != config.client_id:
        # Eine andere Client-ID ist ein anderer Zugang - die Tokens der alten
        # gelten dafuer nicht.
        config.client_id = payload.client_id
        disconnect(config)
    if payload.vin is not None:
        vin = payload.vin.upper()
        if len(vin) != VIN_LENGTH:
            raise HTTPException(
                422, f"Die FIN muss {VIN_LENGTH} Zeichen lang sein (eingetragen: {len(vin)})"
            )
        config.vin = vin

    # Zugangsdaten sind hier die Verbindung mit BMW (auth_poll); das
    # Speichern schaltet nur ein, wenn erst die FIN die Konfiguration vervollstaendigt.
    enabled = payload.enabled or should_enable(
        db, config, complete_before=complete_before,
        complete_now=_is_complete(config), credentials_entered=False,
    )
    if enabled:
        if not config.client_id:
            raise HTTPException(422, "Ohne Client-ID kann der Abruf nicht aktiviert werden")
        if not is_connected(config):
            raise HTTPException(422, "Bitte zuerst mit BMW verbinden")
        if not config.vin:
            raise HTTPException(422, "Ohne FIN kann der Abruf nicht aktiviert werden")

    config.enabled = enabled
    if payload.poll_interval_hours != config.poll_interval_hours and config.next_poll_at:
        config.next_poll_at = None
    config.poll_interval_hours = payload.poll_interval_hours
    config.initial_history_days = payload.initial_history_days
    if config.enabled and not was_enabled:
        config.next_poll_at = None

    db.commit()
    db.refresh(config)
    return _to_out(db, config)


@router.delete("/configs/{vehicle_id}", status_code=204)
def delete_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Entfernt Konfiguration, Anmeldung und Protokoll. Bereits angelegte
    Ladevorgaenge bleiben. Den Zugang selbst widerruft man bei BMW, indem man
    die Client-ID im MyBMW-Portal loescht."""
    config = _find_config(db, user, vehicle_id)
    if not config:
        raise HTTPException(404, "Keine CarData-Konfiguration fuer dieses Fahrzeug")
    db.query(models.BmwCarDataLogEntry).filter(
        models.BmwCarDataLogEntry.vehicle_id == vehicle_id,
        models.BmwCarDataLogEntry.user_id == user.id,
    ).delete(synchronize_session=False)
    db.delete(config)
    db.commit()


# --------------------------------------------------------------------------
# Anmeldung
# --------------------------------------------------------------------------

@router.post("/configs/{vehicle_id}/auth/start", response_model=schemas.BmwCarDataConfigOut)
def auth_start(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    if not config.client_id:
        raise HTTPException(422, "Bitte zuerst die Client-ID eintragen und speichern")
    try:
        start_authorization(db, config)
    except CarDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    db.refresh(config)
    return _to_out(db, config)


@router.post("/configs/{vehicle_id}/auth/poll", response_model=schemas.BmwCarDataAuthResult)
def auth_poll(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    was_connected = is_connected(config)
    try:
        status = finish_authorization(db, config)
    except CarDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    db.refresh(config)
    # Frisch verbunden = Zugangsdaten stehen: Abruf einschalten, sofern die
    # FIN bekannt ist (BMW liefert sie bei genau einer Zuordnung selbst).
    if status == "connected" and not was_connected and should_enable(
        db, config, complete_before=False,
        complete_now=_is_complete(config), credentials_entered=True,
    ):
        config.enabled = True
        config.next_poll_at = None
        db.commit()
        db.refresh(config)
    return schemas.BmwCarDataAuthResult(status=status, config=_to_out(db, config))


@router.post("/configs/{vehicle_id}/disconnect", response_model=schemas.BmwCarDataConfigOut)
def auth_disconnect(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    disconnect(config)
    config.enabled = False
    config.last_status = None
    config.last_error = None
    db.commit()
    db.refresh(config)
    return _to_out(db, config)


@router.post("/configs/{vehicle_id}/poll", response_model=schemas.BmwCarDataConfigOut)
def poll_now(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Ladehistorie sofort abrufen - auch bei ausgeschaltetem Abruf. Kostet
    mindestens eine Anfrage aus dem Tageskontingent."""
    config = _get_or_create_config(db, user, vehicle_id)
    if not is_connected(config):
        raise HTTPException(422, "Bitte zuerst mit BMW verbinden")
    poll_vehicle(db, config)
    db.refresh(config)
    return _to_out(db, config)


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------

@router.get("/logs", response_model=list[schemas.BmwCarDataLogOut])
def list_logs(
    vehicle_id: str | None = None,
    limit: int = Query(default=100, le=300),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.BmwCarDataLogEntry).filter(models.BmwCarDataLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.BmwCarDataLogEntry.vehicle_id == vehicle_id)
    entries = q.order_by(models.BmwCarDataLogEntry.created_at.desc()).limit(limit).all()
    return [
        schemas.BmwCarDataLogOut(
            id=e.id,
            created_at=e.created_at,
            level=e.level,
            event=e.event,
            message=e.message,
            has_payload=bool(e.payload),
        )
        for e in entries
    ]


@router.get("/logs/export.zip")
def export_logs_zip(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Alle Protokolleintraege samt Rohdaten als ZIP (siehe log_export.py)."""
    q = db.query(models.BmwCarDataLogEntry).filter(models.BmwCarDataLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.BmwCarDataLogEntry.vehicle_id == vehicle_id)
    return zip_response(q.order_by(models.BmwCarDataLogEntry.created_at.asc()).all(), "bmw-cardata")


@router.get("/logs/{entry_id}/payload")
def get_log_payload(
    entry_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    entry = (
        db.query(models.BmwCarDataLogEntry)
        .filter(
            models.BmwCarDataLogEntry.id == entry_id,
            models.BmwCarDataLogEntry.user_id == user.id,
        )
        .first()
    )
    if not entry or not entry.payload:
        raise HTTPException(404, "Keine Rohdaten zu diesem Eintrag")
    return Response(content=entry.payload, media_type="application/json")


@router.delete("/logs", status_code=204)
def clear_logs(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.BmwCarDataLogEntry).filter(models.BmwCarDataLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.BmwCarDataLogEntry.vehicle_id == vehicle_id)
    q.delete(synchronize_session=False)
    db.commit()
