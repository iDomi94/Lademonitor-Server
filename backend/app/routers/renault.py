"""Konfiguration, Anmeldung und Protokoll der Ladeerkennung ueber Renault.

Der Abruf laeuft im Hintergrund (`renault_poller.py`, Scheduler in
`main.py`). Wie bei den anderen Herstellerquellen vorerst nur ueber die Web-UI
(Einstellungen -> API & Debug), die Endpunkte sind aber regulaerer Teil der
REST-API. Passwort und Login-Token werden nie zurueckgegeben.

Anmeldung in zwei Schritten, weil Renault seit 2026 eine Bestaetigung per
E-Mail-Code verlangen kann: `POST .../login` meldet mit den gespeicherten
Zugangsdaten an und schickt bei Bedarf den Code los (`code_sent`),
`POST .../login/code` reicht ihn ein.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..auto_enable import should_enable
from ..log_export import zip_response
from ..renault import LOCALES
from ..renault_poller import (
    complete_login,
    forget_client,
    poll_vehicle,
    start_login,
    test_connection,
)

router = APIRouter(prefix="/api/renault", tags=["renault"])

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


def _find_config(db: Session, user: models.User, vehicle_id: str) -> models.RenaultConfig | None:
    return (
        db.query(models.RenaultConfig)
        .filter(
            models.RenaultConfig.vehicle_id == vehicle_id,
            models.RenaultConfig.user_id == user.id,
        )
        .first()
    )


def _get_or_create_config(db: Session, user: models.User, vehicle_id: str) -> models.RenaultConfig:
    _get_owned_vehicle(db, user, vehicle_id)
    config = _find_config(db, user, vehicle_id)
    if not config:
        config = models.RenaultConfig(user_id=user.id, vehicle_id=vehicle_id)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _to_out(db: Session, config: models.RenaultConfig) -> schemas.RenaultConfigOut:
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    return schemas.RenaultConfigOut(
        vehicle_id=config.vehicle_id,
        vehicle_name=vehicle.name if vehicle else "?",
        enabled=config.enabled,
        locale=config.locale,
        email=config.email,
        has_password=bool(config.password),
        vin=config.vin,
        connected=bool(config.login_token),
        vehicle_model=config.model_label,
        poll_interval_idle_minutes=config.poll_interval_idle_minutes,
        poll_interval_active_minutes=config.poll_interval_active_minutes,
        detect_missed_sessions=config.detect_missed_sessions,
        missed_session_min_soc_delta=config.missed_session_min_soc_delta,
        backdate_session_start=config.backdate_session_start,
        backdate_max_gap_minutes=config.backdate_max_gap_minutes,
        log_enabled=config.log_enabled,
        log_raw_payload=config.log_raw_payload,
        last_poll_at=config.last_poll_at,
        next_poll_at=config.next_poll_at,
        last_status=config.last_status,
        last_error=config.last_error,
        last_captured_at=config.last_captured_at,
        last_charging_state=config.last_charging_state,
        last_soc=config.last_soc,
        open_start_time=config.open_start_time,
        open_soc_start=config.open_soc_start,
        open_soc_last=config.open_soc_last,
        open_charging_type=config.open_charging_type,
    )



@router.get("/locales")
def list_locales() -> list[str]:
    return sorted(LOCALES)


@router.get("/configs", response_model=list[schemas.RenaultConfigOut])
def list_configs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    configs = db.query(models.RenaultConfig).filter(models.RenaultConfig.user_id == user.id).all()
    return [_to_out(db, c) for c in configs]


@router.get("/configs/{vehicle_id}", response_model=schemas.RenaultConfigOut)
def get_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return _to_out(db, _get_or_create_config(db, user, vehicle_id))


def _is_complete(config: models.RenaultConfig) -> bool:
    return bool(config.email and config.password and config.vin)


@router.put("/configs/{vehicle_id}", response_model=schemas.RenaultConfigOut)
def update_config(
    vehicle_id: str,
    payload: schemas.RenaultConfigIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    was_enabled = config.enabled
    complete_before = _is_complete(config)
    credentials_entered = bool(payload.password)
    if payload.locale not in LOCALES:
        raise HTTPException(422, f"Unbekanntes Land/Sprache: {payload.locale}")

    account_changed = (
        payload.locale != config.locale
        or (payload.email is not None and payload.email != config.email)
        or (payload.password is not None and payload.password != config.password)
    )
    config.locale = payload.locale
    if payload.email is not None:
        config.email = payload.email
    if payload.password is not None:
        config.password = payload.password
    if account_changed:
        # Anderes Konto: Token und Kontokennung gehoeren zum alten. Die
        # Geraetekennung bleibt - sie beschreibt diesen Server, nicht das Konto.
        config.login_token = None
        config.account_id = None
    if payload.vin is not None:
        vin = payload.vin.upper()
        if len(vin) != VIN_LENGTH:
            raise HTTPException(
                422, f"Die FIN muss {VIN_LENGTH} Zeichen lang sein (eingetragen: {len(vin)})"
            )
        if vin != config.vin:
            config.model_code = None
            config.model_label = None
        config.vin = vin

    enabled = payload.enabled or should_enable(
        db, config, complete_before=complete_before,
        complete_now=_is_complete(config), credentials_entered=credentials_entered,
    )
    if enabled:
        if not (config.email and config.password):
            raise HTTPException(422, "Ohne E-Mail-Adresse und Passwort kann der Abruf nicht aktiviert werden")
        if not config.vin:
            raise HTTPException(422, "Ohne FIN kann der Abruf nicht aktiviert werden")

    config.enabled = enabled
    config.poll_interval_idle_minutes = payload.poll_interval_idle_minutes
    config.poll_interval_active_minutes = payload.poll_interval_active_minutes
    config.detect_missed_sessions = payload.detect_missed_sessions
    config.missed_session_min_soc_delta = payload.missed_session_min_soc_delta
    config.backdate_session_start = payload.backdate_session_start
    config.backdate_max_gap_minutes = payload.backdate_max_gap_minutes
    config.log_enabled = payload.log_enabled
    config.log_raw_payload = payload.log_raw_payload
    if account_changed or (config.enabled and not was_enabled):
        config.next_poll_at = None
        if account_changed and config.last_status in ("auth_error", "tfa_required"):
            config.last_status = None
            config.last_error = None
    db.commit()
    db.refresh(config)
    if account_changed:
        forget_client(config.id)
    return _to_out(db, config)


@router.delete("/configs/{vehicle_id}", status_code=204)
def delete_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Entfernt Konfiguration, Zugangsdaten und Protokoll. Angelegte
    Ladevorgaenge bleiben."""
    config = _find_config(db, user, vehicle_id)
    if not config:
        raise HTTPException(404, "Keine Konfiguration fuer dieses Fahrzeug")
    forget_client(config.id)
    db.query(models.RenaultLogEntry).filter(
        models.RenaultLogEntry.vehicle_id == vehicle_id,
        models.RenaultLogEntry.user_id == user.id,
    ).delete(synchronize_session=False)
    db.delete(config)
    db.commit()


