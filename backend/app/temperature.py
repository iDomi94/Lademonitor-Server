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

**Ausnahme: Werte vom Wetterdienst** (`outside_temp_source == weather_daily`,
seit v0.24.1) sind bereits das Mittel der Tagstunden ueber genau diesen
Fahrt-Zeitraum - `weather.py` holt dafuer jeden Tag zwischen N-1 und N. Sie
werden deshalb unveraendert genommen; sie nochmal mit dem Wert davor zu mitteln
wuerde die Nachbarfahrt hineinmischen und die bessere Angabe verwaessern.

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


def _covers_the_whole_drive(session: models.ChargingSession) -> bool:
    """Beschreibt die hinterlegte Temperatur schon die ganze Fahrt?

    Nur Werte vom Wetterdienst koennen das: sie sind das Mittel der Tagstunden
    ueber den Zeitraum seit dem vorherigen Ladevorgang. Ein Fahrzeugsensor
    misst beim Einstecken, also punktuell am Ende der Fahrt - fuer ihn bleibt
    es bei der Paarung mit dem Wert davor.
    """
    return session.outside_temp_source == models.TemperatureSource.WEATHER_DAILY

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

# Ab hier das Knickmodell (zwei Geraden mit gemeinsamem Knickpunkt).
#
# Warum nicht EINE Gerade: unterhalb der Komfortgrenze HEIZT das Fahrzeug,
# oberhalb KUEHLT es. Das sind zwei verschiedene Verbraucher, nicht eine
# gemeinsame lineare Ursache - der Verbrauch ueber der Temperatur ist eine
# Wanne, kein Hang. Eine einzelne Gerade presst beide Aeste in eine Steigung
# und mittelt sie gegeneinander weg; bei einem Datensatz mit viel Sommer und
# wenig Winter kommt dabei sogar das falsche Vorzeichen heraus.
#
# Warum nicht eine Parabel: die erzwingt symmetrische Kruemmung um ihren
# Scheitel und biegt ausserhalb der Daten schnell ins Unsinnige ab. Heiz- und
# Kuehlleistung wachsen dagegen jeweils ungefaehr linear mit dem Abstand zur
# Komforttemperatur, und beide Aeste duerfen unterschiedlich steil sein (Heizen
# kostet deutlich mehr als Kuehlen). Zwei Geraden bilden genau das ab und
# bleiben erklaerbar: "je Grad kaelter X, je Grad waermer Y".
#
# Der Knickpunkt wird gesucht, nicht gesetzt: wo die Wanne liegt, haengt an
# Fahrzeug, Klimaautomatik und Gewohnheit.
BREAKPOINT_SEARCH_MIN_C = 8.0
BREAKPOINT_SEARCH_MAX_C = 26.0
BREAKPOINT_SEARCH_STEP_C = 0.5

# Wann das Knickmodell ueberhaupt angeboten wird. Zwei zusaetzliche Parameter
# passen IMMER besser als einer - ohne diese Huerden waere das Ergebnis nie
# wieder eine Gerade, auch wenn die Daten nur einen Ast hergeben.
MIN_POINTS_PER_BRANCH = 4
# Dieselbe Spreizung, die weiter oben fuer die Kurve als Ganzes verlangt wird -
# JE AST. Der Wert stand zuerst auf 5 K, und an den echten Daten des Nutzers
# (ab Maerz gemessen, kaeltester Wert 5,7 Grad) hat sich das als zu lax
# erwiesen: das Modell legte den Knick auf 23 Grad, machte aus dem gesamten
# Bereich 5,7-23 Grad einen praktisch waagerechten "kalten" Ast und aus den
# zwei heissesten Fahrten einen steilen "warmen". Formal besser, inhaltlich
# nichts als zwei Ausreisser mit einer Geschichte drumherum.
MIN_BRANCH_SPAN_C = 8.0
# Um wieviel muss das Knickmodell die Streuung besser erklaeren als die
# Gerade, damit es die Gerade ersetzt? Zwei zusaetzliche Parameter verbessern
# r2 auch bei reinem Zufall ein Stueck weit - 0,05 war genau der Wert, den der
# Ausreisser-Fit oben gerade so erreicht hat.
MIN_R2_GAIN_FOR_BREAKPOINT = 0.10

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
class CurveVertex:
    """Ein Eckpunkt der Ausgleichskurve - Clients zeichnen den Streckenzug."""

    temp_c: float
    consumption: float


