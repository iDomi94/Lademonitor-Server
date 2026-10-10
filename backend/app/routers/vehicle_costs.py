"""Sonstige Fahrzeugkosten (siehe models.VehicleCost, vehicle_costs.py).

Flach unter /api/vehicle-costs wie die Grundgebuehren unter
/api/provider-fees: die Apps spiegeln die Tabelle ueber eine einfache Liste.
"""

from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..fees import load_allocation
from ..sync import record_deletion
from ..vehicle_costs import cost_entries, occurrences, ownership_entries

router = APIRouter(prefix="/api/vehicle-costs", tags=["vehicle-costs"])


def _day(value: datetime | None) -> datetime | None:
    """Nur das Datum zaehlt (siehe routers/provider_fees.py)."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def _validate(cost: models.VehicleCost) -> None:
    # Eine einzelne Zahlung hat kein Enddatum - ein mitgeschicktes wird
    # verworfen statt abgelehnt, damit ein Wechsel von "jaehrlich" auf
    # "einmalig" nicht am alten Enddatum scheitert.
    if cost.interval == "once":
        cost.end_date = None
    if cost.end_date is not None and cost.end_date < cost.start_date:
        raise HTTPException(422, "Das Enddatum liegt vor dem Startdatum")


def _require_own_vehicle(db: Session, user: models.User, vehicle_id: str) -> None:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden")


def _out(cost: models.VehicleCost) -> schemas.VehicleCostOut:
    out = schemas.VehicleCostOut.model_validate(cost)
    out.paid_to_date = round(cost.amount * len(occurrences(cost, date.today())), 2)
    return out


@router.get("", response_model=list[schemas.VehicleCostOut])
def list_costs(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.VehicleCost).filter(models.VehicleCost.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.VehicleCost.vehicle_id == vehicle_id)
    return [_out(c) for c in q.order_by(models.VehicleCost.start_date.desc()).all()]


@router.get("/summary", response_model=list[schemas.YearCostStat])
def cost_summary(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Gesamtkosten je Kalenderjahr und Kategorie, neuestes Jahr zuerst.

    Dieselben Bausteine wie der Dashboard-Abschnitt "Gesamtkosten"
    (routers/stats.py::build_vehicle_costs): Laden inkl. Grundgebuehren,
    sonstige Kosten, Wertverlust bzw. Leasing.
    """
    vq = db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id)
    if vehicle_id:
        vq = vq.filter(models.Vehicle.id == vehicle_id)
    vehicles = vq.all()
    ids = [v.id for v in vehicles]
    if not ids:
        return []
    today = date.today()
    costs = db.query(models.VehicleCost).filter(models.VehicleCost.vehicle_id.in_(ids)).all()
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.vehicle_id.in_(ids))
        .order_by(models.ChargingSession.start_time)
        .all()
    )
    entries = cost_entries(costs, today)
    for v in vehicles:
        entries.extend(ownership_entries(v, [s for s in sessions if s.vehicle_id == v.id], today))

    by_year: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    allocation = load_allocation(db, user.id)
    for s in sessions:
        amount = (s.price_total or 0) + allocation.shares.get(s.id, 0)
        if amount:
            by_year[s.start_time.year]["charging"] += amount
    if not vehicle_id:
        for p in allocation.unallocated:
            by_year[p.start.year]["charging"] += p.amount
    for e in entries:
        by_year[e.day.year][e.category] += e.amount

    # Gefahrene km je Jahr: je Fahrzeug letzter Stand des Jahres minus
    # letzter Stand davor (bzw. erster des Jahres) - wie price_per_100km.
    km_by_year: dict[int, int] = defaultdict(int)
    for v in vehicles:
        points = [(s.start_time, s.odometer_km) for s in sessions if s.vehicle_id == v.id and s.odometer_km is not None]
        for year in {t.year for t, _ in points}:
            before = [km for t, km in points if t.year < year]
            within = [km for t, km in points if t.year == year]
            start = before[-1] if before else within[0]
            km_by_year[year] += max(within[-1] - start, 0)

    result = []
    for year in sorted(by_year, reverse=True):
        cats = by_year[year]
        total = sum(cats.values())
        km = km_by_year.get(year) or None
        result.append(
            schemas.YearCostStat(
                year=year,
                total=round(total, 2),
                km_driven=km,
                cost_per_km=round(total / km, 3) if km else None,
                by_category=[
                    schemas.CostCategoryStat(category=c, amount=round(a, 2))
                    for c, a in sorted(cats.items(), key=lambda kv: kv[1], reverse=True)
                    if a
                ],
            )
        )
    return result


@router.post("", response_model=schemas.VehicleCostOut, status_code=201)
def create_cost(
    payload: schemas.VehicleCostCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_own_vehicle(db, user, payload.vehicle_id)
    data = payload.model_dump()
    data["start_date"] = _day(data["start_date"])
    data["end_date"] = _day(data["end_date"])
    cost = models.VehicleCost(**data, user_id=user.id)
    _validate(cost)
    db.add(cost)
    db.commit()
    db.refresh(cost)
    return _out(cost)


@router.patch("/{cost_id}", response_model=schemas.VehicleCostOut)
def update_cost(
    cost_id: str,
    payload: schemas.VehicleCostUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    cost = _get_owned(db, user, cost_id)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("vehicle_id"):
        _require_own_vehicle(db, user, fields["vehicle_id"])
    # null = unveraendert bei allen Pflichtfeldern; leeren lassen sich nur
    # Enddatum, km-Stand, Bezeichnung und Notiz.
    for key in ("vehicle_id", "amount", "category", "interval", "start_date"):
        if key in fields and fields[key] is None:
            fields.pop(key)
    for key in ("start_date", "end_date"):
        if key in fields:
            fields[key] = _day(fields[key])
    for field, value in fields.items():
        setattr(cost, field, value)
    _validate(cost)
    db.commit()
    db.refresh(cost)
    return _out(cost)


@router.delete("/{cost_id}", status_code=204)
def delete_cost(
    cost_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    cost = _get_owned(db, user, cost_id)
    # Grabstein VOR dem Loeschen, im selben Commit - siehe sync.record_deletion().
    record_deletion(db, user.id, models.SyncEntityType.VEHICLE_COST, cost.id)
    db.delete(cost)
    db.commit()


def _get_owned(db: Session, user: models.User, cost_id: str) -> models.VehicleCost:
    cost = (
        db.query(models.VehicleCost)
        .filter(models.VehicleCost.id == cost_id, models.VehicleCost.user_id == user.id)
        .first()
    )
    if not cost:
        raise HTTPException(404, "Kosteneintrag nicht gefunden")
    return cost