def _login_result(db: Session, config: models.RenaultConfig, result: dict) -> schemas.RenaultLoginResult:
    db.refresh(config)
    return schemas.RenaultLoginResult(
        status=result["status"],
        email_hint=result["email_hint"],
        error=result["error"],
        config=_to_out(db, config),
    )


@router.post("/configs/{vehicle_id}/login", response_model=schemas.RenaultLoginResult)
def login(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Mit den gespeicherten Zugangsdaten anmelden. Verlangt Renault einen
    Bestaetigungscode, geht er an die E-Mail-Adresse des Kontos
    (`status=code_sent`)."""
    config = _get_or_create_config(db, user, vehicle_id)
    if not (config.email and config.password):
        raise HTTPException(422, "Bitte zuerst E-Mail-Adresse und Passwort speichern")
    return _login_result(db, config, start_login(db, config))


@router.post("/configs/{vehicle_id}/login/code", response_model=schemas.RenaultLoginResult)
def login_code(
    vehicle_id: str,
    payload: schemas.RenaultCodeIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user, vehicle_id)
    return _login_result(db, config, complete_login(db, config, payload.code))


@router.post("/configs/{vehicle_id}/test", response_model=schemas.RenaultTestResult)
def test_config(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Fahrzeuge und Akkustatus lesen, ohne Ladeerkennung."""
    config = _get_or_create_config(db, user, vehicle_id)
    if not (config.email and config.password):
        raise HTTPException(422, "Bitte zuerst E-Mail-Adresse und Passwort speichern")
    result = test_connection(db, config)
    db.refresh(config)
    return schemas.RenaultTestResult(
        ok=result["ok"],
        error=result["error"],
        vehicles=result["vehicles"],
        summary=result["summary"],
        config=_to_out(db, config),
    )


@router.post("/configs/{vehicle_id}/poll", response_model=schemas.RenaultConfigOut)
def poll_now(
    vehicle_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Sofort abrufen, inklusive Ladeerkennung - auch bei ausgeschaltetem
    Abruf."""
    config = _get_or_create_config(db, user, vehicle_id)
    if not (config.email and config.password and config.vin):
        raise HTTPException(422, "Bitte zuerst E-Mail-Adresse, Passwort und FIN speichern")
    poll_vehicle(db, config)
    db.refresh(config)
    return _to_out(db, config)


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------

@router.get("/logs", response_model=list[schemas.RenaultLogOut])
def list_logs(
    vehicle_id: str | None = None,
    limit: int = Query(default=200, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.RenaultLogEntry).filter(models.RenaultLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.RenaultLogEntry.vehicle_id == vehicle_id)
    entries = q.order_by(models.RenaultLogEntry.created_at.desc()).limit(limit).all()
    return [
        schemas.RenaultLogOut(
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


@router.get("/logs/export.zip")
def export_logs_zip(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Alle Protokolleintraege samt Rohdaten als ZIP (siehe log_export.py)."""
    q = db.query(models.RenaultLogEntry).filter(models.RenaultLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.RenaultLogEntry.vehicle_id == vehicle_id)
    return zip_response(q.order_by(models.RenaultLogEntry.created_at.asc()).all(), "renault")


@router.get("/logs/{entry_id}/payload")
def get_log_payload(
    entry_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    entry = (
        db.query(models.RenaultLogEntry)
        .filter(models.RenaultLogEntry.id == entry_id, models.RenaultLogEntry.user_id == user.id)
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
    q = db.query(models.RenaultLogEntry).filter(models.RenaultLogEntry.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.RenaultLogEntry.vehicle_id == vehicle_id)
    q.delete(synchronize_session=False)
    db.commit()
