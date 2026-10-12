"""Abgleich mit dem Ladeverlauf aus der MySkoda-App (CSV-Export "Ladestatistik").

Die App exportiert je Ladung Beginn und Ende (UTC), Start- und End-SoC und die
Energie als ganze kWh - aber keinen Ort, keinen Preis, keinen Kilometerstand
und keine Lade-Art. Als NEUE Vorgaenge taugen die Zeilen deshalb wenig (ohne
km-Stand kein Verbrauch), ihr Wert liegt im ABGLEICH: der SoC kommt direkt aus
dem Fahrzeug und ist genauer als alles, was Spritmonitor-Importe oder
HA-Stundenmittel hergeben. Entscheidung des Nutzers 12.10.2026, Plan:
`ideen/plan-myskoda-abgleich.md`.

Ablauf: `parse_export()` liest die Datei, `build_plan()` ordnet die Ladungen
den vorhandenen Vorgaengen zu und liefert je Vorgang die vorgeschlagenen
Aenderungen. `apply_plan()` schreibt nur die angehakten. Vorschau und
Uebernahme rechnen denselben Plan aus derselben Datei - der Abgleich ist
deterministisch, der Server muss sich zwischen beiden Schritten nichts merken.

Zuordnung (der Kern):

* Vorgaenge MIT Endzeit (automatisch erfasst, von Hand mit Ende) passen zu
  genau einer Ladung, deren Zeitraum sich mit ihrem ueberschneidet oder deren
  Beginn hoechstens `MAX_START_GAP` entfernt liegt. Die drei Stunden decken
  auch Eintraege ab, die mit UTC statt Ortszeit gespeichert wurden.
* Vorgaenge OHNE Endzeit (vor allem Spritmonitor-Importe, oft ohne Uhrzeit)
  passen zu einer zusammenhaengenden Folge von bis zu `MAX_GROUP` Ladungen,
  die um ihren Ladetag herum endet. Spritmonitor-Eintraege fassen nicht selten
  mehrere Ladungen zusammen ("Sammeleintraege", z.B. Freitag + Sonntag an der
  eigenen Wallbox als ein Eintrag); die Summe der kWh muss dann zum Eintrag
  passen, der End-SoC zur letzten Ladung.
* Alle zulaessigen Paare bekommen eine Bewertung, vergeben wird gierig vom
  besten Paar an - so gewinnt bei zwei Eintraegen am selben Tag der, zu dem
  Uhrzeit und Lade-Art passen, statt der, der zufaellig zuerst kommt.

Was sich nicht eindeutig zuordnen laesst, wird nicht geaendert, sondern als
Hinweis ausgewiesen. Neu angelegt werden nur Ladungen, in deren Naehe GAR KEIN
Vorgang liegt (`NEW_SESSION_CLEARANCE_DAYS`) - eine Ladung neben einem
Eintrag, der nur nicht passte, ist mit hoher Wahrscheinlichkeit derselbe
Vorgang mit anderen Zahlen, und ein Duplikat waere schlimmer als eine Luecke.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models

MAX_START_GAP = timedelta(hours=3)
MAX_GROUP = 4
# Ladetag eines Eintrags ohne Endzeit gegen das Ende der letzten Ladung: der
# Eintrag darf bis zu drei Tage spaeter datiert sein (Spritmonitor-Eintraege
# entstehen oft erst beim naechsten Oeffnen der App).
MAX_DAYS_AFTER_END = 3
SOC_TOLERANCE = 3
# Abweichung, ab der ein Wert als falsch gilt und korrigiert wird. Ein Punkt
# Unterschied ist Rundung (die App rundet anders als der Poller).
SOC_CORRECTION_THRESHOLD = 2
TIME_CORRECTION_THRESHOLD = timedelta(minutes=30)
# Die App liefert ganze kWh und misst naeher am Akku als ein Wallbox-Zaehler.
KWH_TOLERANCE_ABS = 2.5
KWH_TOLERANCE_REL = 0.06
# Mittlere Leistung ueber die tatsaechliche Ladezeit, ab der eine Ladung als DC
# gilt. Die MySkoda-Fahrzeuge laden AC mit hoechstens 11 kW.
DC_POWER_THRESHOLD_KW = 12.0
NEW_SESSION_CLEARANCE_DAYS = 3
EXTERNAL_ID_PREFIX = "myskoda-app-"

COLUMN_ALIASES = {
    "id": ["session id", "sitzungs-id", "id"],
    "start": ["begonnen am", "started at", "start time", "startzeit", "beginn"],
    "end": ["beendet am", "ended at", "end time", "endzeit", "ende"],
    "active_s": ["tatsächliche ladezeit (s)", "actual charging time (s)"],
    "duration_s": ["ladezeit (s)", "charging time (s)"],
    "energy_kwh": ["gesamtenergie (kwh)", "total energy (kwh)"],
    "soc_start": ["start soc (%)", "start-soc (%)", "soc start (%)"],
    "soc_end": ["ende soc (%)", "end soc (%)", "end-soc (%)", "soc ende (%)"],
}
REQUIRED_COLUMNS = ("start", "end", "soc_start", "soc_end")


class ExportFormatError(ValueError):
    """Die Datei sieht nicht wie ein MySkoda-Export aus."""

    def __init__(self, missing: list[str], header: list[str]):
        super().__init__(f"Spalten fehlen: {', '.join(missing)}")
        self.missing = missing
        self.header = header


@dataclass
class Charge:
    """Eine Zeile des Exports, Zeiten in lokaler Wandzeit (naiv, wie die DB)."""

    charge_id: str
    start: datetime
    end: datetime
    soc_start: int
    soc_end: int
    energy_kwh: float | None
    active_s: float | None

    @property
    def is_real(self) -> bool:
        """Hat der SoC zugenommen? Ladungen ohne Anstieg (Kabel steckte, Akku
        war schon voll) zaehlen nur innerhalb eines Sammeleintrags mit."""
        return self.soc_end > self.soc_start


@dataclass
class FieldChange:
    field: str
    old: object
    new: object


@dataclass
class Update:
    session: models.ChargingSession
    charges: list[Charge]
    changes: list[FieldChange]

    @property
    def key(self) -> str:
        return f"update:{self.session.id}"


@dataclass
class NewSession:
    charge: Charge
    charging_type: models.ChargingType | None

    @property
    def key(self) -> str:
        return f"new:{self.charge.charge_id}"


@dataclass
class Hint:
    kind: str
    when: datetime
    data: dict = field(default_factory=dict)


@dataclass
class Plan:
    charges_total: int = 0
    charges_ignored: int = 0
    unchanged: int = 0
    already_imported: int = 0
    updates: list[Update] = field(default_factory=list)
    new_sessions: list[NewSession] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)


# --- Einlesen ---------------------------------------------------------------


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _detect_columns(header: list[str]) -> dict[str, str | None]:
    normalized = {_normalize(h): h for h in header}
    detected: dict[str, str | None] = {}
    for key, aliases in COLUMN_ALIASES.items():
        detected[key] = next((normalized[a] for a in aliases if a in normalized), None)
    return detected


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
    """ISO-Zeitstempel -> lokale Wandzeit ohne Zone (wie `start_time` in der DB).

    Der Export liefert UTC mit "Z". Ohne Zonenangabe wird der Wert als bereits
    lokal genommen - geraten wird nichts.
    """
    raw = raw.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def parse_export(content: bytes) -> list[Charge]:
    text = content.decode("utf-8-sig")
    lines = text.splitlines()
    if not lines:
        raise ExportFormatError(list(REQUIRED_COLUMNS), [])
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = reader.fieldnames or []
    columns = _detect_columns(header)
    missing = [key for key in REQUIRED_COLUMNS if not columns[key]]
    if missing:
        raise ExportFormatError(missing, header)

    charges = []
    for index, row in enumerate(reader):
        try:
            start = _to_local(row[columns["start"]])
            end = _to_local(row[columns["end"]])
        except (ValueError, TypeError):
            continue
        soc_start = _to_float(row.get(columns["soc_start"]))
        soc_end = _to_float(row.get(columns["soc_end"]))
        if soc_start is None or soc_end is None:
            continue
        charge_id = (row.get(columns["id"]) or "").strip() if columns["id"] else ""
        charges.append(
            Charge(
                charge_id=charge_id or f"{start.isoformat()}-{index}",
                start=start,
                end=max(end, start),
                soc_start=int(round(soc_start)),
                soc_end=int(round(soc_end)),
                energy_kwh=_to_float(row.get(columns["energy_kwh"])) if columns["energy_kwh"] else None,
                active_s=_to_float(row.get(columns["active_s"])) if columns["active_s"] else None,
            )
        )
    charges.sort(key=lambda c: (c.start, c.charge_id))
    return charges


# --- Zuordnung --------------------------------------------------------------


def _derived_type(charges: list[Charge]) -> models.ChargingType | None:
    kwh = sum(c.energy_kwh or 0 for c in charges)
    seconds = sum(c.active_s or 0 for c in charges)
    if kwh <= 0 or seconds <= 0:
        return None
    power_kw = kwh / (seconds / 3600)
    return models.ChargingType.DC if power_kw > DC_POWER_THRESHOLD_KW else models.ChargingType.AC


def _kwh_ok(expected: float, actual: float) -> bool:
    return abs(expected - actual) <= max(KWH_TOLERANCE_ABS, KWH_TOLERANCE_REL * expected)


def _has_clock_time(when: datetime) -> bool:
    return (when.hour, when.minute, when.second) != (0, 0, 0)


def _score_timed(session: models.ChargingSession, charge: Charge) -> float | None:
    """Vorgang mit Endzeit gegen genau eine Ladung."""
    if not charge.is_real:
        return None
    overlaps = charge.start <= session.end_time and session.start_time <= charge.end
    gap = abs(charge.start - session.start_time)
    if not overlaps and gap > MAX_START_GAP:
        return None
    score = gap.total_seconds() / 3600 / 3
    if session.soc_end is not None:
        score += abs(session.soc_end - charge.soc_end) / 20
    if (
        session.energy_kwh is not None
        and not session.energy_is_estimated
        and charge.energy_kwh is not None
        and not _kwh_ok(session.energy_kwh, charge.energy_kwh)
    ):
        return None
    return score


def _score_group(session: models.ChargingSession, group: list[Charge]) -> float | None:
    """Vorgang ohne Endzeit gegen eine zusammenhaengende Folge von Ladungen."""
    last = group[-1]
    if not last.is_real:
        return None
    day = session.start_time.date()
    if last.start.date() > day or (day - last.end.date()).days > MAX_DAYS_AFTER_END:
        return None
    same_day = last.start.date() <= day <= last.end.date()

    soc_gap = abs(session.soc_end - last.soc_end) if session.soc_end is not None else 0
    kwh_known = session.energy_kwh is not None and not session.energy_is_estimated
    if kwh_known:
        total = sum(c.energy_kwh or 0 for c in group)
        if not _kwh_ok(session.energy_kwh, total):
            return None
        kwh_error = abs(session.energy_kwh - total) / max(session.energy_kwh, 1)
    else:
        # Ohne gemessene kWh traegt nur der SoC: eine Folge mehrerer Ladungen
        # muss dann an BEIDEN Enden passen (18.05.: geschaetzte 57 kWh, Start 21
        # = Beginn der ersten von drei Ladungen).
        if len(group) > 1 and (
            session.soc_start is None
            or abs(session.soc_start - group[0].soc_start) > SOC_TOLERANCE
        ):
            return None
        kwh_error = 0.0
    # Ein abweichender End-SoC ist nur bei einer einzelnen Ladung am selben Tag
    # zulaessig - dann ist er genau der Fehler, den der Abgleich korrigieren soll.
    if soc_gap > SOC_TOLERANCE and not (len(group) == 1 and same_day and kwh_known):
        return None

    score = kwh_error + soc_gap / 20 + 0.05 * (len(group) - 1)
    # Der Start-SoC ist gerade der Wert, der oft falsch ist (20 aus dem
    # Formular) - er zaehlt deshalb nur schwach, entscheidet aber zwischen einer
    # einzelnen Ladung und einer Folge, die genau dort beginnt.
    if session.soc_start is not None:
        score += abs(session.soc_start - group[0].soc_start) / 100
    if not same_day:
        score += 0.2 * max((day - last.end.date()).days, 0)
    if _has_clock_time(session.start_time) and same_day:
        score += abs(last.start - session.start_time).total_seconds() / 3600 / 6
    derived = _derived_type(group)
    if session.charging_type is not None and derived is not None and derived != session.charging_type:
        score += 1.0
    return score


def _candidate_pairs(
    sessions: list[models.ChargingSession], charges: list[Charge]
) -> list[tuple[float, int, models.ChargingSession, list[int]]]:
    pairs = []
    for order, session in enumerate(sessions):
        if session.end_time is not None:
            for index, charge in enumerate(charges):
                score = _score_timed(session, charge)
                if score is not None:
                    pairs.append((score, order, session, [index]))
            continue
        for last_index in range(len(charges)):
            for size in range(1, MAX_GROUP + 1):
                first_index = last_index - size + 1
                if first_index < 0:
                    break
                group = charges[first_index : last_index + 1]
                score = _score_group(session, group)
                if score is not None:
                    pairs.append((score, order, session, list(range(first_index, last_index + 1))))
    # Bei gleicher Bewertung entscheidet die Reihenfolge der Vorgaenge, damit
    # das Ergebnis nicht vom Zufall der Liste abhaengt.
    pairs.sort(key=lambda p: (p[0], p[1], p[3]))
    return pairs


def _changes_for(session: models.ChargingSession, group: list[Charge]) -> list[FieldChange]:
    first, last = group[0], group[-1]
    changes: list[FieldChange] = []

    def soc_change(name: str, current: int | None, target: int) -> None:
        if current is None or abs(current - target) > SOC_CORRECTION_THRESHOLD:
            changes.append(FieldChange(name, current, target))

    soc_change("soc_start", session.soc_start, first.soc_start)
    soc_change("soc_end", session.soc_end, last.soc_end)

    if abs(session.start_time - first.start) > TIME_CORRECTION_THRESHOLD:
        changes.append(FieldChange("start_time", session.start_time, first.start))
    if session.end_time is None or abs(session.end_time - last.end) > TIME_CORRECTION_THRESHOLD:
        changes.append(FieldChange("end_time", session.end_time, last.end))

    if session.charging_type is None:
        derived = _derived_type(group)
        if derived is not None:
            changes.append(FieldChange("charging_type", None, derived))
    if session.energy_kwh is None:
        total = sum(c.energy_kwh or 0 for c in group)
        if total > 0:
            changes.append(FieldChange("energy_kwh", None, total))
    # Nur die Zeit zu korrigieren, waere bei automatisch erfassten Vorgaengen
    # meist eine Kleinigkeit. Bei Eintraegen ohne Uhrzeit ist sie dagegen der
    # eigentliche Gewinn (Wetterdienst, Rueckblick) - beides wird angeboten.
    return changes


def build_plan(db: Session, user: models.User, vehicle: models.Vehicle, charges: list[Charge]) -> Plan:
    plan = Plan(charges_total=len(charges))
    if not charges:
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
    imported_ids = {
        s.external_session_id[len(EXTERNAL_ID_PREFIX) :]
        for s in sessions
        if s.external_session_id and s.external_session_id.startswith(EXTERNAL_ID_PREFIX)
    }
    # Schon frueher aus dieser Datei angelegte Vorgaenge sind ihr eigenes
    # Gegenstueck - sie nehmen nicht am Abgleich teil, sonst wuerde eine Ladung
    # mit sich selbst verglichen.
    existing = [
        s
        for s in sessions
        if not (s.external_session_id or "").startswith(EXTERNAL_ID_PREFIX)
    ]
    open_charges = [c for c in charges if c.charge_id not in imported_ids]
    plan.already_imported = len(charges) - len(open_charges)

    assigned_charges: set[int] = set()
    matched: dict[str, list[int]] = {}
    for _score, _order, session, indices in _candidate_pairs(existing, open_charges):
        if session.id in matched or assigned_charges.intersection(indices):
            continue
        matched[session.id] = indices
        assigned_charges.update(indices)

    for session in existing:
        indices = matched.get(session.id)
        if indices is None:
            continue
        group = [open_charges[i] for i in indices]
        changes = _changes_for(session, group)
        if changes:
            plan.updates.append(Update(session=session, charges=group, changes=changes))
        else:
            plan.unchanged += 1
        derived = _derived_type(group)
        if session.charging_type is not None and derived is not None and derived != session.charging_type:
            plan.hints.append(
                Hint(
                    "type_mismatch",
                    session.start_time,
                    {
                        "session_id": session.id,
                        "stored": session.charging_type.value,
                        "derived": derived.value,
                    },
                )
            )

    file_start = min(c.start for c in open_charges) if open_charges else None
    file_end = max(c.end for c in open_charges) if open_charges else None
    for index, charge in enumerate(open_charges):
        if index in assigned_charges:
            continue
        if not charge.is_real:
            plan.charges_ignored += 1
            continue
        lower = charge.start.date() - timedelta(days=1)
        upper = charge.end.date() + timedelta(days=NEW_SESSION_CLEARANCE_DAYS)
        near = any(lower <= s.start_time.date() <= upper for s in existing)
        if near:
            plan.hints.append(
                Hint(
                    "unassigned_charge",
                    charge.start,
                    {
                        "soc_start": charge.soc_start,
                        "soc_end": charge.soc_end,
                        "energy_kwh": charge.energy_kwh,
                    },
                )
            )
        else:
            plan.new_sessions.append(NewSession(charge, _derived_type([charge])))

    if file_start is not None:
        for session in existing:
            if session.id in matched:
                continue
            if file_start - timedelta(days=1) <= session.start_time <= file_end + timedelta(days=1):
                plan.hints.append(Hint("unmatched_session", session.start_time, {"session_id": session.id}))

    plan.updates.sort(key=lambda u: u.session.start_time)
    plan.hints.sort(key=lambda h: h.when)
    return plan


# --- Uebernehmen ------------------------------------------------------------


def _fmt(value: object) -> str:
    if value is None:
        return "–"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, models.ChargingType):
        return value.value
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


NOTE_LABELS = {
    "soc_start": "Start-SoC",
    "soc_end": "End-SoC",
    "start_time": "Beginn",
    "end_time": "Ende",
    "charging_type": "Lade-Art",
    "energy_kwh": "kWh",
}


def apply_plan(db: Session, user: models.User, vehicle: models.Vehicle, plan: Plan, accepted: set[str]) -> tuple[int, int]:
    updated = 0
    for update in plan.updates:
        if update.key not in accepted:
            continue
        session = update.session
        for change in update.changes:
            setattr(session, change.field, change.new)
            if change.field == "energy_kwh":
                session.energy_is_estimated = False
        note = "MySkoda-Abgleich: " + ", ".join(
            f"{NOTE_LABELS[c.field]} {_fmt(c.old)} → {_fmt(c.new)}" for c in update.changes
        )
        if len(update.charges) > 1:
            note += f" ({len(update.charges)} Ladungen zusammengefasst)"
        session.notes = f"{session.notes}\n{note}" if session.notes else note
        updated += 1

    created = 0
    for new in plan.new_sessions:
        if new.key not in accepted:
            continue
        charge = new.charge
        db.add(
            models.ChargingSession(
                vehicle_id=vehicle.id,
                user_id=user.id,
                start_time=charge.start,
                end_time=charge.end,
                charging_type=new.charging_type,
                soc_start=charge.soc_start,
                soc_end=charge.soc_end,
                energy_kwh=charge.energy_kwh,
                energy_is_estimated=False,
                # Die App misst naeher am Akku als ein Wallbox-Zaehler (1-3 %
                # darunter) - als Fahrzeugmessung bleibt die Ladung aus der
                # Verlustrechnung heraus.
                energy_meter="vehicle",
                source=models.SessionSource.IMPORT,
                needs_review=True,
                external_session_id=f"{EXTERNAL_ID_PREFIX}{charge.charge_id}",
                notes="Importiert aus der MySkoda-App",
            )
        )
        created += 1

    db.commit()
    return updated, created
