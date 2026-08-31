"""Konfiguration und Debug-Protokoll der automatischen Ladeerkennung ueber die
MyŠkoda Public API.

Reine Konfigurations-/Diagnoseschnittstelle - die eigentliche Erkennung laeuft
im Hintergrund (`myskoda_poller.py`, Scheduler in `main.py`). Vorerst nur
ueber die Web-UI gedacht; die Endpunkte sind trotzdem regulaerer Teil der
REST-API (`/docs`), damit die iOS-App das spaeter ohne Serveraenderung
uebernehmen kann.

Alle Endpunkte arbeiten ausschliesslich auf den Fahrzeugen des angemeldeten
Nutzers (Pro-Nutzer-Datentrennung wie im Rest der App). Der API-Key wird nie
zurueckgegeben - nur, ob einer hinterlegt ist.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..myskoda_poller import poll_vehicle, test_connection

router = APIRouter(prefix="/api/myskoda", tags=["myskoda"])

#: Laenge einer Fahrzeug-Identifizierungsnummer. Reiner Tippfehler-Schutz beim
#: Aktivieren - ein falscher Wert kostet sonst eine Anfrage aus dem
#: Stundenkontingent und liefert nur ein 404.
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


def _get_or_create_config(
    db: Session, user: models.User, vehicle_id: str
) -> models.MySkodaConfig:
    _get_owned_vehicle(db, user, vehicle_id)
    config = (
        db.query(models.MySkodaConfig)
        .filter(
            models.MySkodaConfig.vehicle_id == vehicle_id,
            models.MySkodaConfig.user_id == user.id,
        )
        .first()
    )
    if not config:
        config = models.MySkodaConfig(user_id=user.id, vehicle_id=vehicle_id)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _to_out(db: Session, config: models.MySkodaConfig) -> schemas.MySkodaConfigOut:
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    return schemas.MySkodaConfigOut(
        vehicle_id=config.vehicle_id,
        vehicle_name=vehicle.name if vehicle else "?",
        enabled=config.enabled,
        has_api_key=bool(config.api_key),
        vin=config.vin,
        poll_interval_idle_minutes=config.poll_interval_idle_minutes,
        poll_interval_active_minutes=config.poll_interval_active_minutes,
        detect_missed_sessions=config.detect_missed_sessions,
        missed_session_min_soc_delta=config.missed_session_min_soc_delta,
        log_enabled=config.log_enabled,
        log_raw_payload=config.log_raw_payload,
        last_poll_at=config.last_poll_at,
        next_poll_at=config.next_poll_at,
        last_status=config.last_status,
        last_error=config.last_error,
        last_charging_state=config.last_charging_state,
        last_soc=config.last_soc,
        last_captured_at=config.last_captured_at,
        api_key_expires_at=config.api_key_expires_at,
        rate_limit_limit=config.rate_limit_limit,
        rate_limit_remaining=config.rate_limit_remaining,
        rate_limit_resets_at=config.rate_limit_resets_at,
        open_start_time=config.open_start_time,
        open_soc_start=config.open_soc_start,
        open_soc_last=config.open_soc_last,
        open_charging_type=config.open_charging_type,
        open_max_power_kw=config.open_max_power_kw,
        open_poll_count=config.open_poll_count or 0,
    )


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

@router.get("/configs", response_model=list[schemas.MySkodaConfigOut])
def list_configs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    configs = (
        db.query(models.MySkodaConfig).filter(models.MySkodaConfig.user_id == user.id).all()
    )
    return [_to_out(db, c) for c in configs]


@router.get("/configs/{vehicle_id}", response_model=schemas.MySkodaConfigOut)
def get_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return _to_out(db, _get_or_create_config(db, user, vehicle_id))


@router.put("/configs/{vehicle_id}", response_model=schemas.MySkodaConfigOut)
def update_config(
    vehicle_id: str,
    payload: schemas.MySkodaConfigIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    was_enabled = config.enabled

    if payload.api_key:
        config.api_key = payload.api_key
    if payload.vin is not None:
        config.vin = payload.vin.upper()

    if payload.enabled:
        if not config.api_key:
            raise HTTPException(422, "Ohne API-Key kann die Abfrage nicht aktiviert werden")
        if not config.vin:
            raise HTTPException(422, "Ohne FIN kann die Abfrage nicht aktiviert werden")
        if len(config.vin) != VIN_LENGTH:
            raise HTTPException(
                422, f"Die FIN muss {VIN_LENGTH} Zeichen lang sein (eingetragen: {len(config.vin)})"
            )

    config.enabled = payload.enabled
    config.poll_interval_idle_minutes = payload.poll_interval_idle_minutes
    config.poll_interval_active_minutes = payload.poll_interval_active_minutes
    config.detect_missed_sessions = payload.detect_missed_sessions
    config.missed_session_min_soc_delta = payload.missed_session_min_soc_delta
    config.log_enabled = payload.log_enabled
    config.log_raw_payload = payload.log_raw_payload

    # Frisch aktiviert: sofort abfragen lassen, statt bis zum naechsten
    # regulaeren Intervall zu warten - sonst wirkt das Aktivieren wie ein No-Op.
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
    """Entfernt Konfiguration inkl. API-Key und Protokoll. Bereits angelegte
    Ladevorgaenge bleiben unangetastet."""
    config = (
        db.query(models.MySkodaConfig)
        .filter(
            models.MySkodaConfig.vehicle_id == vehicle_id,
            models.MySkodaConfig.user_id == user.id,
        )
        .first()
    )
    if not config:
        raise HTTPException(404, "Keine MyŠkoda-Konfiguration fuer dieses Fahrzeug")
    db.query(models.MySkodaLogEntry).filter(
        models.MySkodaLogEntry.vehicle_id == vehicle_id,
        models.MySkodaLogEntry.user_id == user.id,
    ).delete(synchronize_session=False)
    db.delete(config)
    db.commit()


def _require_configured(config: models.MySkodaConfig) -> None:
    if not config.api_key or not config.vin:
        raise HTTPException(422, "Bitte zuerst API-Key und FIN eintragen und speichern")


@router.post("/configs/{vehicle_id}/test", response_model=schemas.MySkodaTestResult)
def test_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Einmaliger Abruf ohne Auswertung - legt bewusst keinen Ladevorgang an.

    Kostet eine Anfrage aus dem Stundenkontingent des API-Keys.
    """
    config = _get_or_create_config(db, user, vehicle_id)
    _require_configured(config)
    result = test_connection(db, config)
    db.refresh(config)
    return schemas.MySkodaTestResult(
        ok=result["ok"],
        error=result["error"],
        summary=result["summary"],
        config=_to_out(db, config),
    )