@dataclass(slots=True)
class Trend:
    """Ausgleichskurve durch die Punkte.

    `slope`/`intercept` beschreiben IMMER die einfache Ausgleichsgerade, auch
    wenn `model == "breakpoint"` - aeltere Clients (iOS/Android vor dieser
    Version) zeichnen damit weiter eine plausible Linie, statt gar keine. Wer
    die Kurve kennt, zeichnet `curve` als Streckenzug; sie hat zwei Eckpunkte
    bei der Geraden und drei beim Knickmodell.
    """

    slope: float
    intercept: float
    r2: float
    consumption_at_0c: float
    consumption_at_20c: float
    # Wieviel Prozent mehr bei 0 statt 20 Grad - die eigentliche Antwort auf
    # "was kostet mich der Winter".
    extra_pct_at_0c: float
    # "linear" oder "breakpoint".
    model: str
    curve: list[CurveVertex]
    # Nur beim Knickmodell: Temperatur des geringsten Verbrauchs, und die
    # Steigung je Ast (kalt <= 0, warm >= 0).
    breakpoint_c: float | None = None
    slope_cold: float | None = None
    slope_warm: float | None = None
    # True, wenn 0 Grad unterhalb der kaeltesten gemessenen Fahrt liegt - dann
    # ist die Kennzahl eine Hochrechnung, keine Messung. Das MUSS mit raus:
    # ein Datensatz, der im Maerz beginnt, hat schlicht keinen Winter gesehen.
    at_0c_is_extrapolated: bool = False


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
            if own is not None and _covers_the_whole_drive(session):
                # Ein Wert vom Wetterdienst ist seit v0.24.1 bereits das Mittel
                # ueber genau diesen Fahrt-Zeitraum (weather.py). Ihn nochmal
                # mit dem Wert davor zu mitteln wuerde die Nachbarfahrt
                # hineinmischen und die bessere Angabe wieder verwaessern.
                temp = own
            elif own is not None and before is not None:
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


def _weighted_line(points: list[TempPoint]) -> tuple[float, float] | None:
    """Km-gewichtete Ausgleichsgerade (Steigung, Achsenabschnitt).

    Gewichtete kleinste Quadrate von Hand - fuer eine Gerade sind das fuenf
    Summen, und numpy/scipy waere eine Abhaengigkeit fuer nichts (scipy steckt
    zwar ueber `reverse_geocoder` schon drin, aber genau dieses Paket ist beim
    Bauen fuer fremde Architekturen schon einmal zum Problem geworden - siehe
    CLAUDE.md).
    """
    total_weight = sum(p.km for p in points)
    if total_weight <= 0:
        return None
    mean_x = sum(p.temp_c * p.km for p in points) / total_weight
    mean_y = sum(p.consumption * p.km for p in points) / total_weight
    variance_x = sum(p.km * (p.temp_c - mean_x) ** 2 for p in points)
    if variance_x <= 0:
        return None
    covariance = sum(
        p.km * (p.temp_c - mean_x) * (p.consumption - mean_y) for p in points
    )
    slope = covariance / variance_x
    return slope, mean_y - slope * mean_x


def _r2(points: list[TempPoint], predict) -> float:
    """Anteil der Streuung, den ein Modell erklaert - km-gewichtet.

    Ohne diesen Wert liesse sich aus jeder noch so zufaelligen Punktwolke eine
    Kurve ablesen; die Oberflaeche zeigt ihn deshalb mit an.
    """
    total_weight = sum(p.km for p in points)
    mean_y = sum(p.consumption * p.km for p in points) / total_weight
    variance_y = sum(p.km * (p.consumption - mean_y) ** 2 for p in points)
    if variance_y <= 0:
        return 0.0
    residual = sum(p.km * (p.consumption - predict(p.temp_c)) ** 2 for p in points)
    return max(0.0, min(1.0, 1 - residual / variance_y))


def _fit_at_breakpoint(
    points: list[TempPoint], breakpoint_c: float
) -> tuple[float, float, float] | None:
    """Zwei Geraden, die sich bei `breakpoint_c` treffen (Wert, kalt, warm).

    Modell: `y = a + s_kalt * min(x - b, 0) + s_warm * max(x - b, 0)`. Bei
    festem `b` ist das in den drei Unbekannten LINEAR, also wieder gewichtete
    kleinste Quadrate - und weil die beiden Basisfunktionen nie gleichzeitig
    ungleich null sind, faellt der Kreuzterm weg und das 3x3-System loest sich
    von Hand auf. Deshalb wird `b` aussen abgesucht statt mit einem Optimierer:
    das ist der ganze Grund, warum hier weiterhin kein scipy noetig ist.
    """
    sum_w = sum_u = sum_v = sum_uu = sum_vv = 0.0
    sum_y = sum_uy = sum_vy = 0.0
    for point in points:
        weight = point.km
        delta = point.temp_c - breakpoint_c
        cold = min(delta, 0.0)
        warm = max(delta, 0.0)
        sum_w += weight
        sum_u += weight * cold
        sum_v += weight * warm
        sum_uu += weight * cold * cold
        sum_vv += weight * warm * warm
        sum_y += weight * point.consumption
        sum_uy += weight * cold * point.consumption
        sum_vy += weight * warm * point.consumption

    # Beide Aeste brauchen eine eigene Spreizung, sonst ist ihre Steigung gar
    # nicht bestimmt (alle Punkte eines Astes auf derselben Temperatur).
    if sum_uu <= 0 or sum_vv <= 0:
        return None
    denominator = sum_w - sum_u * sum_u / sum_uu - sum_v * sum_v / sum_vv
    if abs(denominator) < 1e-9:
        return None
    value_at_break = (
        sum_y - sum_u * sum_uy / sum_uu - sum_v * sum_vy / sum_vv
    ) / denominator
    slope_cold = (sum_uy - value_at_break * sum_u) / sum_uu
    slope_warm = (sum_vy - value_at_break * sum_v) / sum_vv
    return value_at_break, slope_cold, slope_warm


