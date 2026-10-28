"""Ø-Geschwindigkeit aus der Fahrtenstatistik der MySkoda-App.

Die App exportiert je FAHRT Beginn und Ende (lokale Zeit), Strecke,
Kilometerstand am Anfang und Ende und die Fahrzeit in Minuten. Daraus ergibt
sich fuer jeden Ladevorgang N genau die Angabe, die `drive_breakdown.py`
braucht: die Ø-Geschwindigkeit ueber alle Fahrten zwischen Vorgang N-1 und N,
also Summe km / Summe Fahrzeit - nicht das Mittel der einzelnen Ø-Werte, sonst
zaehlte eine 2-km-Fahrt so viel wie die Autobahnfahrt. Wunsch des Nutzers
27.10.2026; Teil des MySkoda-Abgleichs (`myskoda_import.py`), erkannt an den
Spalten, Vorschau und Uebernahme wie dort.

Zuordnung ueber den KILOMETERSTAND, nicht ueber die Zeit: Spritmonitor-Importe
tragen keine Uhrzeit, und an einem Ladetag liegen Fahrten vor und nach dem
Laden. Eine Fahrt zaehlt mit dem Anteil ihrer km, der zwischen den Staenden von
N-1 und N liegt - eine Fahrt mit Ladestopp unterwegs (DC an der Autobahn)
verteilt sich so anteilig auf beide Seiten, statt ganz herauszufallen. Fahrten
ohne Strecke (Rangieren, 0 km) zaehlen ueber die Zeit dazu; der Bordcomputer
rechnet sie in "seit Laden" ebenfalls mit.

Was gefuellt wird, nur wenn das Feld leer ist:

* `odometer_km` eines Vorgangs mit Uhrzeit: der Stand am Ende der letzten
  Fahrt vor dem Ladebeginn - vorausgesetzt, keine Fahrt laeuft ueber den
  Ladebeginn und die Datei reicht ueber ihn hinaus (sonst koennte danach noch
  gefahren worden sein, bevor geladen wurde).
* `avg_speed_kmh`, wenn die Fahrten mindestens `MIN_COVERAGE` der Strecke
  zwischen beiden Staenden abdecken (in der echten Datei fehlt bei einzelnen
  Fahrten der Start-km-Stand; die Strecke steht trotzdem da) und der Zeitraum
  ganz in der Datei liegt.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models

MIN_COVERAGE = 0.8
MAX_COVERAGE = 1.2
# Ein Ladebeginn darf kurz VOR dem Ende der letzten Fahrt liegen
# (Rueckdatierung der Poller, Uhrzeit von Hand auf die Minute gerundet).
PLUG_TOLERANCE = timedelta(minutes=10)
ODOMETER_TOLERANCE_KM = 1.0
MIN_SPEED_KMH = 5.0
MAX_SPEED_KMH = 250.0

COLUMN_ALIASES = {
    "start": ["start of trip", "beginn der fahrt", "fahrtbeginn", "start der fahrt"],
    "end": ["end of trip", "ende der fahrt", "fahrtende"],
    "distance_km": ["distance in km", "strecke in km", "entfernung in km", "distanz in km"],
    "odo_start": [
        "start odometer in km",
        "kilometerstand bei beginn in km",
        "kilometerstand am start in km",
        "km-stand start in km",
    ],
    "odo_end": [
        "end odometer in km",
        "kilometerstand am ende in km",
        "kilometerstand bei ende in km",
        "km-stand ende in km",
    ],
    "driving_min": ["driving time in minutes", "fahrzeit in minuten", "fahrtzeit in minuten"],
}
REQUIRED_COLUMNS = ("start", "end", "distance_km", "odo_end", "driving_min")


@dataclass
class Trip:
    start: datetime
    end: datetime
    distance_km: float
    odo_start: float | None
    odo_end: float
    driving_min: float

    @property
    def odo_from(self) -> float:
        """Start-km-Stand, notfalls aus Ende minus Strecke."""
        return self.odo_start if self.odo_start is not None else self.odo_end - self.distance_km


@dataclass
class FieldChange:
    field: str
    old: object
    new: object


@dataclass
class TripSummary:
    count: int
    km: float
    minutes: float


@dataclass
class Update:
    session: models.ChargingSession
    changes: list[FieldChange]
    trips: TripSummary | None

    @property
    def key(self) -> str:
        return f"trips:{self.session.id}"


@dataclass
class Hint:
    kind: str
    when: datetime
    data: dict = field(default_factory=dict)


@dataclass
class Plan:
    trips_total: int = 0
    unchanged: int = 0
    updates: list[Update] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _detect_columns(header: list[str]) -> dict[str, str | None]:
    normalized = {_normalize(h): h for h in header}
    return {
        key: next((normalized[a] for a in aliases if a in normalized), None)
        for key, aliases in COLUMN_ALIASES.items()
    }


def _reader(text: str) -> csv.DictReader:
    first = text.splitlines()[0] if text else ""
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    return csv.DictReader(io.StringIO(text), dialect=dialect)


def is_trip_export(content: bytes) -> bool:
    """Fahrtenstatistik statt Ladestatistik? Entschieden an den Spalten."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    columns = _detect_columns(_reader(text).fieldnames or [])
    return all(columns[key] for key in REQUIRED_COLUMNS)


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _to_local(raw: str) -> datetime:
    """Die App schreibt lokale Wandzeit ("27.10.2026 22:48") - wie die DB."""
    raw = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(raw).replace(tzinfo=None)


