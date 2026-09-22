"""Reifensaetze verwalten und vergleichen.

Warum die Auswertung hier haengt und nicht unter `/api/stats`: sie braucht
neben den Ladevorgaengen die Reifentabelle und gehoert damit fachlich zu
diesem Router. `/api/stats/temperature` bleibt die Quelle der Punkte - beide
Auswertungen rechnen auf derselben Grundlage (siehe tires.compare()).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas, tires
from ..auth import get_current_user
from ..consumption import compute_vehicle_consumptions
from ..database import get_db
from ..temperature import collect_points

router = APIRouter(prefix="/api/tires", tags=["tires"])


@router.get("", response_model=list[schemas.TireSetOut])
def list_tire_sets(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    query = db.query(models.TireSet).filter(models.TireSet.user_id == user.id)
    if vehicle_id:
        query = query.filter(models.TireSet.vehicle_id == vehicle_id)
    # Neuester Wechsel zuerst - das ist der montierte Satz, und danach wird am
    # haeufigsten gesucht.
    return query.order_by(models.TireSet.installed_on.desc()).all()


@router.post("", response_model=schemas.TireSetOut, status_code=201)
def create_tire_set(
    payload: schemas.TireSetCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_own_vehicle(db, user, payload.vehicle_id)
    tire_set = models.TireSet(**payload.model_dump(), user_id=user.id)
    db.add(tire_set)
    db.commit()
    db.refresh(tire_set)
    return tire_set


@router.patch("/{tire_set_id}", response_model=schemas.TireSetOut)
def update_tire_set(
    tire_set_id: str,
    payload: schemas.TireSetUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    tire_set = _get_owned(db, user, tire_set_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tire_set, field, value)
    db.commit()
    db.refresh(tire_set)
    return tire_set


@router.delete("/{tire_set_id}", status_code=204)
def delete_tire_set(
    tire_set_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    tire_set = _get_owned(db, user, tire_set_id)
    # Kein Grabstein: Reifen sind nicht Teil von SyncEntityType, weil die Apps
    # sie (noch) gar nicht kennen - siehe models.TireSet.
    db.delete(tire_set)
    db.commit()


@router.get("/overview", response_model=schemas.TireOverviewOut)
def tire_overview(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Laufleistung, Dauer und Fahrten je Montage und je Satz.

    Getrennt vom Vergleich, weil die Frage eine andere ist: hier geht es um
    Alter und Kilometer (also um den naechsten Reifenkauf), dort um den
    Verbrauch. Zusammengelegt braeuchte eine Antwort beides, obwohl die
    Uebersicht auch dann etwas zeigt, wenn fuer den Vergleich noch viel zu
    wenige Fahrten mit Temperatur vorliegen.
    """
    sessions, tire_sets = _own_data(db, user, vehicle_id)

    consumptions: dict = {}
    for vid in {s.vehicle_id for s in sessions}:
        vehicle = db.get(models.Vehicle, vid)
        consumptions.update(
            compute_vehicle_consumptions(
                [s for s in sessions if s.vehicle_id == vid],
                vehicle.battery_capacity_kwh if vehicle else None,
            )
        )

    result = tires.overview(sessions, tire_sets, consumptions)
    return schemas.TireOverviewOut(
        mountings=[
            schemas.TireMountingOut(
                tire_set_id=m.tire_set_id,
                vehicle_id=m.vehicle_id,
                kind=m.kind,
                label=m.label,
                installed_on=m.installed_on,
                removed_on=m.removed_on,
                is_current=m.is_current,
                days=m.days,
                drives=m.drives,
                km=m.km,
                km_source=m.km_source,
                energy_kwh=m.energy_kwh,
                avg_consumption_kwh_per_100km=m.avg_consumption,
            )
            for m in result.mountings
        ],
        sets=[
            schemas.TireSetSummaryOut(
                key=s.key,
                label=s.label,
                kind=s.kind,
                mountings=s.mountings,
                first_installed_on=s.first_installed_on,
                age_days=s.age_days,
                days_mounted=s.days_mounted,
                drives=s.drives,
                km=s.km,
                km_source=s.km_source,
                energy_kwh=s.energy_kwh,
                is_current=s.is_current,
                avg_consumption_kwh_per_100km=s.avg_consumption,
            )
            for s in result.sets
        ],
        drives_without_set=result.drives_without_set,
        drives_spanning_change=result.drives_spanning_change,
    )


@router.get("/comparison", response_model=schemas.TireComparisonOut)
def tire_comparison(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Verbrauch je Reifenart und je Satz, temperaturbereinigt.

    Kein Datumsfilter, anders als bei den uebrigen Auswertungen: ein Reifensatz
    IST bereits ein Zeitraum. Ihn zusaetzlich zu beschneiden wuerde die
    Gruppen verkleinern, ohne die Frage zu schaerfen.
    """
    sessions, tire_sets = _own_data(db, user, vehicle_id)

    capacity_by_vehicle_id = {
        v.id: v.battery_capacity_kwh
        for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id).all()
    }
    points, _ = collect_points(sessions, capacity_by_vehicle_id)
    result = tires.compare(sessions, tire_sets, points)

    return schemas.TireComparisonOut(
        by_kind=[_group_out(g) for g in result.by_kind],
        by_set=[_group_out(g) for g in result.by_set],
        reference_temp_c=result.reference_temp_c,
        model=result.model,
        r2=result.r2,
        drives_without_set=result.drives_without_set,
        drives_spanning_change=result.drives_spanning_change,
        overlap_span_c=result.overlap_span_c,
        overlap_ok=result.overlap_ok,
        winter_vs_summer_pct=result.winter_vs_summer_pct,
    )


def _own_data(
    db: Session, user: models.User, vehicle_id: str | None
) -> tuple[list[models.ChargingSession], list[models.TireSet]]:
    """Ladevorgaenge und Reifensaetze des Nutzers, chronologisch.

    Der Fahrzeugfilter greift erst in Python: beide Auswertungen brauchen die
    Vorgaenge in einem Stueck, und die Menge ist die eines einzelnen Nutzers.
    """
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.user_id == user.id)
        .order_by(models.ChargingSession.start_time)
        .all()
    )
    tire_sets = db.query(models.TireSet).filter(models.TireSet.user_id == user.id).all()
    if vehicle_id:
        sessions = [s for s in sessions if s.vehicle_id == vehicle_id]
        tire_sets = [t for t in tire_sets if t.vehicle_id == vehicle_id]
    return sessions, tire_sets


def _group_out(group: tires.GroupStat) -> schemas.TireGroupOut:
    return schemas.TireGroupOut(
        key=group.key,
        label=group.label,
        kind=group.kind,
        drives=group.drives,
        km=group.km,
        avg_consumption_kwh_per_100km=group.avg_consumption,
        avg_temp_c=group.avg_temp_c,
        min_temp_c=group.min_temp_c,
        max_temp_c=group.max_temp_c,
        adjusted_consumption_kwh_per_100km=group.adjusted_consumption,
        delta_pct_vs_model=group.delta_pct_vs_model,
    )


def _require_own_vehicle(db: Session, user: models.User, vehicle_id: str) -> None:
    exists = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not exists:
        raise HTTPException(404, "Fahrzeug nicht gefunden")


def _get_owned(db: Session, user: models.User, tire_set_id: str) -> models.TireSet:
    tire_set = (
        db.query(models.TireSet)
        .filter(models.TireSet.id == tire_set_id, models.TireSet.user_id == user.id)
        .first()
    )
    if not tire_set:
        raise HTTPException(404, "Reifensatz nicht gefunden")
    return tire_set
