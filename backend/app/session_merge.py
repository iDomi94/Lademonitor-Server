"""Mehrere automatische Quellen fuer dasselbe Fahrzeug, ohne Dubletten.

Bis 0.45 durfte je Fahrzeug nur EINE automatische Quelle laufen: jede baut
ihre `external_session_id` anders (`myskoda-`, `vwda-`, `renault-`, `bmw-`,
Home Assistant eine eigene), zwei Quellen legten also jeden Ladevorgang
zweimal an. Jetzt gilt: **wer zuerst kommt, legt an, jede weitere Quelle
ergaenzt.** Bevor eine Quelle einen Vorgang anlegt, sucht `find_match()` einen
vorhandenen Vorgang desselben Fahrzeugs, der zeitlich dazu passt; gibt es ihn,
fuehrt `merge_into()` die Werte zusammen, statt einen zweiten anzulegen.

**Zuordnung:** die Zeitraeume ueberschneiden sich (mit `OVERLAP_TOLERANCE`
Spiel, weil Poller Beginn und Ende verspaetet sehen) - zwei echte Ladevorgaenge
koennen das nicht. Fehlt einem der beiden das Ende, reicht ein Beginn innerhalb
von `START_WINDOW`. Bei mehreren Kandidaten gewinnt der naechstgelegene Beginn.

**Was ergaenzt wird** (Rangfolge je Feld, siehe Analyse vom 26.10.2026):

* Leere Felder werden immer gefuellt - auch an bestaetigten oder von Hand
  angelegten Vorgaengen.
* Vorhandene Werte werden nur an Vorgaengen ueberschrieben, die noch
  automatisch und "zu pruefen" sind. Was der Nutzer bestaetigt oder selbst
  eingetragen hat, fasst keine Quelle mehr an.
* Zeiten, SoC und Kilometerstand: die Quelle mit dem kleineren
  `TIME_SOC_RANK` gewinnt. Home Assistant meldet ereignisgesteuert auf die
  Sekunde, BMW liefert fertige Vorgaenge aus dem Fahrzeug, MyŠkoda und
  Renault pollen im Minutentakt, das VW-Datenportal hinkt bis zu Stunden
  hinterher.
* Energie: gemessene kWh schlagen geschaetzte, unabhaengig von der Quelle.
  Sind beide gemessen, bleibt der vorhandene Wert. Aendert sich der SoC und
  die Energie war nur geschaetzt, wird sie neu geschaetzt.
* Lade-Art: DC schlaegt AC (wer einmal ueber 23 kW sieht, hat recht).
* Temperatur: ein Fahrzeugwert ersetzt einen vom Wetterdienst.
* Gemessene Kosten aus Home Assistant (Netzbezug + Preis) ersetzen den
  vorgeschlagenen Anbieterpreis.

Jede Aenderung steht als Zeile in der Notiz. Ein zweites Zusammenfuehren mit
denselben Werten aendert nichts - so bleiben wiederholte Pushes und das
erneute Lesen der BMW-Historie folgenlos.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models
from .routers.sessions import estimate_energy_kwh, normalize_energy_meter

#: Spiel beim Ueberschneiden der Zeitraeume.
OVERLAP_TOLERANCE = timedelta(minutes=30)
#: Abstand der Beginne, wenn einem Vorgang das Ende fehlt.
START_WINDOW = timedelta(hours=3)
#: Wie weit vor/nach dem neuen Vorgang ueberhaupt gesucht wird.
SEARCH_MARGIN = timedelta(days=2)

#: Praefixe der external_session_id je Quelle (laengster zuerst).
_PREFIXES = (
    ("myskoda-app-", "myskoda_app"),
    ("myskoda-", "myskoda"),
    ("vwda-", "vwda"),
    ("renault-", "renault"),
    ("bmw-", "bmw"),
)

#: Kleiner ist genauer - fuer Zeiten, SoC und Kilometerstand.
TIME_SOC_RANK = {
    "myskoda_app": 0,
    "ha": 1,
    "bmw": 1,
    "myskoda": 2,
    "renault": 2,
    "vwda": 3,
}
_UNKNOWN_RANK = 9

_WEATHER_SOURCES = (models.TemperatureSource.WEATHER, models.TemperatureSource.WEATHER_DAILY)


def source_key(session: models.ChargingSession) -> str | None:
    """Kurzname der Quelle, aus der ein Vorgang stammt (None = von Hand/Import)."""
    external_id = session.external_session_id or ""
    for prefix, key in _PREFIXES:
        if external_id.startswith(prefix):
            return key
    if session.source == models.SessionSource.AUTOMATIC:
        return "ha"
    return None


def _rank(session: models.ChargingSession) -> int:
    return TIME_SOC_RANK.get(source_key(session) or "", _UNKNOWN_RANK)


def naive_local(when: datetime | None) -> datetime | None:
    """Zeitzonenbehaftet -> naive Ortszeit, wie `start_time` in der DB liegt."""
    if when is None or when.tzinfo is None:
        return when
    return when.astimezone().replace(tzinfo=None)


def _matches(existing: models.ChargingSession, start: datetime, end: datetime | None) -> bool:
    if existing.end_time is None or end is None:
        return abs(existing.start_time - start) <= START_WINDOW
    return (
        existing.start_time <= end + OVERLAP_TOLERANCE
        and start <= existing.end_time + OVERLAP_TOLERANCE
    )


def find_match(
    db: Session,
    user_id: str,
    vehicle_id: str,
    start_time: datetime,
    end_time: datetime | None,
) -> models.ChargingSession | None:
    """Vorhandener Vorgang desselben Fahrzeugs, zu dem dieser Zeitraum passt."""
    start = naive_local(start_time)
    end = naive_local(end_time)
    candidates = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.user_id == user_id,
            models.ChargingSession.vehicle_id == vehicle_id,
            models.ChargingSession.start_time >= start - SEARCH_MARGIN,
            models.ChargingSession.start_time <= (end or start) + SEARCH_MARGIN,
        )
        .all()
    )
    matching = [c for c in candidates if _matches(c, start, end)]
    if not matching:
        return None
    return min(matching, key=lambda c: abs(c.start_time - start))


def _fmt(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, datetime):
        return value.strftime("%d.%m. %H:%M")
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, models.ChargingType):
        return value.value
    return str(value)


def merge_into(
    db: Session,
    existing: models.ChargingSession,
    incoming: models.ChargingSession,
    label: str,
) -> list[str]:
    """Fuehrt `incoming` (nicht in der DB) in `existing` zusammen.

    Gibt die Aenderungen als lesbare Zeilen zurueck; leer, wenn nichts zu tun
    war. `label` nennt die ergaenzende Quelle in der Notiz.
    """
    editable = existing.needs_review and existing.source == models.SessionSource.AUTOMATIC
    better = editable and _rank(incoming) < _rank(existing)
    changes: list[str] = []

    def take(field: str, name: str, value, *, overwrite: bool) -> bool:
        current = getattr(existing, field)
        if value is None or value == current:
            return False
        if current is not None and not overwrite:
            return False
        changes.append(f"{name} {_fmt(current)} → {_fmt(value)}")
        setattr(existing, field, value)
        return True

    take("start_time", "Beginn", naive_local(incoming.start_time), overwrite=better)
    take("end_time", "Ende", naive_local(incoming.end_time), overwrite=better)
    soc_changed = take("soc_start", "SoC Start", incoming.soc_start, overwrite=better)
    soc_changed |= take("soc_end", "SoC Ende", incoming.soc_end, overwrite=better)
    take("odometer_km", "km-Stand", incoming.odometer_km, overwrite=better)
    take(
        "charging_type",
        "Lade-Art",
        incoming.charging_type,
        overwrite=editable and incoming.charging_type == models.ChargingType.DC,
    )

    if existing.latitude is None and existing.longitude is None and incoming.latitude is not None:
        existing.latitude = incoming.latitude
        existing.longitude = incoming.longitude
        changes.append("Position ergänzt")
    if existing.location_id is None and incoming.location_id is not None:
        existing.location_id = incoming.location_id
        existing.geocoded_place = None
        changes.append("Ladeort ergänzt")
    elif existing.location_id is None and not existing.geocoded_place and incoming.geocoded_place:
        existing.geocoded_place = incoming.geocoded_place

    if incoming.outside_temp_c is not None and (
        existing.outside_temp_c is None
        or (editable and existing.outside_temp_source in _WEATHER_SOURCES
            and incoming.outside_temp_source == models.TemperatureSource.VEHICLE)
    ):
        if take("outside_temp_c", "Temperatur", incoming.outside_temp_c, overwrite=True):
            existing.outside_temp_source = incoming.outside_temp_source

    # --- Energie ------------------------------------------------------------
    energy_changed = False
    incoming_measured = incoming.energy_kwh is not None and not incoming.energy_is_estimated
    existing_measured = existing.energy_kwh is not None and not existing.energy_is_estimated
    if incoming_measured and not existing_measured and (existing.energy_kwh is None or editable):
        changes.append(f"kWh {_fmt(existing.energy_kwh)} → {_fmt(incoming.energy_kwh)} (gemessen)")
        existing.energy_kwh = incoming.energy_kwh
        existing.energy_is_estimated = False
        existing.energy_meter = incoming.energy_meter
        energy_changed = True
    elif existing.energy_kwh is None and incoming.energy_kwh is not None:
        changes.append(f"kWh – → {_fmt(incoming.energy_kwh)} (geschätzt)")
        existing.energy_kwh = incoming.energy_kwh
        existing.energy_is_estimated = True
        energy_changed = True
    elif soc_changed and existing.energy_is_estimated and editable:
        vehicle = db.get(models.Vehicle, existing.vehicle_id)
        before = existing.energy_kwh
        existing.energy_kwh = None
        if vehicle is not None:
            estimate_energy_kwh(existing, vehicle)
        if existing.energy_kwh is None:
            existing.energy_kwh = before
        elif existing.energy_kwh != before:
            changes.append(f"kWh {_fmt(before)} → {_fmt(existing.energy_kwh)} (geschätzt)")
            energy_changed = True

    # --- Anbieter und Preis -------------------------------------------------
    if existing.provider_id is None and incoming.provider_id is not None:
        existing.provider_id = incoming.provider_id
        changes.append("Anbieter ergänzt")
        if existing.price_per_kwh is None and incoming.price_per_kwh is not None:
            existing.price_per_kwh = incoming.price_per_kwh

    measured_cost = incoming.grid_energy_kwh is not None and incoming.price_total is not None
    if measured_cost and existing.grid_energy_kwh is None and (editable or existing.price_total is None):
        existing.grid_energy_kwh = incoming.grid_energy_kwh
        existing.price_total = incoming.price_total
        existing.price_per_kwh = (
            round(incoming.price_total / existing.energy_kwh, 4) if existing.energy_kwh else None
        )
        changes.append(f"gemessene Kosten {_fmt(incoming.price_total)} € übernommen")
    elif existing.price_total is None and incoming.price_total is not None:
        existing.price_total = incoming.price_total
        existing.price_per_kwh = incoming.price_per_kwh
        changes.append(f"Preis {_fmt(incoming.price_total)} € übernommen")
    elif (
        energy_changed
        and existing.price_per_kwh is not None
        and existing.grid_energy_kwh is None
        and existing.energy_kwh
    ):
        existing.price_total = round(existing.price_per_kwh * existing.energy_kwh, 2)
    elif energy_changed and existing.grid_energy_kwh is not None and existing.price_total and existing.energy_kwh:
        existing.price_per_kwh = round(existing.price_total / existing.energy_kwh, 4)

    if energy_changed:
        normalize_energy_meter(existing, db)

    if changes:
        line = f"Ergänzt durch {label}: " + ", ".join(changes) + "."
        existing.notes = f"{existing.notes}\n{line}" if existing.notes else line
        existing.updated_at = datetime.utcnow()
    return changes


def merge_or_none(
    db: Session,
    incoming: models.ChargingSession,
    label: str,
) -> tuple[models.ChargingSession | None, list[str]]:
    """Sucht einen passenden Vorgang und fuehrt `incoming` hinein.

    Gibt (vorhandener Vorgang, Aenderungen) zurueck, oder (None, []), wenn
    `incoming` neu angelegt werden soll.
    """
    match = find_match(
        db, incoming.user_id, incoming.vehicle_id, incoming.start_time, incoming.end_time
    )
    if match is None:
        return None, []
    return match, merge_into(db, match, incoming, label)
