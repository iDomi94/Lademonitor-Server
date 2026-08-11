from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("", response_model=list[schemas.ProviderOut])
def list_providers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.Provider)
        .filter(models.Provider.user_id == user.id)
        .order_by(models.Provider.name)
        .all()
    )


@router.post("", response_model=schemas.ProviderOut, status_code=201)
def create_provider(
    payload: schemas.ProviderCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    existing = (
        db.query(models.Provider)
        .filter(models.Provider.name == payload.name, models.Provider.user_id == user.id)
        .first()
    )
    if existing:
        raise HTTPException(409, "Anbieter mit diesem Namen existiert bereits")
    provider = models.Provider(**payload.model_dump(), user_id=user.id)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch("/{provider_id}", response_model=schemas.ProviderOut)
def update_provider(
    provider_id: str,
    payload: schemas.ProviderUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    provider = _get_owned(db, user, provider_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    db.commit()
    db.refresh(provider)
    return provider


@router.delete("/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    provider = _get_owned(db, user, provider_id)
    db.delete(provider)
    db.commit()


def _get_owned(db: Session, user: models.User, provider_id: str) -> models.Provider:
    provider = (
        db.query(models.Provider)
        .filter(models.Provider.id == provider_id, models.Provider.user_id == user.id)
        .first()
    )
    if not provider:
        raise HTTPException(404, "Anbieter nicht gefunden")
    return provider
