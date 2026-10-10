"""Erinnerungen je Fahrzeug: HU, Inspektion, Reifenwechsel, ... (ab v0.33.0).

Gespeichert wird nur die naechste Faelligkeit (Datum und/oder km-Stand,
models.Reminder). Status und km-Prognose werden bei jedem Abruf frisch
gerechnet, wie Verbrauch und Kosten. Die Apps rechnen dasselbe lokal nach
(LocalReminderCalculator in beiden Repos) - Regelaenderungen hier MUESSEN dort
mitgezogen werden; tests/test_reminders.py haelt die Faelle fest.

Regeln:

* **Status:** `overdue`, wenn das Datum VERSTRICHEN ist (der Faelligkeitstag
  selbst zaehlt noch als "bald faellig") oder der km-Stand erreicht ist;
  `due_soon` innerhalb des Vorlaufs (`lead_days` bzw. `lead_km`); sonst `ok`.
  Ohne jede Faelligkeit: `done`, wenn schon einmal erledigt, sonst `ok`.
* **km-Prognose:** aktueller Stand = juengster km-Stand der Ladevorgaenge,
  Fahrleistung aus den letzten 90 Tagen (mindestens 14 Tage Spanne, sonst die
  ganze Historie - dieselbe Regel wie der Tarifrechner der Apps). Daraus das
  Datum, an dem `due_km` erreicht ist.
* **Erledigt:** die naechste Faelligkeit kommt aus der Wiederholung. Bei Arten
  mit festem Kalendertag (Reifenwechsel, Versicherung, Steuer, THG) laeuft das
  Datum vom ALTEN Faelligkeitstag aus weiter, bis es nach dem
  Erledigungsdatum liegt - ein Reifenwechsel am 20.10. macht den naechsten
  nicht zum 20.10., sondern wieder zum 15.10. Alle anderen (HU, Inspektion,
  ...) rechnen ab Erledigungsdatum. km immer ab dem km-Stand beim Erledigen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .fees import add_months

KINDS = (
    "hu",
    "inspection",
    "brake_fluid",
    "tire_winter",
    "tire_summer",
    "insurance",
    "tax",
    "thg",
    "custom",
)

# Vorlagen beim Anlegen (alle Werte aenderbar). `day` = fester Kalendertag
# (Monat, Tag) fuer die erste Faelligkeit.
TEMPLATES: dict[str, dict] = {
    "hu": {"repeat_months": 24},
    "inspection": {"repeat_months": 12, "repeat_km": 30000},
    "brake_fluid": {"repeat_months": 24},
    "tire_winter": {"repeat_months": 12, "day": (10, 15)},
    "tire_summer": {"repeat_months": 12, "day": (4, 15)},
    "insurance": {"repeat_months": 12},
    "tax": {"repeat_months": 12},
    "thg": {"repeat_months": 12},
    "custom": {},
}

# Arten, deren Faelligkeit an einem festen Kalendertag haengt (siehe oben).
FIXED_DATE_KINDS = frozenset({"tire_winter", "tire_summer", "insurance", "tax", "thg"})

# Welche Kostenkategorie "Erledigt" vorschlaegt. THG ist eine Einnahme, keine
# Kosten - dafuer gibt es keinen Vorschlag.
COST_CATEGORY = {
    "hu": "maintenance",
    "inspection": "maintenance",
    "brake_fluid": "maintenance",
    "tire_winter": "tires",
    "tire_summer": "tires",
    "insurance": "insurance",
    "tax": "tax",
    "custom": "other",
}

# Welcher Reifenwechsel welche Erinnerung erledigt.
TIRE_KIND_REMINDER = {"winter": "tire_winter", "summer": "tire_summer"}
# Ein Wechsel erledigt die Erinnerung nur, wenn er hoechstens so viele Tage
# VOR ihrer Faelligkeit liegt - ein nachgetragener Wechsel von vor zwei Jahren
# soll die kommende nicht abhaken.
TIRE_COMPLETE_WINDOW_DAYS = 90

MILEAGE_WINDOW_DAYS = 90
MIN_MILEAGE_SPAN_DAYS = 14

STATUS_RANK = {"ok": 0, "done": 0, "due_soon": 1, "overdue": 2}


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class Mileage:
    """Aktueller km-Stand und Fahrleistung eines Fahrzeugs."""

    current_km: int | None
    km_per_day: float | None


def mileage(sessions, today: date) -> Mileage:
    """Aus den Ladevorgaengen eines Fahrzeugs (beliebige Reihenfolge)."""
    points = sorted(
        (s.start_time, s.odometer_km) for s in sessions if s.odometer_km is not None
    )
    if not points:
        return Mileage(None, None)
    last_time, last_km = points[-1]
    window_start = last_time - timedelta(days=MILEAGE_WINDOW_DAYS)
    # Anker: der letzte Stand VOR dem Fenster, damit die Spanne das ganze
    # Fenster abdeckt (wie im Tarifrechner).
    before = [p for p in points if p[0] < window_start]
    anchor = before[-1] if before else points[0]
    span = (last_time - anchor[0]).days
    if span < MIN_MILEAGE_SPAN_DAYS:
        anchor = points[0]
        span = (last_time - anchor[0]).days
    rate = None
    if span >= MIN_MILEAGE_SPAN_DAYS and last_km >= anchor[1]:
        rate = (last_km - anchor[1]) / span
    return Mileage(last_km, rate)


@dataclass(frozen=True)
class ReminderState:
    status: str
    # Datum, an dem `due_km` bei der aktuellen Fahrleistung erreicht ist.
    km_forecast_date: date | None
    km_remaining: int | None
    days_remaining: int | None
    # Frueheres aus Datum und Prognose - zum Sortieren und fuer die
    # Mitteilungen der Apps.
    next_due_date: date | None


def evaluate(reminder, m: Mileage, today: date) -> ReminderState:
    due_day = _as_date(reminder.due_date)
    due_km = reminder.due_km
    lead_days = reminder.lead_days if reminder.lead_days is not None else 30
    lead_km = reminder.lead_km if reminder.lead_km is not None else 1000

    days_remaining = (due_day - today).days if due_day else None
    km_remaining = None
    forecast = None
    if due_km is not None and m.current_km is not None:
        km_remaining = due_km - m.current_km
        if km_remaining <= 0:
            forecast = today
        elif m.km_per_day:
            forecast = today + timedelta(days=round(km_remaining / m.km_per_day))

    if due_day is None and due_km is None:
        status = "done" if reminder.last_done_date else "ok"
    elif (days_remaining is not None and days_remaining < 0) or (
        km_remaining is not None and km_remaining <= 0
    ):
        status = "overdue"
    elif (days_remaining is not None and days_remaining <= lead_days) or (
        km_remaining is not None and km_remaining <= lead_km
    ):
        status = "due_soon"
    else:
        status = "ok"

    candidates = [d for d in (due_day, forecast) if d is not None]
    return ReminderState(
        status=status,
        km_forecast_date=forecast,
        km_remaining=km_remaining,
        days_remaining=days_remaining,
        next_due_date=min(candidates) if candidates else None,
    )


def first_due_date(kind: str, today: date) -> date | None:
    """Vorschlag fuer die erste Faelligkeit einer Vorlage mit festem Tag."""
    day = TEMPLATES.get(kind, {}).get("day")
    if not day:
        return None
    candidate = date(today.year, *day)
    return candidate if candidate >= today else date(today.year + 1, *day)


def next_due(reminder, done_day: date, done_km: int | None) -> tuple[date | None, int | None]:
    """Naechste Faelligkeit nach "Erledigt" (siehe Modul-Docstring)."""
    next_day = None
    months = reminder.repeat_months
    old_day = _as_date(reminder.due_date)
    if months:
        if reminder.kind in FIXED_DATE_KINDS and old_day is not None:
            k = 1
            next_day = add_months(old_day, months)
            while next_day <= done_day:
                k += 1
                next_day = add_months(old_day, months * k)
        else:
            next_day = add_months(done_day, months)
    next_km = None
    if reminder.repeat_km and done_km is not None:
        next_km = done_km + reminder.repeat_km
    return next_day, next_km
