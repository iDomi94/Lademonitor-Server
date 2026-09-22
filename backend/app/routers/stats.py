from collections import defaultdict
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..consumption import compute_vehicle_consumptions
from ..database import get_db
from ..fees import load_allocation
from ..temperature import (
    BUCKET_WIDTH_C,
    build_buckets,
    build_seasons,
    build_trend,
    collect_points,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=schemas.StatsSummary)
def stats_summary(
    vehicle_id: str | None = None,
    # Inklusiver Datumsfilter, wie bei GET /api/sessions - schraenkt hier zusaetzlich
    # ein, worueber Verbrauch/km-Differenzen berechnet werden (dieselbe Logik wie beim
    # bestehenden vehicle_id-Filter: der Filter wirkt VOR der Aggregation).
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.ChargingSession).filter(models.ChargingSession.user_id == user.id)
    if vehicle_id:
        q = q.filter(models.ChargingSession.vehicle_id == vehicle_id)
    if start_date:
        q = q.filter(models.ChargingSession.start_time >= datetime.combine(start_date, time.min))
    if end_date:
        q = q.filter(models.ChargingSession.start_time <= datetime.combine(end_date, time.max))
    sessions = q.order_by(models.ChargingSession.start_time).all()

    # Grundgebuehren (fees.py): umgelegt wird ueber ALLE Vorgaenge des
    # Nutzers, erst danach greifen die Filter oben - sonst bekaeme der letzte
    # Vorgang vor der Filtergrenze die ganze Gebuehr seiner Periode.
    allocation = load_allocation(db, user.id)
    shares = allocation.shares

    def cost(s: models.ChargingSession) -> float:
        """Kosten eines Vorgangs inkl. seines Grundgebuehr-Anteils."""
        return (s.price_total or 0) + shares.get(s.id, 0)

    # Perioden ohne einen einzigen Ladevorgang: bezahlt, aber an keinem
    # Vorgang sichtbar. Sie gehoeren zu keinem Fahrzeug (ein Abo gilt fuers
    # Konto), fallen bei einem Fahrzeugfilter also weg; beim Datumsfilter
    # zaehlt der Beginn der Periode.
    unallocated = []
    if not vehicle_id:
        unallocated = [
            p
            for p in allocation.unallocated
            if (not start_date or p.start >= start_date) and (not end_date or p.start <= end_date)
        ]

    total_sessions = len(sessions)
    total_kwh = sum(s.energy_kwh or 0 for s in sessions)
    session_fees = sum(shares.get(s.id, 0) for s in sessions)
    unallocated_fees = sum(p.amount for p in unallocated)
    total_fees = session_fees + unallocated_fees
    total_cost = sum(s.price_total or 0 for s in sessions) + total_fees

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
            # Wie bei den kWh zaehlen die Kosten ab dem zweiten Vorgang (die
            # Strecke VOR dem ersten liegt ausserhalb der km-Differenz), und
            # von den nicht umgelegten Gebuehren nur die, deren Periode in
            # genau diesem Zeitraum beginnt.
            first_day = with_odo[0].start_time.date()
            last_day = with_odo[-1].start_time.date()
            cost_in_range = sum(cost(s) for s in with_odo[1:]) + sum(
                p.amount for p in unallocated if first_day < p.start <= last_day
            )
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
    provider_fees: dict[str, float] = defaultdict(float)
    for s in sessions:
        name = providers_by_id[s.provider_id].name if s.provider_id in providers_by_id else "Ohne Anbieter"
        provider_kwh[name] += s.energy_kwh or 0
        provider_cost[name] += cost(s)
        provider_fees[name] += shares.get(s.id, 0)
    if unallocated:
        fee_provider_ids = {p.provider_id for p in unallocated} - set(providers_by_id)
        if fee_provider_ids:
            providers_by_id.update(
                {
                    p.id: p
                    for p in db.query(models.Provider)
                    .filter(models.Provider.id.in_(fee_provider_ids))
                    .all()
                }
            )
        for p in unallocated:
            name = providers_by_id[p.provider_id].name if p.provider_id in providers_by_id else "Ohne Anbieter"
            provider_kwh[name] += 0  # Anbieter auch ohne kWh in der Liste fuehren
            provider_cost[name] += p.amount
            provider_fees[name] += p.amount
    by_provider = [
        schemas.ProviderStat(
            provider_name=name,
            total_kwh=round(provider_kwh[name], 2),
            total_cost=round(provider_cost[name], 2),
            total_fees=round(provider_fees[name], 2),
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
    monthly_fees: dict[str, float] = defaultdict(float)
    monthly_kwh: dict[str, float] = defaultdict(float)
    monthly_count: dict[str, int] = defaultdict(int)
    monthly_consumption_num: dict[str, float] = defaultdict(float)
    monthly_consumption_km: dict[str, float] = defaultdict(float)
    # Nicht umgelegte Gebuehren im Monat, in dem ihre Periode beginnt.
    for p in unallocated:
        key = p.start.strftime("%Y-%m")
        monthly_cost[key] += p.amount
        monthly_fees[key] += p.amount
    for s in sessions:
        key = s.start_time.strftime("%Y-%m")
        monthly_cost[key] += cost(s)
        monthly_fees[key] += shares.get(s.id, 0)
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
            total_fees=round(monthly_fees[month], 2),
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
        total_fees=round(total_fees, 2),
        unallocated_fees=round(unallocated_fees, 2),
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


@router.get("/temperature", response_model=schemas.TemperatureStats)
def stats_temperature(
    vehicle_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Verbrauch gegen Aussentemperatur und Jahreszeit.

    Eigener Endpunkt statt eines weiteren Feldes in `/summary`: die Antwort
    enthaelt einen Punkt JE FAHRT (fuer das Streudiagramm) und waere damit um
    ein Vielfaches groesser als die Kennzahlen, die das Dashboard sonst
    braucht - die soll nicht jeder Seitenaufruf mitschleppen.

    Wichtig beim Datumsfilter: anders als bei `/summary` wird hier NICHT die
    gefilterte Menge gerechnet und dann ausgewertet. Die Verbrauchskette
    braucht immer die vollstaendige Fahrzeughistorie (siehe consumption.py),
    sonst bekaeme der erste Vorgang im Zeitraum den falschen Vorgaenger - und
    fuer die Temperatur-Paarung gilt dasselbe. Gefiltert wird deshalb erst
    danach, auf den fertigen Punkten.
    """
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.user_id == user.id)
        .order_by(models.ChargingSession.start_time)
        .all()
    )
    if vehicle_id:
        sessions = [s for s in sessions if s.vehicle_id == vehicle_id]

    capacity_by_vehicle_id = {
        v.id: v.battery_capacity_kwh
        for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id).all()
    }

    points, without_temp = collect_points(sessions, capacity_by_vehicle_id)

    if start_date:
        limit = datetime.combine(start_date, time.min)
        points = [p for p in points if p.start_time >= limit]
    if end_date:
        limit = datetime.combine(end_date, time.max)
        points = [p for p in points if p.start_time <= limit]

    return schemas.TemperatureStats(
        points=[
            schemas.TempPointOut(
                session_id=p.session_id,
                start_time=p.start_time,
                temp_c=p.temp_c,
                consumption_kwh_per_100km=p.consumption,
                km=round(p.km, 1),
                consumption_method=p.method,
                season=p.season,
            )
            for p in points
        ],
        buckets=[
            schemas.TempBucketOut(
                from_c=b.from_c,
                to_c=b.to_c,
                avg_consumption_kwh_per_100km=b.avg_consumption,
                session_count=b.session_count,
                km=b.km,
            )
            for b in build_buckets(points)
        ],
        seasons=[
            schemas.SeasonStatOut(
                season=s.season,
                avg_consumption_kwh_per_100km=s.avg_consumption,
                session_count=s.session_count,
                km=s.km,
            )
            for s in build_seasons(points)
        ],
        trend=(
            schemas.TempTrendOut(
                slope=trend.slope,
                intercept=trend.intercept,
                r2=trend.r2,
                consumption_at_0c=trend.consumption_at_0c,
                consumption_at_20c=trend.consumption_at_20c,
                extra_pct_at_0c=trend.extra_pct_at_0c,
            )
            if (trend := build_trend(points))
            else None
        ),
        sessions_without_temp=without_temp,
        bucket_width_c=BUCKET_WIDTH_C,
    )
