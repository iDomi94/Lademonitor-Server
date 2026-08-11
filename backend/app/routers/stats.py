from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=schemas.StatsSummary)
def stats_summary(vehicle_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.ChargingSession)
    if vehicle_id:
        q = q.filter(models.ChargingSession.vehicle_id == vehicle_id)
    sessions = q.order_by(models.ChargingSession.start_time).all()

    total_sessions = len(sessions)
    total_kwh = sum(s.energy_kwh or 0 for s in sessions)
    total_cost = sum(s.price_total or 0 for s in sessions)

    # Gewichteter Durchschnittspreis: Gesamtkosten / Gesamt-kWh (nicht der simple
    # Mittelwert der Einzelpreise, da sonst kleine Ladevorgänge das Bild verzerren)
    avg_price = round(total_cost / total_kwh, 4) if total_kwh else None

    ac_count = sum(1 for s in sessions if s.charging_type == models.ChargingType.AC)
    dc_count = sum(1 for s in sessions if s.charging_type == models.ChargingType.DC)
    typed_total = ac_count + dc_count
    ac_share = round(ac_count / typed_total * 100, 1) if typed_total else None
    dc_share = round(dc_count / typed_total * 100, 1) if typed_total else None

    # Verbrauch: kWh pro 100km ueber Kilometerstand-Differenzen zwischen aufeinanderfolgenden Sessions
    with_odo = [s for s in sessions if s.odometer_km is not None]
    consumption = None
    price_per_100km = None
    if len(with_odo) >= 2:
        km_driven = with_odo[-1].odometer_km - with_odo[0].odometer_km
        kwh_in_range = sum(s.energy_kwh or 0 for s in with_odo[1:])
        if km_driven > 0:
            consumption = round(kwh_in_range / km_driven * 100, 1)
            cost_in_range = sum(s.price_total or 0 for s in with_odo[1:])
            price_per_100km = round(cost_in_range / km_driven * 100, 2)

    monthly_cost: dict[str, float] = defaultdict(float)
    monthly_kwh: dict[str, float] = defaultdict(float)
    monthly_count: dict[str, int] = defaultdict(int)
    for s in sessions:
        key = s.start_time.strftime("%Y-%m")
        monthly_cost[key] += s.price_total or 0
        monthly_kwh[key] += s.energy_kwh or 0
        monthly_count[key] += 1

    monthly = [
        schemas.MonthlyStat(
            month=month,
            total_cost=round(monthly_cost[month], 2),
            total_kwh=round(monthly_kwh[month], 2),
            session_count=monthly_count[month],
        )
        # Neuester Monat zuerst
        for month in sorted(monthly_cost.keys(), reverse=True)
    ]

    total_km_driven = (
        with_odo[-1].odometer_km - with_odo[0].odometer_km
        if len(with_odo) >= 2
        else None
    )

    return schemas.StatsSummary(
        total_sessions=total_sessions,
        total_kwh=round(total_kwh, 2),
        total_cost=round(total_cost, 2),
        avg_price_per_kwh=avg_price,
        avg_consumption_kwh_per_100km=consumption,
        price_per_100km=price_per_100km,
        ac_share_pct=ac_share,
        dc_share_pct=dc_share,
        total_km_driven=total_km_driven,
        monthly=monthly,
    )
