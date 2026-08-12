from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..consumption import compute_vehicle_consumptions
from ..database import get_db

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=schemas.StatsSummary)
def stats_summary(
    vehicle_id: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ChargingSession).filter(models.ChargingSession.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.ChargingSession.vehicle_id == vehicle_id)
    sessions = q.order_by(models.ChargingSession.start_time).all()

    total_sessions = len(sessions)
    total_kwh = sum(s.energy_kwh or 0 for s in sessions)
    total_cost = sum(s.price_total or 0 for s in sessions)

    # Gewichteter Durchschnittspreis: Gesamtkosten / Gesamt-kWh (nicht der simple
    # Mittelwert der Einzelpreise, da sonst kleine Ladevorgänge das Bild verzerren)
    avg_price = round(total_cost / total_kwh, 4) if total_kwh else None

    # AC/DC-Anteil nach geladener Energie, NICHT nach Anzahl Ladevorgänge -
    # ein langer AC-Ladevorgang zuhause zaehlt sonst genauso viel wie eine
    # kurze DC-Schnellladung und verzerrt das Bild
    ac_kwh = sum(s.energy_kwh or 0 for s in sessions if s.charging_type == models.ChargingType.AC)
    dc_kwh = sum(s.energy_kwh or 0 for s in sessions if s.charging_type == models.ChargingType.DC)
    typed_kwh = ac_kwh + dc_kwh
    ac_share = round(ac_kwh / typed_kwh * 100, 1) if typed_kwh else None
    dc_share = round(dc_kwh / typed_kwh * 100, 1) if typed_kwh else None

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

    # Anbieter-Aufteilung (kWh + bezahlt) - Sessions ohne Anbieter landen in
    # einem eigenen "Ohne Anbieter"-Eintrag, damit die Summen vollstaendig bleiben
    provider_ids = {s.provider_id for s in sessions if s.provider_id}
    providers_by_id = (
        {p.id: p for p in db.query(models.Provider).filter(models.Provider.id.in_(provider_ids)).all()}
        if provider_ids
        else {}
    )
    provider_kwh: dict[str, float] = defaultdict(float)
    provider_cost: dict[str, float] = defaultdict(float)
    for s in sessions:
        name = providers_by_id[s.provider_id].name if s.provider_id in providers_by_id else "Ohne Anbieter"
        provider_kwh[name] += s.energy_kwh or 0
        provider_cost[name] += s.price_total or 0
    by_provider = [
        schemas.ProviderStat(
            provider_name=name,
            total_kwh=round(provider_kwh[name], 2),
            total_cost=round(provider_cost[name], 2),
        )
        # Groesster Anteil zuerst, passend zur Legenden-Reihenfolge im Web-UI
        for name in sorted(provider_kwh.keys(), key=lambda n: provider_kwh[n], reverse=True)
    ]

    # Monatlicher Verbrauch: dieselbe Fallback-Kette wie pro Ladevorgang
    # (consumption.py), km-gewichtet gemittelt statt naiv pro Session -
    # sessions ist hier bereits die vollstaendige (ungepaginierte) Menge,
    # daher reicht Gruppieren in Python statt erneuter DB-Abfrage pro Fahrzeug
    consumption_by_session_id = {}
    for vid in {s.vehicle_id for s in sessions}:
        vehicle = db.get(models.Vehicle, vid)
        vehicle_sessions = [s for s in sessions if s.vehicle_id == vid]
        capacity = vehicle.battery_capacity_kwh if vehicle else None
        consumption_by_session_id.update(compute_vehicle_consumptions(vehicle_sessions, capacity))

    monthly_cost: dict[str, float] = defaultdict(float)
    monthly_kwh: dict[str, float] = defaultdict(float)
    monthly_count: dict[str, int] = defaultdict(int)
    monthly_consumption_num: dict[str, float] = defaultdict(float)
    monthly_consumption_km: dict[str, float] = defaultdict(float)
    for s in sessions:
        key = s.start_time.strftime("%Y-%m")
        monthly_cost[key] += s.price_total or 0
        monthly_kwh[key] += s.energy_kwh or 0
        monthly_count[key] += 1
        result = consumption_by_session_id.get(s.id)
        if result and result.value is not None and result.km:
            monthly_consumption_num[key] += result.value * result.km
            monthly_consumption_km[key] += result.km

    monthly = [
        schemas.MonthlyStat(
            month=month,
            total_cost=round(monthly_cost[month], 2),
            total_kwh=round(monthly_kwh[month], 2),
            session_count=monthly_count[month],
            avg_consumption_kwh_per_100km=(
                round(monthly_consumption_num[month] / monthly_consumption_km[month], 1)
                if monthly_consumption_km.get(month)
                else None
            ),
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
        ac_kwh=round(ac_kwh, 2),
        dc_kwh=round(dc_kwh, 2),
        total_km_driven=total_km_driven,
        by_provider=by_provider,
        monthly=monthly,
    )
