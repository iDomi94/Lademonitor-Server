"""Grundgebuehren/Abos der Anbieter (siehe models.ProviderFee, fees.py).

Flach unter /api/provider-fees statt unter /api/providers/{id}/fees: die
Apps spiegeln die Tabelle wie die vier Kern-Entitaeten ueber eine einfache
Liste, und `provider_id` ist ohnehin ein Feld des Datensatzes.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..fees import charged_to_date
from ..sync import record_deletion

router = APIRouter(prefix="/api/provider-fees", tags=["provider-fees"])


def _day(value: datetime | None) -> datetime | None:
    """Nur das Datum zaehlt - eine mitgeschickte Uhrzeit (oder Zeitzone) wird
    verworfen, sonst verschiebt ein Client in einer anderen Zeitzone die
    Periodengrenzen um einen Tag."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def _validate(fee: models.ProviderFee) -> None:
    if fee.interval == models.FeeInterval.ONCE and fee.end_date is None:
        raise HTTPException(422, "Eine einmalige Gebuehr braucht ein Enddatum")
    if fee.end_date is not None and fee.end_date < fee.start_date:
        raise HTTPException(422, "Das Enddatum liegt vor dem Startdatum")


def _require_own_provider(db: Session, user: models.User, provider_id: str) -> None:
    provider = (
        db.query(models.Provider)
        .filter(models.Provider.id == provider_id, models.Provider.user_id == user.id)
        .first()
    )
    if not provider:
        raise HTTPException(404, "Anbieter nicht gefunden")


def _out(fee: models.ProviderFee) -> schemas.ProviderFeeOut:
    out = schemas.ProviderFeeOut.model_validate(fee)
    out.charged_to_date = charged_to_date(fee)
    return out


@router.get("", response_model=list[schemas.ProviderFeeOut])
def list_fees(
    provider_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ProviderFee).filter(models.ProviderFee.user_id == user.id)
    if provider_id:
        q = q.filter(models.ProviderFee.provider_id == provider_id)
    # Neueste zuerst - das ist die gerade laufende.
    return [_out(f) for f in q.order_by(models.ProviderFee.start_date.desc()).all()]


@router.post("", response_model=schemas.ProviderFeeOut, status_code=201)
def create_fee(
    payload: schemas.ProviderFeeCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_own_provider(db, user, payload.provider_id)
    data = payload.model_dump()
    data["start_date"] = _day(data["start_date"])
    data["end_date"] = _day(data["end_date"])
    fee = models.ProviderFee(**data, user_id=user.id)
    _validate(fee)
    db.add(fee)
    db.commit()
    db.refresh(fee)
    return _out(fee)


@router.patch("/{fee_id}", response_model=schemas.ProviderFeeOut)
def update_fee(
    fee_id: str,
    payload: schemas.ProviderFeeUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    fee = _get_owned(db, user, fee_id)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("provider_id"):
        _require_own_provider(db, user, fields["provider_id"])
    elif "provider_id" in fields:
        fields.pop("provider_id")  # null = unveraendert, eine Gebuehr ohne Anbieter gibt es nicht
    if fields.get("start_date") is None:
        fields.pop("start_date", None)
    for key in ("start_date", "end_date"):
        if key in fields:
            fields[key] = _day(fields[key])
    if fields.get("amount") is None:
        fields.pop("amount", None)
    if fields.get("interval") is None:
        fields.pop("interval", None)
    for field, value in fields.items():
        setattr(fee, field, value)
    _validate(fee)
    db.commit()
    db.refresh(fee)
    return _out(fee)


@router.delete("/{fee_id}", status_code=204)
def delete_fee(
    fee_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    fee = _get_owned(db, user, fee_id)
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.PROVIDER_FEE, fee.id)
    db.delete(fee)
    db.commit()


def _get_owned(db: Session, user: models.User, fee_id: str) -> models.ProviderFee:
    fee = (
        db.query(models.ProviderFee)
        .filter(models.ProviderFee.id == fee_id, models.ProviderFee.user_id == user.id)
        .first()
    )
    if not fee:
        raise HTTPException(404, "Gebuehr nicht gefunden")
    return fee