@router.post("/configs/{vehicle_id}/poll", response_model=schemas.MySkodaConfigOut)
def poll_now(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Vollwertige Abfrage inkl. Auswertung - kann also einen Ladevorgang
    beginnen oder abschliessen. Funktioniert auch bei deaktivierter
    Automatik, damit sich die Erkennung gezielt testen laesst."""
    config = _get_or_create_config(db, user, vehicle_id)
    _require_configured(config)
    poll_vehicle(db, config)
    db.refresh(config)
    return _to_out(db, config)


# --------------------------------------------------------------------------
# Debug-Protokoll
# --------------------------------------------------------------------------

@router.get("/logs", response_model=list[schemas.MySkodaLogOut])
def list_logs(
    vehicle_id: str | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.MySkodaLogEntry).filter(models.MySkodaLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.MySkodaLogEntry.vehicle_id == vehicle_id)
    entries = q.order_by(models.MySkodaLogEntry.created_at.desc()).limit(limit).all()
    return [
        schemas.MySkodaLogOut(
            id=e.id,
            created_at=e.created_at,
            level=e.level,
            event=e.event,
            message=e.message,
            charging_state=e.charging_state,
            soc_percent=e.soc_percent,
            charge_power_kw=e.charge_power_kw,
            captured_at=e.captured_at,
            has_payload=bool(e.payload),
        )
        for e in entries
    ]


@router.get("/logs/export")
def export_logs(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Komplettes Protokoll inkl. Rohantworten als JSON-Datei.

    Gedacht zum Weiterreichen (z.B. an ein Issue), wenn ein echter
    Ladevorgang nicht wie erwartet erkannt wurde. Enthaelt bewusst KEINEN
    API-Key, wohl aber FIN und GPS-Koordinaten aus den Rohantworten.
    """
    q = db.query(models.MySkodaLogEntry).filter(models.MySkodaLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.MySkodaLogEntry.vehicle_id == vehicle_id)
    entries = q.order_by(models.MySkodaLogEntry.created_at.asc()).all()

    export = [
        {
            "created_at": e.created_at.isoformat(),
            "vehicle_id": e.vehicle_id,
            "level": e.level,
            "event": e.event,
            "message": e.message,
            "charging_state": e.charging_state,
            "soc_percent": e.soc_percent,
            "charge_power_kw": e.charge_power_kw,
            "captured_at": e.captured_at.isoformat() if e.captured_at else None,
            "payload": json.loads(e.payload) if e.payload else None,
        }
        for e in entries
    ]
    filename = f"myskoda-log-{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')}.json"
    return Response(
        content=json.dumps(export, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/logs/{entry_id}/payload")
def get_log_payload(
    entry_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Rohantwort eines einzelnen Protokolleintrags - separat abrufbar, damit
    die Protokolltabelle in der Web-UI leichtgewichtig bleibt."""
    entry = (
        db.query(models.MySkodaLogEntry)
        .filter(
            models.MySkodaLogEntry.id == entry_id,
            models.MySkodaLogEntry.user_id == user.id,
        )
        .first()
    )
    if not entry:
        raise HTTPException(404, "Protokolleintrag nicht gefunden")
    if not entry.payload:
        raise HTTPException(404, "Zu diesem Eintrag wurde keine Rohantwort gespeichert")
    return Response(content=entry.payload, media_type="application/json")


@router.delete("/logs", status_code=204)
def clear_logs(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.MySkodaLogEntry).filter(models.MySkodaLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.MySkodaLogEntry.vehicle_id == vehicle_id)
    q.delete(synchronize_session=False)
    db.commit()
