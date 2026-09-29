"""Akku-Gesundheit und Ladeverluste aus den gemessenen Ladevorgaengen.

Beide Fragen haengen an derselben Kennzahl: wie viele kWh ein Ladevorgang pro
Prozentpunkt SoC gebraucht hat. Hochgerechnet auf 100 % ergibt das die
**scheinbare Kapazitaet** - das, was man an der Saeule oder Wallbox bezahlen
muesste, um den Akku einmal ganz zu fuellen.

    scheinbare_kapazitaet = energy_kwh / (soc_end - soc_start) * 100

Darin stecken zwei Dinge, die sich aus einer einzelnen Zahl nicht trennen
lassen: die nutzbare Kapazitaet des Akkus UND die Verluste auf dem Weg vom
Zaehler in die Zellen (Ladegeraet, Kabel, Akkuheizung). Deshalb zwei getrennte
Auswertungen, die jeweils nur das behaupten, was sie auch zeigen koennen:

**Ladeverluste** (`build_losses`) vergleichen die scheinbare Kapazitaet mit der
hinterlegten Nennkapazitaet (`Vehicle.battery_capacity_kwh`). Das Ergebnis ist
der Mehrbedarf gegenueber einem verlustfreien Laden eines neuen Akkus - bei
einem gealterten Akku faellt er also etwas zu KLEIN aus, nie zu gross. Belastbar
ist vor allem der Unterschied zwischen AC und DC bzw. zwischen Anbietern, weil
dort derselbe Akku verglichen wird.

**Akku-Gesundheit** (`build_health`) verfolgt die scheinbare Kapazitaet ueber
die Zeit, getrennt nach Lade-Art (AC hat groessere Verluste als DC, gemischt
wuerde ein Wechsel der Gewohnheiten wie Alterung aussehen). Jeder Wert wird auf
den Anfang seiner eigenen Lade-Art bezogen; ausgewiesen wird ein Index "100 % =
wie zu Beginn der Aufzeichnung". Das ist bewusst KEIN absoluter SoH: dafuer
muesste man die Verluste kennen, und die stecken in derselben Zahl. Nimmt die
Kapazitaet ab, sinkt der Index - das ist die Frage, die sich damit beantworten
laesst.

Was herausfaellt (und gezaehlt wird, statt still zu verschwinden):

* **geschaetzte Energie** (`energy_is_estimated`): die ist selbst aus
  SoC-Hub x Nennkapazitaet gerechnet, ergaebe also immer exakt die
  Nennkapazitaet. Automatisch erkannte Vorgaenge (HA-Push, MySkoda-Poller)
  fallen damit fast alle heraus - die Auswertung lebt von Vorgaengen mit einer
  echten kWh-Angabe (Rechnung, Wallbox, Spritmonitor-Import).
* **kleiner SoC-Hub** (< `MIN_SOC_DELTA`): der SoC ist ganzzahlig, bei 5 %
  Hub macht ein einziger Prozentpunkt schon 20 % Fehler.
* **Unplausibles** (ausserhalb `PLAUSIBLE_RANGE` um die Nennkapazitaet bzw.
  ohne Nennkapazitaet um den Median): Tippfehler, eine Ladung, bei der das
  Fahrzeug nebenher vorklimatisiert hat, ein vertauschter SoC.

**Messort** (`energy_meter`, ab v0.29.0): wurden die kWh im FAHRZEUG
abgelesen statt an der Ladesaeule, fehlen die Ladeverluste bereits in der
Zahl. Solche Vorgaenge gehen nicht in die Verlustrechnung ein (gezaehlt als
`vehicle_measured`) - sie wuerden den Verlust nach unten ziehen, bis hin zu
einem scheinbar verlustfreien Laden. Fuer den Akku-Index taugen sie dagegen
sogar besser; dort bekommen sie eine eigene Gruppe mit eigenem Anfangswert
("AC/vehicle"), aus demselben Grund, aus dem AC und DC getrennt sind.
Ermittelt wird der Messort ueber `effective_meter()`: Ausnahme am Vorgang,
sonst die Einstellung des Anbieters, sonst Ladesaeule.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from . import models

# Mindest-SoC-Hub eines Vorgangs in Prozentpunkten. Der SoC kommt ganzzahlig -
# bei 20 Punkten Hub liegt der Rundungsfehler bei hoechstens 5 %, darunter
# geht die Aussage im Rauschen unter.
MIN_SOC_DELTA = 20

# Zulaessiger Bereich der scheinbaren Kapazitaet relativ zur Nennkapazitaet
# (ohne Nennkapazitaet: relativ zum Median aller Vorgaenge des Fahrzeugs).
# Nach unten grosszuegig, weil ein gealterter Akku echt weniger aufnimmt; nach
# oben sind 40 % Mehrbedarf mehr als jede reale Ladung verliert.
PLAUSIBLE_RANGE = (0.70, 1.40)

# Anfangswert je Lade-Art: Median der ersten so vielen Vorgaenge. Einer allein
# waere Zufall, zu viele verwischen genau die Alterung, die gemessen werden
# soll.
BASELINE_SESSIONS = 5
# Ohne so viele Vorgaenge einer Lade-Art wird sie fuer den Verlauf nicht
# verwendet - es gaebe keinen belastbaren Anfangswert.
MIN_SESSIONS_PER_TYPE = 3
# Ein Quartal wird erst ab so vielen Vorgaengen ausgewiesen.
MIN_SESSIONS_PER_PERIOD = 2
# Die Trendangabe "% pro Jahr" erst ab so vielen Punkten ueber so viele Tage -
# drei Wochen Daten ergeben rechnerisch auch eine Steigung, nur keine, die
# etwas ueber Alterung sagt.
MIN_POINTS_FOR_TREND = 8
MIN_DAYS_FOR_TREND = 180

UNKNOWN_TYPE = "unknown"

METER_CHARGER = "charger"
METER_VEHICLE = "vehicle"
ENERGY_METERS = (METER_CHARGER, METER_VEHICLE)


def effective_meter(
    session: models.ChargingSession, provider_meters: dict[str, str] | None = None
) -> str:
    """Wo die kWh dieses Vorgangs abgelesen wurden: Ausnahme am Vorgang, sonst
    der Standard seines Anbieters, sonst die Ladesaeule (so wurde vor v0.29.0
    alles gerechnet)."""
    if session.energy_meter in ENERGY_METERS:
        return session.energy_meter
    if session.provider_id and provider_meters:
        meter = provider_meters.get(session.provider_id)
        if meter in ENERGY_METERS:
            return meter
    if session.provider_id and session.provider is not None:
        if session.provider.energy_meter in ENERGY_METERS:
            return session.provider.energy_meter
    return METER_CHARGER


@dataclass
class BatteryPoint:
    session_id: str
    start_time: datetime
    charging_type: str
    provider_id: str | None
    soc_delta: int
    energy_kwh: float
    apparent_capacity_kwh: float
    # (scheinbar / nenn - 1) * 100, None ohne Nennkapazitaet und bei im
    # Fahrzeug gemessenen kWh (dort steckt kein Verlust in der Zahl)
    loss_pct: float | None
    energy_meter: str = METER_CHARGER

    @property
    def health_key(self) -> str:
        """Gruppe fuer den Akku-Index: Lade-Art, bei Fahrzeugmessung mit
        eigenem Anfangswert."""
        if self.energy_meter == METER_VEHICLE:
            return f"{self.charging_type}/{METER_VEHICLE}"
        return self.charging_type


@dataclass
class Exclusions:
    estimated_energy: int = 0
    missing_values: int = 0
    small_soc_delta: int = 0
    implausible: int = 0
    # Nicht AUSGESCHLOSSEN, nur nicht in der Verlustrechnung: im Fahrzeug
    # gemessen, zaehlt aber im Akku-Index mit.
    vehicle_measured: int = 0


@dataclass
class LossGroup:
    key: str
    session_count: int
    energy_kwh: float
    apparent_capacity_kwh: float
    loss_pct: float | None


@dataclass
class HealthPeriod:
    period: str  # "2026-Q3"
    index_pct: float
    session_count: int


@dataclass
class Health:
    periods: list[HealthPeriod]
    latest_index_pct: float | None
    trend_pct_per_year: float | None
    # Lade-Arten, die in den Verlauf eingegangen sind, samt ihrem Anfangswert
    baselines: dict[str, float]


def _type_of(session: models.ChargingSession) -> str:
    return session.charging_type.value if session.charging_type else UNKNOWN_TYPE


def collect_points(
    sessions: list[models.ChargingSession],
    nominal_kwh: float | None,
    provider_meters: dict[str, str] | None = None,
) -> tuple[list[BatteryPoint], Exclusions]:
    """Alle verwertbaren Vorgaenge EINES Fahrzeugs, chronologisch."""
    excluded = Exclusions()
    candidates: list[BatteryPoint] = []
    for s in sorted(sessions, key=lambda x: (x.start_time, x.id or "")):
        if s.energy_kwh is None or s.soc_start is None or s.soc_end is None:
            excluded.missing_values += 1
            continue
        if s.energy_is_estimated:
            excluded.estimated_energy += 1
            continue
        delta = s.soc_end - s.soc_start
        if delta < MIN_SOC_DELTA or s.energy_kwh <= 0:
            excluded.small_soc_delta += 1
            continue
        apparent = s.energy_kwh / delta * 100
        meter = effective_meter(s, provider_meters)
        candidates.append(
            BatteryPoint(
                session_id=s.id,
                start_time=s.start_time,
                charging_type=_type_of(s),
                provider_id=s.provider_id,
                soc_delta=delta,
                energy_kwh=s.energy_kwh,
                apparent_capacity_kwh=round(apparent, 2),
                loss_pct=(
                    round((apparent / nominal_kwh - 1) * 100, 1)
                    if nominal_kwh and meter == METER_CHARGER
                    else None
                ),
                energy_meter=meter,
            )
        )

    reference = nominal_kwh or (
        median(p.apparent_capacity_kwh for p in candidates) if candidates else None
    )
    low, high = PLAUSIBLE_RANGE
    points = []
    for p in candidates:
        if reference and not (low <= p.apparent_capacity_kwh / reference <= high):
            excluded.implausible += 1
            continue
        if p.energy_meter == METER_VEHICLE:
            excluded.vehicle_measured += 1
        points.append(p)
    return points, excluded


def _group(points: list[BatteryPoint], key, nominal_kwh: float | None) -> list[LossGroup]:
    groups: dict[str, list[BatteryPoint]] = defaultdict(list)
    for p in points:
        groups[key(p)].append(p)
    result = []
    for k, items in groups.items():
        energy = sum(p.energy_kwh for p in items)
        soc = sum(p.soc_delta for p in items)
        # Nach Energie gewichtet: Summe der kWh durch Summe der SoC-Punkte,
        # nicht das Mittel der Einzelquoten - sonst zaehlt ein 20-%-Zwischenstopp
        # so viel wie eine Ladung von 10 auf 90 %.
        apparent = energy / soc * 100
        result.append(
            LossGroup(
                key=k,
                session_count=len(items),
                energy_kwh=round(energy, 1),
                apparent_capacity_kwh=round(apparent, 2),
                loss_pct=round((apparent / nominal_kwh - 1) * 100, 1) if nominal_kwh else None,
            )
        )
    return sorted(result, key=lambda g: (-g.energy_kwh, g.key))


def build_losses(points: list[BatteryPoint], nominal_kwh: float | None):
    """(nach Lade-Art, nach Anbieter-ID) - jeweils absteigend nach Energie.

    Nur an der Ladesaeule gemessene Vorgaenge: bei im Fahrzeug abgelesenen
    kWh sind die Verluste schon heraus, sie wuerden den Wert nur verduennen."""
    points = [p for p in points if p.energy_meter == METER_CHARGER]
    return (
        _group(points, lambda p: p.charging_type, nominal_kwh),
        _group(points, lambda p: p.provider_id or "", nominal_kwh),
    )


def _quarter(ts: datetime) -> str:
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def build_health(points: list[BatteryPoint]) -> Health:
    by_type: dict[str, list[BatteryPoint]] = defaultdict(list)
    for p in points:
        by_type[p.health_key].append(p)

    baselines = {
        t: median(p.apparent_capacity_kwh for p in items[:BASELINE_SESSIONS])
        for t, items in by_type.items()
        if len(items) >= MIN_SESSIONS_PER_TYPE
    }

    normalized = sorted(
        (
            (p.start_time, p.apparent_capacity_kwh / baselines[p.health_key] * 100)
            for p in points
            if p.health_key in baselines
        ),
        key=lambda x: x[0],
    )

    per_period: dict[str, list[float]] = defaultdict(list)
    for ts, value in normalized:
        per_period[_quarter(ts)].append(value)
    periods = [
        HealthPeriod(period=k, index_pct=round(median(v), 1), session_count=len(v))
        for k, v in sorted(per_period.items())
        if len(v) >= MIN_SESSIONS_PER_PERIOD
    ]

    trend = None
    if len(normalized) >= MIN_POINTS_FOR_TREND:
        first = normalized[0][0]
        xs = [(ts - first).total_seconds() / 86400 / 365.25 for ts, _ in normalized]
        if xs[-1] * 365.25 >= MIN_DAYS_FOR_TREND:
            ys = [v for _, v in normalized]
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                trend = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, 1)

    return Health(
        periods=periods,
        latest_index_pct=periods[-1].index_pct if periods else None,
        trend_pct_per_year=trend,
        baselines={t: round(v, 2) for t, v in baselines.items()},
    )
