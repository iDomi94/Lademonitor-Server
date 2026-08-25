from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..webdav_backup import run_backup_for_user

router = APIRouter(prefix="/api/backup/webdav", tags=["webdav-backup"])


def _get_or_create_config(db: Session, user: models.User) -> models.WebdavBackupConfig:
    config = db.query(models.WebdavBackupConfig).filter(models.WebdavBackupConfig.user_id == user.id).first()
    if not config:
        config = models.WebdavBackupConfig(user_id=user.id)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _to_out(config: models.WebdavBackupConfig) -> schemas.WebdavBackupConfigOut:
    return schemas.WebdavBackupConfigOut(
        enabled=config.enabled,
        url=config.url,
        username=config.username,
        has_password=bool(config.password),
        frequency=config.frequency,
        retention_days=config.retention_days,
        last_run_at=config.last_run_at,
        last_status=config.last_status,
        last_error=config.last_error,
    )


@router.get("", response_model=schemas.WebdavBackupConfigOut)
def get_config(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return _to_out(_get_or_create_config(db, user))


@router.put("", response_model=schemas.WebdavBackupConfigOut)
def update_config(
    payload: schemas.WebdavBackupConfigIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    config = _get_or_create_config(db, user)
    config.enabled = payload.enabled
    config.url = payload.url.strip().rstrip("/")
    config.username = payload.username or None
    if payload.password:
        config.password = payload.password
    config.frequency = payload.frequency
    config.retention_days = payload.retention_days
    db.commit()
    db.refresh(config)
    return _to_out(config)


@router.post("/run", response_model=schemas.WebdavBackupConfigOut)
def run_now(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    config = _get_or_create_config(db, user)
    if not config.url:
        raise HTTPException(422, "Bitte zuerst eine WebDAV-Adresse eintragen und speichern")
    run_backup_for_user(db, user, config)
    return _to_out(config)
