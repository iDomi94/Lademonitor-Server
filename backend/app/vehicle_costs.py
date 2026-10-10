"""Sonstige Fahrzeugkosten, Wertverlust und Leasing (ab v0.32.0).

Drei Kostenarten neben dem Laden, alle je Fahrzeug:

* **Sonstige Kosten** (models.VehicleCost): einzelne oder wiederkehrende
  Zahlungen. Ein Jahresbetrag zaehlt in dem Monat, in dem er faellig ist -
  wie auf dem Konto, nicht auf zwoelf Monate verteilt.
* **Leasing**: Monatsrate plus die Sonderzahlung, gleichmaessig auf die
  Laufzeit verteilt.
* **Wertverlust** beim Kauf: aus einer Restwertkurve, oder ein fester
  Monatsbetrag, wenn der Nutzer ihn ueberschreibt.

Wie die Grundgebuehren (fees.py) wird alles bei jedem Abruf frisch gerechnet
und nie gespeichert. Die Apps rechnen dasselbe lokal nach
(LocalVehicleCostCalculator in beiden Repos) - Regelaenderungen hier MUESSEN
dort mitgezogen werden; tests/test_vehicle_costs.py haelt die Faelle fest.

Restwertkurve: im ersten Jahr linear von 100 % auf 75 % des Neupreises,
danach je Jahr x0,87 - nach drei Jahren rund 57 %, nach fuenf rund 43 %, die
uebliche Groessenordnung fuer E-Autos. Die Kilometer gehen ueber ein
"wirksames Alter" ein: es zaehlt das Hoehere aus Kalenderalter und
km / 15.000. Ein Wagen mit 60.000 km nach zwei Jahren wird also wie ein
vierjaehriger bewertet. Der Kaufpreis wird nicht als Neupreis verstanden,
sondern als Wert beim Kauf: gerechnet wird mit dem VERHAELTNIS der Kurve
zwischen heute und dem Kaufzeitpunkt, sonst braeuchte es den Neupreis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .fees import add_months

CATEGORIES = (
    "insurance",
    "tax",
    "maintenance",
    "repair",
    "tires",
    "wallbox",
    "parking",
    "toll",
    "cleaning",
    "other",
)

# Jahresfahrleistung, die das "wirksame Alter" einem Kalenderjahr gleichsetzt.
KM_PER_YEAR_REFERENCE = 15000.0
FIRST_YEAR_RESIDUAL = 0.75
YEARLY_RESIDUAL_FACTOR = 0.87
DAYS_PER_YEAR = 365.25
# Zeitraum, aus dem die aktuelle Fahrleistung geschaetzt wird.
MILEAGE_WINDOW_DAYS = 365
MIN_MILEAGE_SPAN_DAYS = 30


@dataclass(frozen=True)
class CostEntry:
    """Eine einzelne Zahlung bzw. ein Monatsbetrag, fertig zum Aufsummieren."""

    vehicle_id: str
    day: date
    amount: float
    # Kategorie aus CATEGORIES, bzw. "depreciation" / "leasing".
    category: str


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


# ---------- Sonstige Kosten ----------


def occurrences(cost, until: date) -> list[date]:
    """Zahlungstermine eines Kosteneintrags bis `until` (einschliesslich).

    `once` ist genau eine Zahlung am Startdatum. Wiederkehrende laufen vom
    urspruenglichen Ankertag aus (31.01. -> 28.02. -> 31.03., wie bei den
    Grundgebuehren) bis zum Enddatum oder bis heute.
    """
    start = _as_date(cost.start_date)
    if start is None or start > until:
        return []
    if cost.interval not in ("monthly", "yearly"):
        return [start]
    end = _as_date(cost.end_date)
    step = 12 if cost.interval == "yearly" else 1
    result = []
    k = 0
    while True:
        day = add_months(start, k * step)
        if day > until or (end is not None and day > end):
            break
        result.append(day)
        k += 1
    return result


def cost_entries(costs, until: date) -> list[CostEntry]:
    return [
        CostEntry(c.vehicle_id, day, c.amount, c.category if c.category in CATEGORIES else "other")
        for c in costs
        for day in occurrences(c, until)
    ]


# ---------- Wertverlust ----------


def residual_fraction(age_years: float) -> float:
    """Restwert in Anteilen des Neupreises nach `age_years` wirksamen Jahren."""
    if age_years <= 0:
        return 1.0
    if age_years <= 1:
        return 1.0 - (1.0 - FIRST_YEAR_RESIDUAL) * age_years
    return FIRST_YEAR_RESIDUAL * YEARLY_RESIDUAL_FACTOR ** (age_years - 1)


class Depreciation:
    """Wertverlauf eines gekauften Fahrzeugs.

    `odometer` sind (Datum, km-Stand)-Paare aus den Ladevorgaengen. Zwischen
    zwei Staenden wird linear interpoliert, nach dem letzten mit der
    aktuellen Fahrleistung fortgeschrieben - sonst stuende der Wertverlust
    still, sobald ein paar Tage nicht geladen wurde.
    """

    def __init__(self, vehicle, odometer: list[tuple[date, int]], today: date):
        self.price = vehicle.purchase_price or 0.0
        self.purchase_day = _as_date(vehicle.purchase_date)
        self.registration_day = _as_date(vehicle.first_registration) or self.purchase_day
        self.today = today
        points: list[tuple[date, float]] = []
        if self.purchase_day and vehicle.purchase_odometer_km is not None:
            points.append((self.purchase_day, float(vehicle.purchase_odometer_km)))
        for day, km in sorted(odometer):
            if self.purchase_day and day < self.purchase_day:
                continue
            # Ein rueckwaerts laufender Stand (Zahlendreher) wird ignoriert.
            if points and km < points[-1][1]:
                continue
            points.append((day, float(km)))
        self.points = points
        self.km_per_year = self._km_per_year()

    @property
    def valid(self) -> bool:
        return self.price > 0 and self.purchase_day is not None

    def _km_per_year(self) -> float:
        if len(self.points) < 2:
            return KM_PER_YEAR_REFERENCE
        last_day, last_km = self.points[-1]
        window_start = last_day - timedelta(days=MILEAGE_WINDOW_DAYS)
        recent = [p for p in self.points if p[0] >= window_start]
        first_day, first_km = recent[0] if len(recent) >= 2 else self.points[0]
        span = (last_day - first_day).days
        if span < MIN_MILEAGE_SPAN_DAYS:
            first_day, first_km = self.points[0]
            span = (last_day - first_day).days
        if span < MIN_MILEAGE_SPAN_DAYS:
            return KM_PER_YEAR_REFERENCE
        return max(last_km - first_km, 0.0) / span * DAYS_PER_YEAR

    def km_at(self, day: date) -> float:
        if not self.points:
            # Ohne jeden km-Stand zaehlt allein das Kalenderalter.
            return 0.0
        if day <= self.points[0][0]:
            return self.points[0][1]
        for (d0, k0), (d1, k1) in zip(self.points, self.points[1:]):
            if d0 <= day <= d1:
                span = (d1 - d0).days
                return k0 if span == 0 else k0 + (k1 - k0) * (day - d0).days / span
        last_day, last_km = self.points[-1]
        return last_km + self.km_per_year * (day - last_day).days / DAYS_PER_YEAR

    def effective_age(self, day: date) -> float:
        calendar = max((day - self.registration_day).days, 0) / DAYS_PER_YEAR
        return max(calendar, self.km_at(day) / KM_PER_YEAR_REFERENCE)

    def value_at(self, day: date) -> float:
        base = residual_fraction(self.effective_age(self.purchase_day))
        return self.price * residual_fraction(self.effective_age(day)) / base

    def loss_between(self, start: date, end: date) -> float:
        return max(self.value_at(start) - self.value_at(end), 0.0)

    def suggested_monthly(self) -> float:
        return self.loss_between(self.today, add_months(self.today, 1))


def _odometer_points(sessions) -> list[tuple[date, int]]:
    return [(s.start_time.date(), s.odometer_km) for s in sessions if s.odometer_km is not None]


def ownership_entries(vehicle, sessions, until: date) -> list[CostEntry]:
    """Monatsbetraege fuer Wertverlust bzw. Leasing bis `until`.

    Eine Periode laeuft vom Kauf- bzw. Leasingbeginn aus je einen Monat und
    zaehlt voll, sobald sie begonnen hat - wie bei den Grundgebuehren.
    """
    if vehicle.ownership == "leasing":
        return _leasing_entries(vehicle, until)
    if vehicle.ownership != "purchase":
        return []
    start = _as_date(vehicle.purchase_date)
    if start is None or start > until or not (vehicle.purchase_price or vehicle.depreciation_monthly_override):
        return []
    override = vehicle.depreciation_monthly_override
    curve = Depreciation(vehicle, _odometer_points(sessions), until) if override is None else None
    if curve is not None and not curve.valid:
        return []
    result = []
    k = 0
    while True:
        p_start = add_months(start, k)
        if p_start > until:
            break
        amount = override if override is not None else curve.loss_between(p_start, add_months(start, k + 1))
        result.append(CostEntry(vehicle.id, p_start, round(amount, 2), "depreciation"))
        k += 1
    return result


def leasing_term_months(vehicle) -> int | None:
    start = _as_date(vehicle.leasing_start)
    end = _as_date(vehicle.leasing_end)
    if start is None or end is None or end < start:
        return None
    k = 0
    while add_months(start, k) <= end:
        k += 1
    return k


def _leasing_entries(vehicle, until: date) -> list[CostEntry]:
    start = _as_date(vehicle.leasing_start)
    if start is None or start > until:
        return []
    end = _as_date(vehicle.leasing_end)
    rate = vehicle.leasing_rate_monthly or 0.0
    term = leasing_term_months(vehicle)
    down = vehicle.leasing_down_payment or 0.0
    result = []
    k = 0
    while True:
        p_start = add_months(start, k)
        if p_start > until or (end is not None and p_start > end):
            break
        # Ohne Enddatum laesst sich die Sonderzahlung nicht verteilen - dann
        # zaehlt sie im ersten Monat.
        share = down / term if term else (down if k == 0 else 0.0)
        result.append(CostEntry(vehicle.id, p_start, round(rate + share, 2), "leasing"))
        k += 1
    return result


def ownership_info(vehicle, sessions, today: date) -> dict:
    """Kennzahlen fuer die Anzeige am Fahrzeug (VehicleOut)."""
    info = {"estimated_value": None, "depreciation_suggested_monthly": None, "ownership_monthly": None}
    if vehicle.ownership == "leasing":
        term = leasing_term_months(vehicle)
        down = vehicle.leasing_down_payment or 0.0
        if vehicle.leasing_rate_monthly is not None or down:
            info["ownership_monthly"] = round(
                (vehicle.leasing_rate_monthly or 0.0) + (down / term if term else 0.0), 2
            )
    elif vehicle.ownership == "purchase":
        curve = Depreciation(vehicle, _odometer_points(sessions), today)
        if curve.valid:
            info["estimated_value"] = round(curve.value_at(today), 2)
            info["depreciation_suggested_monthly"] = round(curve.suggested_monthly(), 2)
        override = vehicle.depreciation_monthly_override
        info["ownership_monthly"] = override if override is not None else info["depreciation_suggested_monthly"]
    return info


def has_cost_data(vehicle, costs) -> bool:
    """Ob es fuer dieses Fahrzeug ueberhaupt etwas jenseits des Ladens gibt -
    sonst blendet das Dashboard den Abschnitt aus."""
    return vehicle.ownership in ("purchase", "leasing") or any(c.vehicle_id == vehicle.id for c in costs)
