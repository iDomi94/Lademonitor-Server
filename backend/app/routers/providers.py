from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("", response_model=list[schemas.ProviderOut])
def list_providers(db: Session = Depends(get_db)):
    return db.query(models.Provider).order_by(models.Provider.name).all()


@router.post("", response_model=schemas.ProviderOut, status_code=201)
def create_provider(payload: schemas.ProviderCreate, db: Session = Depends(get_db)):
    existing = db.query(models.Provider).filter(models.Provider.name == payload.name).first()
    if existing:
        raise HTTPException(409, "Anbieter mit diesem Namen existiert bereits")
    provider = models.Provider(**payload.model_dump())
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch("/{provider_id}", response_model=schemas.ProviderOut)
def update_provider(provider_id: str, payload: schemas.ProviderUpdate, db: Session = Depends(get_db)):
    provider = db.get(models.Provider, provider_id)
    if not provider:
        raise HTTPException(404, "Anbieter nicht gefunden")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    db.commit()
    db.refresh(provider)
    return provider


@router.delete("/{provider_id}", status_code=204)
def delete_provider(provider_id: str, db: Session = Depends(get_db)):
    provider = db.get(models.Provider, provider_id)
    if not provider:
        raise HTTPException(404, "Anbieter nicht gefunden")
    db.delete(provider)
    db.commit()