def _best_breakpoint(points: list[TempPoint]):
    """Den Knickpunkt suchen, der die Streuung am besten erklaert.

    Gibt `None` zurueck, wenn das Knickmodell hier nicht zu rechtfertigen ist -
    und das ist der wichtigere Teil dieser Funktion. Verlangt werden:

    * genug Punkte UND genug Temperaturspreizung auf BEIDEN Aesten (sonst
      beschreibt ein Ast eine Wolke und der andere zwei Ausreisser),
    * die physikalisch erwartete Wannenform (kalter Ast faellt, warmer Ast
      steigt) - kommt etwas anderes heraus, sagen die Daten gerade nicht das,
      was das Modell behauptet.

    Die dritte Huerde - spuerbar besser als die Gerade - zieht `build_trend()`,
    weil sie beide Modelle vergleicht.
    """
    temperatures = [p.temp_c for p in points]
    lowest, highest = min(temperatures), max(temperatures)

    best = None
    steps = int(
        (BREAKPOINT_SEARCH_MAX_C - BREAKPOINT_SEARCH_MIN_C) / BREAKPOINT_SEARCH_STEP_C
    )
    for index in range(steps + 1):
        candidate = BREAKPOINT_SEARCH_MIN_C + index * BREAKPOINT_SEARCH_STEP_C
        if candidate - lowest < MIN_BRANCH_SPAN_C or highest - candidate < MIN_BRANCH_SPAN_C:
            continue
        cold = [p for p in points if p.temp_c < candidate]
        warm = [p for p in points if p.temp_c >= candidate]
        if len(cold) < MIN_POINTS_PER_BRANCH or len(warm) < MIN_POINTS_PER_BRANCH:
            continue
        fit = _fit_at_breakpoint(points, candidate)
        if fit is None:
            continue
        value_at_break, slope_cold, slope_warm = fit
        # Die Wannenform ist die Begruendung des Modells, keine Zugabe.
        if slope_cold > 0 or slope_warm < 0:
            continue

        def predict(temp, b=candidate, a=value_at_break, sc=slope_cold, sw=slope_warm):
            return a + (sc * (temp - b) if temp < b else sw * (temp - b))

        score = _r2(points, predict)
        if best is None or score > best[0]:
            best = (score, candidate, slope_cold, slope_warm, predict)
    return best


def build_trend(points: list[TempPoint]) -> Trend | None:
    """Ausgleichskurve durch die Punkte - Knickmodell, sonst Gerade.

    Gibt None zurueck, wenn zu wenige Punkte oder ein zu schmaler
    Temperaturbereich vorliegen (siehe die Konstanten oben): eine Kurve durch
    vier Punkte, die alle zwischen 18 und 20 Grad liegen, sagt nichts ueber
    den Winter.
    """
    if len(points) < MIN_POINTS_FOR_TREND:
        return None
    temperatures = [p.temp_c for p in points]
    lowest, highest = min(temperatures), max(temperatures)
    if highest - lowest < MIN_TEMP_SPAN_FOR_TREND_C:
        return None
    if sum(p.km for p in points) <= 0:
        return None

    line = _weighted_line(points)
    if line is None:
        return None
    slope, intercept = line

    def linear(temp: float) -> float:
        return slope * temp + intercept

    linear_r2 = _r2(points, linear)

    model, predict, r2 = "linear", linear, linear_r2
    breakpoint_c = slope_cold = slope_warm = None

    best = _best_breakpoint(points)
    if best is not None and best[0] - linear_r2 >= MIN_R2_GAIN_FOR_BREAKPOINT:
        r2, breakpoint_c, slope_cold, slope_warm, predict = best
        model = "breakpoint"

    at_0 = predict(0.0)
    at_20 = predict(20.0)
    if at_20 <= 0:
        return None

    # Eckpunkte fuer die Darstellung: gezeichnet wird nur ueber den GEMESSENEN
    # Bereich. Die Kurve darueber hinaus zu verlaengern liesse eine
    # Hochrechnung wie eine Messung aussehen.
    vertices = [lowest, highest]
    if breakpoint_c is not None and lowest < breakpoint_c < highest:
        vertices = [lowest, breakpoint_c, highest]

    return Trend(
        slope=round(slope, 3),
        intercept=round(intercept, 2),
        r2=round(r2, 2),
        consumption_at_0c=round(at_0, 1),
        consumption_at_20c=round(at_20, 1),
        extra_pct_at_0c=round((at_0 - at_20) / at_20 * 100, 1),
        model=model,
        curve=[
            CurveVertex(temp_c=round(t, 1), consumption=round(predict(t), 2))
            for t in vertices
        ],
        breakpoint_c=round(breakpoint_c, 1) if breakpoint_c is not None else None,
        slope_cold=round(slope_cold, 3) if slope_cold is not None else None,
        slope_warm=round(slope_warm, 3) if slope_warm is not None else None,
        at_0c_is_extrapolated=lowest > 0.0,
    )
