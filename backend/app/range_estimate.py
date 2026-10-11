"""Reale Reichweite je Temperatur (ab v0.34.0).

Reichweite = nutzbare Akkukapazitaet / Verbrauch ab Akku. Der Verbrauch kommt
aus der Temperaturkurve (`temperature.build_trend()`), allerdings nicht aus der
angezeigten, sondern aus einer zweiten Kurve AB AKKU:

**Die Verbrauchswerte der App sind aus den GELADENEN kWh gerechnet**, enthalten
also den Ladeverlust (siehe `consumption.py`). Aus dem Akku kommt weniger -
ohne Korrektur waere die Reichweite um den Verlust (typisch 8-15 %) zu
niedrig. Entscheidung des Nutzers (11.10.2026): herausrechnen, wenn bekannt.

Korrigiert wird je Fahrt, nicht pauschal: nur Fahrten, deren Vorgang an der
LADESAEULE gemessen wurde, enthalten den Verlust. Geschaetzte Energie
(SoC-Hub x Nennkapazitaet) und im Fahrzeug abgelesene kWh sind schon ab Akku -
sie zu teilen wuerde doppelt korrigieren. Bei einem Vollladungs-Intervall
(`full_charge_interval`) entscheidet der Vorgang N selbst, eine Vereinfachung:
das Intervall kann gemischte Vorgaenge enthalten.

Der Verlust ist der energiegewichtete Gesamtwert aus `battery.py` (alle an der
Ladesaeule gemessenen Vorgaenge des Fahrzeugs, AC und DC gemischt - genau so
gemischt wie die Fahrten, die korrigiert werden). Unter
`MIN_LOSS_SESSIONS` Vorgaengen wird nichts abgezogen und die Antwort sagt das
(`loss_pct` None). Der Akku-Index wird bewusst NICHT eingerechnet (Entscheidung
11.10.2026): er ist kein echter SoH.

Die Apps (Reichweitenrechner) bekommen `curve` je ganzem Grad und
interpolieren selbst - sie muessen das Kurvenmodell nicht kennen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from . import battery, models
from .temperature import TempPoint, Trend, build_trend

RANGE_STEP_C = 5
# Temperaturbereich der Kurve fuer die Apps (Reglerbereich des Rechners).
CURVE_FROM_C = -20
CURVE_TO_C = 40
MIN_LOSS_SESSIONS = 3
# Unter diesem Verbrauch ist eine hochgerechnete Kurve unphysikalisch
# (ein Ast laeuft gegen null) - dort gibt es keinen Wert statt eines absurden.
MIN_PLAUSIBLE_CONSUMPTION = 8.0


@dataclass(slots=True)
class RangeRow:
    temp_c: int
    consumption: float
    range_km: int
    extrapolated: bool


@dataclass(slots=True)
class RangeEstimate:
    vehicle_id: str
    capacity_kwh: float
    # Abgezogener Ladeverlust in Prozent, None = nicht bekannt, nichts abgezogen
    loss_pct: float | None
    measured_from_c: float
    measured_to_c: float
    rows: list[RangeRow]
    # (Temperatur je ganzem Grad, Verbrauch ab Akku)
    curve: list[tuple[int, float]]


def predict(trend: Trend, temp_c: float) -> float:
    """Verbrauch der Kurve bei `temp_c`, auch ausserhalb des Messbereichs."""
    if trend.model == "breakpoint" and trend.breakpoint_c is not None:
        b = trend.breakpoint_c
        at_b = next(v.consumption for v in trend.curve if v.temp_c == b)
        slope = trend.slope_cold if temp_c < b else trend.slope_warm
        return at_b + (slope or 0.0) * (temp_c - b)
    return trend.slope * temp_c + trend.intercept


def overall_loss(
    sessions: list[models.ChargingSession],
    nominal_kwh: float | None,
    provider_meters: dict[str, str],
) -> float | None:
    """Energiegewichteter Ladeverlust (Anteil, z.B. 0.11) oder None."""
    if not nominal_kwh:
        return None
    points, _ = battery.collect_points(sessions, nominal_kwh, provider_meters)
    charger = [p for p in points if p.energy_meter == battery.METER_CHARGER]
    if len(charger) < MIN_LOSS_SESSIONS:
        return None
    apparent = sum(p.energy_kwh for p in charger) / sum(p.soc_delta for p in charger) * 100
    # Weniger als die Nennkapazitaet (gealterter Akku, Messfehler) heisst nicht
    # "negativer Verlust" - dann wird schlicht nichts abgezogen.
    return max(apparent / nominal_kwh - 1, 0.0)


def battery_side_points(
    points: list[TempPoint],
    sessions_by_id: dict[str, models.ChargingSession],
    provider_meters: dict[str, str],
    loss: float,
) -> list[TempPoint]:
    """Verbrauch je Fahrt ab Akku (siehe Modul-Docstring)."""
    result = []
    for p in points:
        session = sessions_by_id.get(p.session_id)
        contains_loss = (
            session is not None
            and p.method != "estimated_energy"
            and not session.energy_is_estimated
            and battery.effective_meter(session, provider_meters) == battery.METER_CHARGER
        )
        result.append(replace(p, consumption=p.consumption / (1 + loss)) if contains_loss else p)
    return result


def _temperatures(lowest: float, highest: float) -> list[int]:
    start = math.ceil(lowest / RANGE_STEP_C) * RANGE_STEP_C
    end = math.floor(highest / RANGE_STEP_C) * RANGE_STEP_C
    temps = set(range(start, end + 1, RANGE_STEP_C)) | {0, 20}
    return sorted(t for t in temps if CURVE_FROM_C <= t <= CURVE_TO_C)


def build_range(
    points: list[TempPoint],
    sessions_by_id: dict[str, models.ChargingSession],
    vehicle: models.Vehicle,
    provider_meters: dict[str, str],
) -> RangeEstimate | None:
    """None ohne Akkukapazitaet oder ohne belastbare Kurve."""
    capacity = vehicle.battery_capacity_kwh
    if not capacity or not points:
        return None
    loss = overall_loss(
        [s for s in sessions_by_id.values() if s.vehicle_id == vehicle.id],
        capacity,
        provider_meters,
    )
    used = battery_side_points(points, sessions_by_id, provider_meters, loss) if loss else points
    trend = build_trend(used)
    if trend is None:
        return None
    lowest = min(p.temp_c for p in points)
    highest = max(p.temp_c for p in points)

    rows = []
    for t in _temperatures(lowest, highest):
        consumption = predict(trend, t)
        if consumption < MIN_PLAUSIBLE_CONSUMPTION:
            continue
        rows.append(
            RangeRow(
                temp_c=t,
                consumption=round(consumption, 1),
                range_km=round(capacity / consumption * 100),
                extrapolated=t < lowest or t > highest,
            )
        )
    curve = [
        (t, round(c, 2))
        for t in range(CURVE_FROM_C, CURVE_TO_C + 1)
        if (c := predict(trend, t)) >= MIN_PLAUSIBLE_CONSUMPTION
    ]
    return RangeEstimate(
        vehicle_id=vehicle.id,
        capacity_kwh=capacity,
        loss_pct=round(loss * 100, 1) if loss is not None else None,
        measured_from_c=round(lowest, 1),
        measured_to_c=round(highest, 1),
        rows=rows,
        curve=curve,
    )
