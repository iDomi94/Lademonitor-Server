"""Verbrauch gegen Aussentemperatur (und Jahreszeit).

Die bekannteste Eigenart eines E-Autos: im Winter geht der Verbrauch spuerbar
hoch - Heizung, kalter Akku, hoeherer Rollwiderstand. Diese Datei beantwortet,
WIE stark das beim eigenen Fahrzeug ausfaellt, aus den ohnehin vorhandenen
Ladevorgaengen.

Drei Entscheidungen, die den Rest erklaeren:

**1. Welche Temperatur gehoert zu welchem Verbrauch?**
`consumption.py` berechnet den Verbrauch eines Vorgangs N aus der Strecke
zwischen N-1 und N - der Wert beschreibt also eine FAHRT, nicht den Ladevorgang.
`outside_temp_c` wird dagegen beim Einstecken gemessen, liegt also am ENDE
dieser Fahrt (und, fuer den naechsten Vorgang, ungefaehr an ihrem Anfang).
Deshalb wird gepaart: Temperatur der Fahrt = Mittel aus `temp(N-1)` und
`temp(N)`, wenn beide bekannt sind, sonst `temp(N)` allein. Das ist genauer als
nur einen der beiden Endpunkte zu nehmen und bleibt trotzdem erklaerbar - eine
echte Fahrtaufzeichnung mit Temperaturverlauf hat diese App nicht.

**2. Gewichtet wird mit Kilometern, nicht pro Vorgang.**
Wie beim Monatsdurchschnitt in `routers/stats.py`: ein 5-km-Vorgang und eine
400-km-Fahrt sind nicht gleich viel wert. Ohne Gewichtung zieht eine einzelne
Kurzstrecke mit unplausiblem Verbrauch den ganzen Temperaturbereich schief.

**3. Ausgewertet wird nur, was Daten hat.**
Vorgaenge ohne Temperatur oder ohne berechenbaren Verbrauch fallen heraus -
ihre Anzahl wird aber ausgewiesen (`sessions_without_temp`), damit sichtbar
bleibt, auf wie duenner Grundlage eine Aussage steht. Bestandsdaten haben
naturgemaess gar keine Temperatur (die Spalte gibt es erst seit v0.23.0), die
Auswertung waechst also erst mit der Zeit.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from . import models
from .consumption import compute_vehicle_consumptions

# Breite einer Temperaturklasse in Grad. 5 ist ein Kompromiss: schmaler wird
# jede Klasse zu duenn besetzt, breiter verwischt der Effekt, um den es geht
# (zwischen 0 und 10 Grad passiert der groesste Teil davon).
BUCKET_WIDTH_C = 5

# Unterhalb dieser Grenzen wird keine Regressionsgerade ausgewiesen: aus drei
# Punkten oder aus einem Temperaturbereich von zwei Grad laesst sich keine
# Steigung ablesen, die irgendetwas bedeutet. Lieber nichts zeigen als eine
# Zahl, der man ansieht, dass sie stimmt.
MIN_POINTS_FOR_TREND = 5
MIN_TEMP_SPAN_FOR_TREND_C = 8.0

# Meteorologische Jahreszeiten (Nordhalbkugel) - Monatsgrenzen statt
# Sonnenstand, weil die App ohnehin nur Monate kennt. Fuer Nutzer auf der
# Suedhalbkugel waeren die Bezeichnungen vertauscht; das ist eine bewusste
# Vereinfachung, keine Auslassung (die Temperaturklassen weiter unten sind
# davon unabhaengig und bleiben ueberall richtig).
SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
SEASON_ORDER = ("winter", "spring", "summer", "autumn")


@dataclass(slots=True)
class TempPoint:
    """Ein auswertbarer Punkt: eine Fahrt mit Temperatur und Verbrauch."""

    session_id: str
    start_time: datetime
    temp_c: float
    consumption: float
    km: float
    method: str
    season: str


@dataclass(slots=True)
class Bucket:
    """Eine Temperaturklasse, z.B. 0 bis 5 Grad."""

    from_c: float
    to_c: float
    avg_consumption: float
    session_count: int
    km: float


@dataclass(slots=True)
class SeasonStat:
    season: str
    avg_consumption: float
    session_count: int
    km: float


@dataclass(slots=True)
class Trend:
    """Ausgleichsgerade Verbrauch = slope * Temperatur + intercept."""

    slope: float
    intercept: float
    r2: float
    consumption_at_0c: float
    consumption_at_20c: float
    # Wieviel Prozent mehr bei 0 statt 20 Grad - die eigentliche Antwort auf
    # "was kostet mich der Winter".
    extra_pct_at_0c: float


def _weighted_average(pairs: list[tuple[float, float]]) -> float | None:
    """pairs: (Wert, Gewicht). Gewicht 0 kommt vor (Vorgang ohne Strecke)."""
    total_weight = sum(weight for _, weight in pairs)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in pairs) / total_weight


def collect_points(
    sessions: list[models.ChargingSession],
    capacity_by_vehicle_id: dict[str, float | None],
) -> tuple[list[TempPoint], int]:
    """Paart Verbrauch und Temperatur (siehe Modul-Docstring, Punkt 1).

    Erwartet die Vorgaenge EINES Nutzers, beliebig viele Fahrzeuge - die
    Verbrauchskette wird pro Fahrzeug gerechnet, die Temperatur-Paarung
    ebenfalls (der Vorgaenger eines Vorgangs ist immer einer desselben
    Fahrzeugs).

    Zweiter Rueckgabewert: wieviele Vorgaenge einen berechenbaren Verbrauch
    haetten, aber keine Temperatur - genau die Zahl, die zeigt, wie viel
    Aussagekraft noch fehlt.
    """
    points: list[TempPoint] = []
    without_temp = 0

    by_vehicle: dict[str, list[models.ChargingSession]] = defaultdict(list)
    for session in sessions:
        by_vehicle[session.vehicle_id].append(session)

    for vehicle_id, vehicle_sessions in by_vehicle.items():
        ordered = sorted(vehicle_sessions, key=lambda s: s.start_time)
        results = compute_vehicle_consumptions(
            ordered, capacity_by_vehicle_id.get(vehicle_id)
        )
        previous: models.ChargingSession | None = None
        for session in ordered:
            result = results.get(session.id)
            previous_session, previous = previous, session
            if not result or result.value is None or not result.km:
                continue

            own = session.outside_temp_c
            before = previous_session.outside_temp_c if previous_session else None
            if own is not None and before is not None:
                temp = (own + before) / 2
            elif own is not None:
                temp = own
            else:
                # Kein Wert am Ende der Fahrt. Der Startwert allein waere hier
                # verlockend, taugt aber nicht: zwischen dem Einstecken davor
                # und dieser Fahrt koennen Tage liegen.
                without_temp += 1
                continue

            points.append(
                TempPoint(
                    session_id=session.id,
                    start_time=session.start_time,
                    temp_c=round(temp, 1),
                    consumption=result.value,
                    km=result.km,
                    method=result.method or "unavailable",
                    season=SEASONS[session.start_time.month],
                )
            )

    points.sort(key=lambda p: p.start_time)
    return points, without_temp


def build_buckets(points: list[TempPoint]) -> list[Bucket]:
    """Fasst die Punkte in 5-Grad-Klassen zusammen (km-gewichtet)."""
    grouped: dict[int, list[TempPoint]] = defaultdict(list)
    for point in points:
        # Abrunden auf das Vielfache der Klassenbreite, auch im Negativen:
        # -3 Grad gehoert in die Klasse -5..0, nicht in 0..5.
        grouped[int(point.temp_c // BUCKET_WIDTH_C)].append(point)

    buckets: list[Bucket] = []
    for index in sorted(grouped):
        group = grouped[index]
        average = _weighted_average([(p.consumption, p.km) for p in group])
        if average is None:
            continue
        buckets.append(
            Bucket(
                from_c=index * BUCKET_WIDTH_C,
                to_c=(index + 1) * BUCKET_WIDTH_C,
                avg_consumption=round(average, 1),
                session_count=len(group),
                km=round(sum(p.km for p in group), 1),
            )
        )
    return buckets


def build_seasons(points: list[TempPoint]) -> list[SeasonStat]:
    grouped: dict[str, list[TempPoint]] = defaultdict(list)
    for point in points:
        grouped[point.season].append(point)

    seasons: list[SeasonStat] = []
    for season in SEASON_ORDER:
        group = grouped.get(season)
        if not group:
            continue
        average = _weighted_average([(p.consumption, p.km) for p in group])
        if average is None:
            continue
        seasons.append(
            SeasonStat(
                season=season,
                avg_consumption=round(average, 1),
                session_count=len(group),
                km=round(sum(p.km for p in group), 1),
            )
        )
    return seasons


def build_trend(points: list[TempPoint]) -> Trend | None:
    """Km-gewichtete Ausgleichsgerade durch die Punkte.

    Gewichtete kleinste Quadrate von Hand - fuer eine Gerade sind das fuenf
    Summen, und numpy/scipy waere eine Abhaengigkeit fuer nichts (scipy steckt
    zwar ueber `reverse_geocoder` schon drin, aber genau dieses Paket ist beim
    Bauen fuer fremde Architekturen schon einmal zum Problem geworden - siehe
    CLAUDE.md).

    Gibt None zurueck, wenn zu wenige Punkte oder ein zu schmaler
    Temperaturbereich vorliegen (siehe die Konstanten oben): eine Gerade durch
    vier Punkte, die alle zwischen 18 und 20 Grad liegen, sagt nichts ueber
    den Winter.
    """
    if len(points) < MIN_POINTS_FOR_TREND:
        return None
    temperatures = [p.temp_c for p in points]
    if max(temperatures) - min(temperatures) < MIN_TEMP_SPAN_FOR_TREND_C:
        return None

    weights = [p.km for p in points]
    total_weight = sum(weights)
    if total_weight <= 0:
        return None

    mean_x = sum(p.temp_c * p.km for p in points) / total_weight
    mean_y = sum(p.consumption * p.km for p in points) / total_weight

    covariance = sum(p.km * (p.temp_c - mean_x) * (p.consumption - mean_y) for p in points)
    variance_x = sum(p.km * (p.temp_c - mean_x) ** 2 for p in points)
    if variance_x <= 0:
        return None

    slope = covariance / variance_x
    intercept = mean_y - slope * mean_x

    variance_y = sum(p.km * (p.consumption - mean_y) ** 2 for p in points)
    residual = sum(
        p.km * (p.consumption - (slope * p.temp_c + intercept)) ** 2 for p in points
    )
    # r2 sagt, wieviel der Streuung die Temperatur ueberhaupt erklaert. Ohne
    # diesen Wert liesse sich aus jeder noch so zufaelligen Punktwolke eine
    # Steigung ablesen - die Oberflaeche zeigt ihn deshalb mit an.
    r2 = 1 - residual / variance_y if variance_y > 0 else 0.0

    at_0 = intercept
    at_20 = slope * 20 + intercept
    if at_20 <= 0:
        return None

    return Trend(
        slope=round(slope, 3),
        intercept=round(intercept, 2),
        r2=round(max(0.0, min(1.0, r2)), 2),
        consumption_at_0c=round(at_0, 1),
        consumption_at_20c=round(at_20, 1),
        extra_pct_at_0c=round((at_0 - at_20) / at_20 * 100, 1),
    )
