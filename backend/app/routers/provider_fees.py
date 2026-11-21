"""Grundgebuehren/Abos der Anbieter (siehe models.ProviderFee, fees.py).

Flach unter /api/provider-fees statt unter /api/providers/{id}/fees: die
Apps spiegeln die Tabelle wie die vier Kern-Entitaeten ueber eine einfache
Liste, und `provider_id` ist ohnehin ein Feld des Datensatzes.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..idempotency import existing as existing_for_key, key_header, remember as remember_key
from ..database import get_db
from ..fees import add_months, charged_to_date
from ..sync import record_deletion

router = APIRouter(prefix="/api/provider-fees", tags=["provider-fees"])


def _day(value: datetime | None) -> datetime | None:
    """Nur das Datum zaehlt - eine mitgeschickte Uhrzeit (oder Zeitzone) wird
    verworfen, sonst verschiebt ein Client in einer anderen Zeitzone die
    Periodengrenzen um einen Tag."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def _normalize(fee: models.ProviderFee) -> None:
    """Leitet die abhaengigen Felder ab, bevor geprueft wird.

    Eine Ladung bzw. x Monate gibt es nur bei `once`. Bei x Monaten steht das
    Enddatum fest (letzter Tag vor dem Monatstag x Monate spaeter) - so sehen
    auch aeltere Apps, die `spread_months` nicht kennen, den richtigen
    Zeitraum. Eine Ladung hat kein Enddatum.
    """
    if fee.interval != models.FeeInterval.ONCE:
        fee.single_session = False
        fee.spread_months = None
        return
    if fee.single_session:
        fee.spread_months = None
        fee.end_date = None
    elif fee.spread_months:
        start = fee.start_date.date()
        end = add_months(start, fee.spread_months) - timedelta(days=1)
        fee.end_date = datetime(end.year, end.month, end.day)


def _validate(fee: models.ProviderFee) -> None:
    if fee.amount is None or fee.amount == 0:
        raise HTTPException(422, "Der Betrag darf nicht 0 sein")
    if fee.interval == models.FeeInterval.ONCE and fee.end_date is None and not fee.single_session:
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
    idem_key: str | None = Depends(key_header),
):
    # Wiederholte Anlage (Antwort ging verloren): dieselbe Zeile zurueck.
    found = existing_for_key(db, user, idem_key, models.ProviderFee)
    if found is not None:
        return _out(found)
    _require_own_provider(db, user, payload.provider_id)
    data = payload.model_dump()
    data["start_date"] = _day(data["start_date"])
    data["end_date"] = _day(data["end_date"])
    fee = models.ProviderFee(**data, user_id=user.id)
    _normalize(fee)
    _validate(fee)
    db.add(fee)
    remember_key(db, user, idem_key, fee)
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
    if fields.get("single_session") is None:
        fields.pop("single_session", None)
    if "end_date" in fields and "spread_months" not in fields:
        # Aeltere App hat das Enddatum gesetzt: das gilt dann, nicht mehr x Monate.
        fee.spread_months = None
    for field, value in fields.items():
        setattr(fee, field, value)
    _normalize(fee)
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
