"""Jahres- und Monatsrueckblick.

Eine Seite pro Kalenderjahr bzw. -monat, alles aus vorhandenen Daten: die
grossen Zahlen mit Vergleich zur Vorperiode, die Ersparnis gegenueber einem
Verbrenner, Rekorde und die Verteilung. Verbindlich ist
`ideen/schritt-11-vertrag.md` - die Apps rechnen denselben Rueckblick lokal
nach (auch im Local-Only-Modus), Regelaenderungen hier MUESSEN dort
mitgezogen werden (`tests/test_review.py` haelt die Regeln fest).

Drei Dinge, die sich durch alles ziehen:

* **Zugeordnet wird ueber den lokalen Kalendertag von `start_time`**, beide
  Enden inklusive - wie bei den Grundgebuehren und dem Erstattungsbericht.
* **Gerechnet wird immer ueber die GANZE Historie**, gefiltert erst danach:
  der Verbrauch eines Vorgangs braucht seinen Vorgaenger (consumption.py), die
  km brauchen den letzten Stand VOR dem Zeitraum als Anker, die Gebuehren
  werden ueber alle Vorgaenge des Nutzers umgelegt (fees.py). Ein Zeitraum,
  der sich selbst genuegt, bekaeme an seinem Anfang falsche Werte.
* **Gerundet wird erst in der Anzeige.** Die Antwort traegt rohe Werte, damit
  sich Summen in Web, Mail und Apps nicht durch Zwischenrundung unterscheiden.

Mail (Jahresrueckblick und Monatsbericht, notifications.py) und Endpunkt
(`GET /api/stats/review`) bauen auf derselben Funktion `build_review()` auf.
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from . import models, schemas
from .consumption import compute_vehicle_consumptions
from .fees import load_allocation

# Standardwerte des Vergleichsfahrzeugs, solange der Nutzer nichts gespeichert
# hat (configured=false). Je Kraftstoff ein eigener Verbrauch und Literpreis -
# ein Diesel mit dem Benzinverbrauch waere ein unfairer Vergleich.
COMBUSTION_DEFAULTS = {
    "petrol": {"consumption_l_per_100km": 7.0, "fuel_price_per_l": 1.75},
    "diesel": {"consumption_l_per_100km": 5.8, "fuel_price_per_l": 1.65},
}
DEFAULT_FUEL_TYPE = "petrol"
DEFAULT_GRID_CO2_KG_PER_KWH = 0.36

# CO2 je Liter, nur Tank-to-Wheel - dieselbe Rechnung wie das App-Tool
# "Vergleich mit Verbrenner" (Betrieb). Beim Strom steckt die Vorkette dagegen
# schon im Strommix-Faktor; der Vergleich faellt also eher zugunsten des
# Verbrenners aus.
CO2_KG_PER_LITER = {"petrol": 2.37, "diesel": 2.65}

# Rekorde je kWh erst ab dieser Energie: eine 0,3-kWh-Ladung mit
# Mindestgebuehr waere sonst immer der "teuerste" Vorgang.
MIN_KWH_FOR_PRICE_RECORD = 1.0
# Ein Minimum/Maximum aus einem einzigen Wert ist kein Rekord.
MIN_CANDIDATES = 2


# ---------------------------------------------------------------------------
# Vergleichsfahrzeug
# ---------------------------------------------------------------------------

def combustion_vehicle(user: models.User) -> schemas.CombustionVehicleOut:
    """Gespeicherte Werte oder die Standardwerte. "Gespeichert" heisst: der
    Kraftstoff ist gesetzt - PUT schreibt immer alle vier Felder zusammen."""
    if user.combustion_fuel_type in COMBUSTION_DEFAULTS:
        defaults = COMBUSTION_DEFAULTS[user.combustion_fuel_type]
        return schemas.CombustionVehicleOut(
            fuel_type=user.combustion_fuel_type,
            consumption_l_per_100km=(
                user.combustion_consumption_l_per_100km or defaults["consumption_l_per_100km"]
            ),
            fuel_price_per_l=user.combustion_fuel_price_per_l or defaults["fuel_price_per_l"],
            grid_co2_kg_per_kwh=(
                user.combustion_grid_co2_kg_per_kwh
                if user.combustion_grid_co2_kg_per_kwh is not None
                else DEFAULT_GRID_CO2_KG_PER_KWH
            ),
            configured=True,
            updated_at=user.combustion_updated_at,
        )
    defaults = COMBUSTION_DEFAULTS[DEFAULT_FUEL_TYPE]
    return schemas.CombustionVehicleOut(
        fuel_type=DEFAULT_FUEL_TYPE,
        consumption_l_per_100km=defaults["consumption_l_per_100km"],
        fuel_price_per_l=defaults["fuel_price_per_l"],
        grid_co2_kg_per_kwh=DEFAULT_GRID_CO2_KG_PER_KWH,
        configured=False,
        updated_at=None,
    )


def build_savings(
    totals: schemas.ReviewTotals, vehicle: schemas.CombustionVehicleOut
) -> schemas.ReviewSavings | None:
    """Was dieselben km mit dem Vergleichsfahrzeug gekostet haetten. None ohne
    gefahrene km - dann gibt es nichts zu vergleichen."""
    if totals.km <= 0:
        return None
    liters = totals.km * vehicle.consumption_l_per_100km / 100
    fuel_cost = liters * vehicle.fuel_price_per_l
    co2_combustion = liters * CO2_KG_PER_LITER[vehicle.fuel_type]
    co2_ev = totals.kwh * vehicle.grid_co2_kg_per_kwh
    return schemas.ReviewSavings(
        fuel_type=vehicle.fuel_type,
        consumption_l_per_100km=vehicle.consumption_l_per_100km,
        fuel_price_per_l=vehicle.fuel_price_per_l,
        grid_co2_kg_per_kwh=vehicle.grid_co2_kg_per_kwh,
        configured=vehicle.configured,
        fuel_liters=liters,
        fuel_cost=fuel_cost,
        charging_cost=totals.cost,
        saved_eur=fuel_cost - totals.cost,
        co2_combustion_kg=co2_combustion,
        co2_ev_kg=co2_ev,
        saved_co2_kg=co2_combustion - co2_ev,
    )


# ---------------------------------------------------------------------------
# Zeitraum
# ---------------------------------------------------------------------------

def period_bounds(period: str, year: int, month: int | None) -> tuple[date, date]:
    if period == "year":
        return date(year, 1, 1), date(year, 12, 31)
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def previous_period(period: str, year: int, month: int | None) -> tuple[int, int | None]:
    if period == "year":
        return year - 1, None
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _day(s: models.ChargingSession) -> date:
    """Lokaler Kalendertag - start_time liegt naiv als lokale Zeit vor."""
    return s.start_time.date()


def _order(s: models.ChargingSession) -> tuple:
    """Chronologisch, bei gleicher Startzeit fest nach id - damit ein
    Gleichstand nicht zwischen zwei Abrufen springt."""
    return (s.start_time, s.id)


# ---------------------------------------------------------------------------
# Rechnung
# ---------------------------------------------------------------------------

class _Context:
    """Was fuer Haupt- und Vorperiode gleich ist und nur einmal geladen wird."""

    def __init__(self, db: Session, user: models.User, vehicle_id: str | None):
        self.vehicle_id = vehicle_id
        sessions = (
            db.query(models.ChargingSession)
            .filter(models.ChargingSession.user_id == user.id)
            .all()
        )
        if vehicle_id:
            sessions = [s for s in sessions if s.vehicle_id == vehicle_id]
        self.sessions = sorted(sessions, key=_order)

        self.vehicles = {
            v.id: v for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id).all()
        }
        self.providers = {
            p.id: p for p in db.query(models.Provider).filter(models.Provider.user_id == user.id).all()
        }
        self.locations = {
            loc.id: loc
            for loc in db.query(models.ChargingLocation)
            .filter(models.ChargingLocation.user_id == user.id)
            .all()
        }

        # Umlage ueber ALLE Vorgaenge des Nutzers (fees.py), Filter danach.
        allocation = load_allocation(db, user.id)
        self.shares = allocation.shares
        # Nicht umgelegte Perioden gehoeren zu keinem Fahrzeug (ein Abo gilt
        # fuers Konto) und fallen beim Fahrzeugfilter weg.
        self.unallocated = [] if vehicle_id else list(allocation.unallocated)

        # Verbrauch je Vorgang aus der vollstaendigen Fahrzeughistorie.
        self.consumption = {}
        by_vehicle = defaultdict(list)
        for s in self.sessions:
            by_vehicle[s.vehicle_id].append(s)
        for vid, vehicle_sessions in by_vehicle.items():
            vehicle = self.vehicles.get(vid)
            capacity = vehicle.battery_capacity_kwh if vehicle else None
            self.consumption.update(compute_vehicle_consumptions(vehicle_sessions, capacity))

    def cost(self, s: models.ChargingSession) -> float:
        """Kosten eines Vorgangs inkl. seines Grundgebuehr-Anteils."""
        return (s.price_total or 0) + self.shares.get(s.id, 0)

    def in_period(self, start: date, end: date) -> list[models.ChargingSession]:
        return [s for s in self.sessions if start <= _day(s) <= end]

    def unallocated_in(self, start: date, end: date) -> float:
        return sum(p.amount for p in self.unallocated if start <= p.start <= end)

    def place_of(self, s: models.ChargingSession) -> str | None:
        """Ladeort-Name > geocoded_place > Anbietername."""
        location = self.locations.get(s.location_id) if s.location_id else None
        if location and location.name:
            return location.name
        if s.geocoded_place:
            return s.geocoded_place
        provider = self.providers.get(s.provider_id) if s.provider_id else None
        return provider.name if provider else None


def _avg_consumption(ctx: _Context, sessions) -> float | None:
    """Km-gewichtet wie `monthly[].avg_consumption_kwh_per_100km` in
    routers/stats.py - eine Kurzstrecke mit unplausiblem Wert soll den
    Zeitraum nicht schief ziehen."""
    num = weight = 0.0
    for s in sessions:
        result = ctx.consumption.get(s.id)
        if result and result.value is not None and result.km:
            num += result.value * result.km
            weight += result.km
    return num / weight if weight else None


def _km(ctx: _Context, start: date, end: date) -> float:
    """Je Fahrzeug: letzter Stand im Zeitraum minus Anker (letzter Stand VOR
    dem Zeitraum, ohne den der erste im Zeitraum). Negativ (Zahlendreher)
    wird 0, damit ein Tippfehler nicht die Summe der uebrigen Fahrzeuge
    auffrisst."""
    total = 0.0
    by_vehicle = defaultdict(list)
    for s in ctx.sessions:
        if s.odometer_km is not None:
            by_vehicle[s.vehicle_id].append(s)
    for readings in by_vehicle.values():
        inside = [s for s in readings if start <= _day(s) <= end]
        if not inside:
            continue
        before = [s for s in readings if _day(s) < start]
        anchor = before[-1].odometer_km if before else inside[0].odometer_km
        total += max(inside[-1].odometer_km - anchor, 0)
    return total


def build_totals(ctx: _Context, start: date, end: date) -> schemas.ReviewTotals:
    sessions = ctx.in_period(start, end)
    kwh = sum(s.energy_kwh or 0 for s in sessions)
    cost = sum(ctx.cost(s) for s in sessions) + ctx.unallocated_in(start, end)
    km = _km(ctx, start, end)
    return schemas.ReviewTotals(
        sessions=len(sessions),
        kwh=kwh,
        cost=cost,
        km=km,
        avg_price_per_kwh=cost / kwh if kwh else None,
        price_per_100km=cost / km * 100 if km else None,
        avg_consumption_kwh_per_100km=_avg_consumption(ctx, sessions),
    )


def _session_record(ctx: _Context, s: models.ChargingSession, value: float, **extra):
    vehicle = ctx.vehicles.get(s.vehicle_id)
    return schemas.ReviewSessionRecord(
        session_id=s.id,
        start_time=s.start_time,
        value=value,
        place=ctx.place_of(s),
        vehicle_name=vehicle.name if vehicle else None,
        **extra,
    )


def _pick(candidates, *, highest: bool):
    """(Vorgang, Wert)-Paare -> Rekord. Bei Gleichstand gewinnt der fruehere
    Vorgang (start_time, dann id)."""
    if not candidates:
        return None
    sign = -1 if highest else 1
    return min(candidates, key=lambda c: (sign * c[1], _order(c[0])))


def _rank(groups: dict[str, list[models.ChargingSession]]) -> schemas.ReviewRankRecord | None:
    """Meiste Vorgaenge, bei Gleichstand mehr kWh, dann alphabetisch."""
    if not groups:
        return None
    rows = [(name, len(items), sum(s.energy_kwh or 0 for s in items)) for name, items in groups.items()]
    name, count, kwh = min(rows, key=lambda r: (-r[1], -r[2], r[0]))
    return schemas.ReviewRankRecord(name=name, sessions=count, kwh=kwh)


def build_records(
    ctx: _Context, start: date, end: date, monthly: list[schemas.ReviewMonthRow]
) -> schemas.ReviewRecords:
    sessions = ctx.in_period(start, end)
    records = schemas.ReviewRecords()

    # Je kWh: Saeulenpreis OHNE Grundgebuehr - der Anteil haengt davon ab,
    # wie oft man sonst noch beim Anbieter geladen hat, und waere kein
    # Merkmal des Vorgangs selbst.
    priced = [
        (s, s.price_total / s.energy_kwh)
        for s in sessions
        if (s.energy_kwh or 0) >= MIN_KWH_FOR_PRICE_RECORD and (s.price_total or 0) > 0
    ]
    if len(priced) >= MIN_CANDIDATES:
        s, v = _pick(priced, highest=False)
        records.cheapest_per_kwh = _session_record(ctx, s, v)
        s, v = _pick(priced, highest=True)
        records.most_expensive_per_kwh = _session_record(ctx, s, v)

    if best := _pick([(s, s.energy_kwh) for s in sessions if (s.energy_kwh or 0) > 0], highest=True):
        records.biggest_charge = _session_record(ctx, *best)

    temps = [(s, s.outside_temp_c) for s in sessions if s.outside_temp_c is not None]
    if len(temps) >= MIN_CANDIDATES:
        records.coldest = _session_record(ctx, *_pick(temps, highest=False))
        records.warmest = _session_record(ctx, *_pick(temps, highest=True))

    # Laengste Strecke: Abstand zum vorherigen Vorgang DESSELBEN Fahrzeugs
    # mit Kilometerstand - der darf vor dem Zeitraum liegen.
    distances = []
    previous: dict[str, models.ChargingSession] = {}
    for s in ctx.sessions:
        if s.odometer_km is None:
            continue
        prev = previous.get(s.vehicle_id)
        previous[s.vehicle_id] = s
        if prev is None or not (start <= _day(s) <= end):
            continue
        km = s.odometer_km - prev.odometer_km
        if km > 0:
            distances.append((s, km, prev))
    if distances:
        s, km, prev = min(distances, key=lambda c: (-c[1], _order(c[0])))
        records.longest_distance = _session_record(ctx, s, km, previous_start_time=prev.start_time)

    by_location: dict[str, list] = defaultdict(list)
    by_provider: dict[str, list] = defaultdict(list)
    for s in sessions:
        location = ctx.locations.get(s.location_id) if s.location_id else None
        name = (location.name if location and location.name else None) or s.geocoded_place
        if name:
            by_location[name].append(s)
        provider = ctx.providers.get(s.provider_id) if s.provider_id else None
        if provider:
            by_provider[provider.name].append(s)
    records.top_location = _rank(by_location)
    records.top_provider = _rank(by_provider)

    months = [(m.month, m.avg_consumption_kwh_per_100km) for m in monthly
              if m.avg_consumption_kwh_per_100km is not None]
    if len(months) >= MIN_CANDIDATES:
        low = min(months, key=lambda m: (m[1], m[0]))
        high = min(months, key=lambda m: (-m[1], m[0]))
        records.lowest_consumption_month = schemas.ReviewMonthRecord(month=low[0], value=low[1])
        records.highest_consumption_month = schemas.ReviewMonthRecord(month=high[0], value=high[1])
    return records


def build_split(ctx: _Context, start: date, end: date) -> schemas.ReviewSplit:
    from .routers.stats import build_by_place  # lokal: vermeidet einen Importzyklus

    sessions = ctx.in_period(start, end)
    ac = sum(s.energy_kwh or 0 for s in sessions if s.charging_type == models.ChargingType.AC)
    dc = sum(s.energy_kwh or 0 for s in sessions if s.charging_type == models.ChargingType.DC)
    typed = ac + dc
    unallocated = [p for p in ctx.unallocated if start <= p.start <= end]
    return schemas.ReviewSplit(
        ac_kwh=ac,
        dc_kwh=dc,
        ac_share_pct=ac / typed * 100 if typed else None,
        dc_share_pct=dc / typed * 100 if typed else None,
        by_place=build_by_place(sessions, ctx.providers, unallocated, ctx.cost),
    )


def build_monthly(ctx: _Context, year: int) -> list[schemas.ReviewMonthRow]:
    """Immer zwoelf Zeilen, auch leere - das Balkendiagramm braucht die
    Luecke, sonst ruecken die Monate zusammen."""
    rows = []
    for month in range(1, 13):
        start, end = period_bounds("month", year, month)
        sessions = ctx.in_period(start, end)
        rows.append(
            schemas.ReviewMonthRow(
                month=month,
                sessions=len(sessions),
                kwh=sum(s.energy_kwh or 0 for s in sessions),
                cost=sum(ctx.cost(s) for s in sessions) + ctx.unallocated_in(start, end),
                avg_consumption_kwh_per_100km=_avg_consumption(ctx, sessions),
            )
        )
    return rows


def build_review(
    db: Session,
    user: models.User,
    period: str,
    year: int,
    month: int | None = None,
    vehicle_id: str | None = None,
) -> schemas.ReviewOut:
    ctx = _Context(db, user, vehicle_id)
    start, end = period_bounds(period, year, month)
    totals = build_totals(ctx, start, end)

    prev_year, prev_month = previous_period(period, year, month)
    prev_start, prev_end = period_bounds(period, prev_year, prev_month)
    previous = build_totals(ctx, prev_start, prev_end)

    monthly = build_monthly(ctx, year) if period == "year" else []
    return schemas.ReviewOut(
        period=period,
        year=year,
        month=month if period == "month" else None,
        start_date=start,
        end_date=end,
        vehicle_id=vehicle_id,
        totals=totals,
        previous=previous if previous.sessions else None,
        savings=build_savings(totals, combustion_vehicle(user)),
        records=build_records(ctx, start, end, monthly),
        split=build_split(ctx, start, end),
        monthly=monthly,
    )