def parse_trips(content: bytes) -> list[Trip]:
    text = content.decode("utf-8-sig")
    reader = _reader(text)
    columns = _detect_columns(reader.fieldnames or [])
    trips = []
    for row in reader:
        try:
            start = _to_local(row[columns["start"]])
            end = _to_local(row[columns["end"]])
        except (ValueError, TypeError, KeyError):
            continue
        distance = _to_float(row.get(columns["distance_km"]))
        odo_end = _to_float(row.get(columns["odo_end"]))
        minutes = _to_float(row.get(columns["driving_min"]))
        if distance is None or odo_end is None or minutes is None or distance < 0 or minutes < 0:
            continue
        trips.append(
            Trip(
                start=start,
                end=max(end, start),
                distance_km=distance,
                odo_start=_to_float(row.get(columns["odo_start"])) if columns["odo_start"] else None,
                odo_end=odo_end,
                driving_min=minutes,
            )
        )
    trips.sort(key=lambda t: (t.start, t.odo_end))
    return trips


def _has_clock_time(when: datetime) -> bool:
    return (when.hour, when.minute, when.second) != (0, 0, 0)


def odometer_at(trips: list[Trip], when: datetime) -> float | None:
    """km-Stand beim Einstecken um `when`, oder None, wenn unsicher."""
    before = [t for t in trips if t.end <= when + PLUG_TOLERANCE]
    after = [t for t in trips if t.end > when + PLUG_TOLERANCE]
    if not before or not after:
        return None
    last = before[-1]
    # Eine Fahrt, die vor dem Ladebeginn beginnt und erst danach endet, laeuft
    # ueber ihn hinweg - der Stand beim Einstecken liegt dann irgendwo darin.
    if any(t.start < when - PLUG_TOLERANCE for t in after):
        return None
    return last.odo_end


def trips_between(trips: list[Trip], odo_from: float, odo_to: float) -> TripSummary:
    """Anteilige km und Fahrzeit aller Fahrten zwischen zwei km-Staenden."""
    count = 0
    km = 0.0
    minutes = 0.0
    for trip in trips:
        lo, hi = trip.odo_from, trip.odo_end
        if hi - lo <= 0 or trip.distance_km <= 0:
            if odo_from < lo < odo_to:
                count += 1
                minutes += trip.driving_min
            continue
        overlap = min(hi, odo_to) - max(lo, odo_from)
        if overlap <= 0:
            continue
        share = min(1.0, overlap / (hi - lo))
        count += 1
        km += trip.distance_km * share
        minutes += trip.driving_min * share
    return TripSummary(count=count, km=km, minutes=minutes)


def build_plan(db: Session, user: models.User, vehicle: models.Vehicle, trips: list[Trip]) -> Plan:
    plan = Plan(trips_total=len(trips))
    if not trips:
        return plan
    sessions = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.vehicle_id == vehicle.id,
            models.ChargingSession.user_id == user.id,
        )
        .order_by(models.ChargingSession.start_time, models.ChargingSession.id)
        .all()
    )
    file_odo_from = min(t.odo_from for t in trips) - ODOMETER_TOLERANCE_KM
    file_odo_to = max(t.odo_end for t in trips) + ODOMETER_TOLERANCE_KM

    # Erst die km-Staende, weil die Ø-Geschwindigkeit an ihnen haengt.
    odometer: dict[str, float | None] = {}
    changes: dict[str, list[FieldChange]] = {s.id: [] for s in sessions}
    for session in sessions:
        odometer[session.id] = session.odometer_km
        if session.odometer_km is None and _has_clock_time(session.start_time):
            found = odometer_at(trips, session.start_time)
            if found is not None:
                odometer[session.id] = found
                changes[session.id].append(FieldChange("odometer_km", None, int(round(found))))

    summaries: dict[str, TripSummary] = {}
    for previous, session in zip(sessions, sessions[1:]):
        odo_from, odo_to = odometer[previous.id], odometer[session.id]
        if odo_from is None or odo_to is None or odo_to <= odo_from:
            continue
        if odo_from < file_odo_from or odo_to > file_odo_to:
            continue
        summary = trips_between(trips, odo_from, odo_to)
        coverage = summary.km / (odo_to - odo_from)
        if summary.minutes <= 0 or not MIN_COVERAGE <= coverage <= MAX_COVERAGE:
            plan.hints.append(
                Hint(
                    "trip_gap",
                    session.start_time,
                    {"pct": round(coverage * 100), "km": round(odo_to - odo_from)},
                )
            )
            continue
        speed = summary.km / (summary.minutes / 60)
        if not MIN_SPEED_KMH <= speed <= MAX_SPEED_KMH:
            continue
        summaries[session.id] = summary
        if session.avg_speed_kmh is None:
            changes[session.id].append(FieldChange("avg_speed_kmh", None, round(speed, 1)))
        else:
            plan.unchanged += 1

    for session in sessions:
        if changes[session.id]:
            plan.updates.append(Update(session, changes[session.id], summaries.get(session.id)))
    return plan


NOTE_LABELS = {"odometer_km": "km-Stand", "avg_speed_kmh": "Ø km/h"}


def apply_plan(db: Session, plan: Plan, accepted: set[str]) -> int:
    updated = 0
    for update in plan.updates:
        if update.key not in accepted:
            continue
        session = update.session
        for change in update.changes:
            setattr(session, change.field, change.new)
        note = "MySkoda-Fahrten: " + ", ".join(
            f"{NOTE_LABELS[c.field]} {c.new:g}" for c in update.changes
        )
        if update.trips:
            note += f" ({update.trips.count} Fahrten, {update.trips.km:.0f} km)"
        session.notes = f"{session.notes}\n{note}" if session.notes else note
        updated += 1
    db.commit()
    return updated
