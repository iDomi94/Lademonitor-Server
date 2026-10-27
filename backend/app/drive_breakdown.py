"""Mehrverbrauch einer Fahrt aufteilen: Tempo, Temperatur, Rest.

Beantwortet "warum heute 20,5 statt 17 kWh/100 km" - ohne den ganzen
Unterschied der Temperatur zuzuschreiben (das taete `temperature.py` allein)
und ohne grobe Kategorien wie Stadt/Land/Autobahn.

**Die eine neue Angabe** ist `ChargingSession.avg_speed_kmh`: die
Durchschnittsgeschwindigkeit seit dem letzten Laden, wie sie der Bordcomputer
anzeigt (beim Enyaq "Seit Laden"). Sie beschreibt genau die Strecke, aus der
`consumption.py` den Verbrauch des Vorgangs rechnet (N-1 bis N) - deshalb
gehoert sie an den Vorgang N, wie die Temperatur. Keine der automatischen
Quellen liefert sie (geprueft 26.10.2026: MyŠkoda Public API und die
laufenden Datensaetze des VW-Datenportals haben weder Tempo noch Fahrzeit),
sie wird also von Hand abgelesen.

**Das Modell ist Physik, nicht Kurvenanpassung**, weil nur so wenige Fahrten
genuegen und jeder Teil erklaerbar bleibt. Verbrauch je 100 km:

    y = a + c * v^2 + (q + h * kalt(T) + k * warm(T)) * 100 / v

* `a` - Rollwiderstand und Antrieb, unabhaengig vom Tempo (frei angepasst).
* `c * v^2` - Luftwiderstand. Die Kraft waechst mit v^2, die Energie je km
  also auch.
* Der Klammerausdruck ist eine LEISTUNG in kW (Grundlast der Elektronik `q`,
  Heizen `h` je Kelvin unter `HEAT_BELOW_C`, Kuehlen `k` je Kelvin ueber
  `COOL_ABOVE_C`). Eine Leistung kostet je km umso mehr, je langsamer man
  faehrt: 1 kW Heizung sind bei 105 km/h knapp 1 kWh/100 km, im Stadtverkehr
  bei 30 km/h ueber 3. Genau deshalb haengt der Temperatureffekt hier am
  Tempo - eine Kurve ueber der Temperatur allein kann das nicht.

Alle vier Koeffizienten ausser `a` werden zu physikalisch plausiblen
Vorgabewerten hin gezogen (Ridge-Regression, `PRIOR_DRIVES` Fahrten Gewicht)
und duerfen nicht negativ werden. Mit wenigen Fahrten bestimmt so vor allem
die Vorgabe die Form und die Daten das Niveau; je mehr Fahrten, desto mehr
uebernehmen die Daten. Gewichtet wird mit km wie ueberall.

**Die Aufteilung** einer Fahrt rechnet gegen das eigene Normal:
Bezugstempo = km-gewichteter Median der eigenen Fahrten, Bezugstemperatur =
im Komfortband (weder Heizen noch Kuehlen).

    Basis      = Modell(Bezugstempo, Komfort)
    Tempo      = Modell(v, Komfort) - Basis
    Temperatur = Modell(v, T) - Modell(v, Komfort)
    Rest       = gemessen - Modell(v, T)

Die vier Teile summieren sich exakt zum gemessenen Wert. Im Rest steckt alles,
was die Daten nicht kennen: Wind, Naesse, Steigung, Fahrweise, Messfehler.
Bewusst kein Schalter fuer die Fahrweise - aus Durchschnitten laesst sie sich
nicht messen, und sie ehrlich im Rest zu lassen ist besser als zu raten.

**Grenzen:** ein Durchschnittstempo verschluckt die Mischung - halb Stadt,
halb Autobahn ergibt dasselbe Mittel wie gleichmaessige Landstrasse, hat aber
mehr Luftwiderstand (der Mittelwert von v^2 ist groesser als das Quadrat des
Mittels). Der kalte Akku kostet eigentlich je km und nicht je Stunde; er
landet hier im Heizterm. Beides ist fuer eine Aufteilung in Groessenordnungen
gut genug, fuer Nachkommastellen nicht.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from . import models
from .consumption import ConsumptionResult, compute_vehicle_consumptions
from .temperature import drive_temperature

# Komfortband: dazwischen wird weder geheizt noch gekuehlt. Fest statt
# gesucht (anders als der Knick in temperature.py): mit wenigen Fahrten liesse
# sich ein Knickpunkt nicht belastbar schaetzen, und das Band ist bei allen
# Fahrzeugen aehnlich.
HEAT_BELOW_C = 18.0
COOL_ABOVE_C = 24.0

# Vorgabewerte (Mittelklasse-SUV wie Enyaq/ID.4, gemessen an der Ladesaeule):
# c so, dass 90 -> 120 km/h rund 4 kWh/100 km mehr kostet; 0,3 kW Grundlast;
# Waermepumpe/Heizung 0,1 kW je Kelvin (bei 0 Grad also ~1,8 kW); Klima
# 0,08 kW je Kelvin ueber 24 Grad.
PRIOR_AERO = 0.0006           # kWh/100 km je (km/h)^2
PRIOR_BASE_LOAD_KW = 0.3
PRIOR_HEAT_KW_PER_K = 0.1
PRIOR_COOL_KW_PER_K = 0.08

# Wie viele Fahrten die Vorgabe wert ist. Die Skalen geben an, um wieviel sich
# das jeweilige Merkmal zwischen typischen Fahrten unterscheidet - ohne sie
# waere die Vorgabe je nach Einheit des Merkmals viel zu stark oder zu
# schwach.
PRIOR_DRIVES = 5.0
FEATURE_SCALES = (3000.0, 1.0, 10.0, 5.0)

# Erst ab so vielen Fahrten mit Tempo UND Temperatur wird aufgeteilt. Darunter
# ist das Niveau `a` nicht bestimmt - jede Fahrt erklaerte sich selbst, und
# der Rest waere immer null.
MIN_DRIVES = 5

# Plausibel fuer eine Durchschnittsgeschwindigkeit ueber eine ganze
# Ladeperiode. Unter 5 km/h ist es kein Fahren mehr (oder ein Tippfehler).
MIN_SPEED_KMH = 5.0
MAX_SPEED_KMH = 250.0


@dataclass(slots=True)
class Drive:
    session_id: str
    start_time: datetime
    speed_kmh: float
    temp_c: float
    consumption: float
    km: float


@dataclass(slots=True)
class Model:
    base_consumption: float        # a
    aero: float                    # c
    base_load_kw: float            # q
    heat_kw_per_k: float           # h
    cool_kw_per_k: float           # k
    reference_speed_kmh: float
    drive_count: int

    def predict(self, speed_kmh: float, temp_c: float) -> float:
        power = (
            self.base_load_kw
            + self.heat_kw_per_k * max(HEAT_BELOW_C - temp_c, 0.0)
            + self.cool_kw_per_k * max(temp_c - COOL_ABOVE_C, 0.0)
        )
        return self.base_consumption + self.aero * speed_kmh ** 2 + power * 100.0 / speed_kmh


@dataclass(slots=True)
class Breakdown:
    base: float
    speed: float
    temperature: float
    rest: float
    reference_speed_kmh: float


def _features(speed: float, temp: float) -> tuple[float, float, float, float]:
    per_kw = 100.0 / speed
    return (
        speed ** 2,
        per_kw,
        max(HEAT_BELOW_C - temp, 0.0) * per_kw,
        max(temp - COOL_ABOVE_C, 0.0) * per_kw,
    )


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gauss mit Spaltenpivot - fuer hoechstens 5 Unbekannte reicht das."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    half = sum(w for _, w in ordered) / 2
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= half:
            return value
    return ordered[-1][0]


def fit(drives: list[Drive]) -> Model | None:
    """Ridge-Regression zu den Vorgabewerten hin, ohne negative Koeffizienten.

    Ein Koeffizient, der trotz Vorgabe negativ herauskommt (z.B. "je waermer
    desto sparsamer" ueber 24 Grad aus zwei Ausreissern), wird auf null
    festgesetzt und der Rest neu angepasst - physikalisch kostet weder Tempo
    noch Heizen noch Kuehlen jemals Energie zurueck.
    """
    if len(drives) < MIN_DRIVES:
        return None
    priors = [PRIOR_AERO, PRIOR_BASE_LOAD_KW, PRIOR_HEAT_KW_PER_K, PRIOR_COOL_KW_PER_K]
    rows = [(_features(d.speed_kmh, d.temp_c), d.consumption, d.km) for d in drives]
    total_km = sum(km for _, _, km in rows)
    if total_km <= 0:
        return None
    mean_km = total_km / len(rows)
    strengths = [PRIOR_DRIVES * mean_km * scale ** 2 for scale in FEATURE_SCALES]

    fixed_zero: set[int] = set()
    for _ in range(len(priors) + 1):
        free = [j for j in range(len(priors)) if j not in fixed_zero]
        size = 1 + len(free)          # Unbekannte: a und die freien Koeffizienten
        xtx = [[0.0] * size for _ in range(size)]
        xty = [0.0] * size
        for feats, y, w in rows:
            x = [1.0] + [feats[j] for j in free]
            for i in range(size):
                xty[i] += w * x[i] * y
                for k in range(size):
                    xtx[i][k] += w * x[i] * x[k]
        for idx, j in enumerate(free, start=1):
            xtx[idx][idx] += strengths[j]
            xty[idx] += strengths[j] * priors[j]
        solution = _solve(xtx, xty)
        if solution is None:
            return None
        coefs = [0.0] * len(priors)
        for idx, j in enumerate(free, start=1):
            coefs[j] = solution[idx]
        negative = [j for j in free if coefs[j] < 0]
        if not negative:
            break
        fixed_zero.update(negative)

    return Model(
        base_consumption=solution[0],
        aero=coefs[0],
        base_load_kw=coefs[1],
        heat_kw_per_k=coefs[2],
        cool_kw_per_k=coefs[3],
        reference_speed_kmh=round(_weighted_median([(d.speed_kmh, d.km) for d in drives]), 1),
        drive_count=len(drives),
    )


def split(model: Model, speed_kmh: float, temp_c: float, consumption: float) -> Breakdown:
    comfort = HEAT_BELOW_C  # liegt im Band: weder Heizen noch Kuehlen
    base = model.predict(model.reference_speed_kmh, comfort)
    at_speed = model.predict(speed_kmh, comfort)
    expected = model.predict(speed_kmh, temp_c)
    return Breakdown(
        base=base,
        speed=at_speed - base,
        temperature=expected - at_speed,
        rest=consumption - expected,
        reference_speed_kmh=model.reference_speed_kmh,
    )


def collect_drives(
    ordered: list[models.ChargingSession],
    results: dict[str, ConsumptionResult],
) -> tuple[list[Drive], int, int]:
    """Fahrten EINES Fahrzeugs (chronologisch) mit Tempo, Temperatur und
    Verbrauch. Zaehlt dazu die Fahrten mit Verbrauch, denen das Tempo bzw.
    (bei vorhandenem Tempo) die Temperatur fehlt."""
    drives: list[Drive] = []
    without_speed = 0
    without_temp = 0
    previous: models.ChargingSession | None = None
    for session in ordered:
        previous_session, previous = previous, session
        result = results.get(session.id)
        if not result or result.value is None or not result.km:
            continue
        speed = session.avg_speed_kmh
        if speed is None or not (MIN_SPEED_KMH <= speed <= MAX_SPEED_KMH):
            without_speed += 1
            continue
        temp = drive_temperature(session, previous_session)
        if temp is None:
            without_temp += 1
            continue
        drives.append(
            Drive(
                session_id=session.id,
                start_time=session.start_time,
                speed_kmh=speed,
                temp_c=temp,
                consumption=result.value,
                km=result.km,
            )
        )
    return drives, without_speed, without_temp


def breakdowns_for_vehicle(
    ordered: list[models.ChargingSession],
    results: dict[str, ConsumptionResult],
) -> dict[str, Breakdown]:
    """Aufteilung je Vorgang-ID - leer, solange es zu wenige Fahrten gibt."""
    drives, _, _ = collect_drives(ordered, results)
    model = fit(drives)
    if model is None:
        return {}
    return {d.session_id: split(model, d.speed_kmh, d.temp_c, d.consumption) for d in drives}


def analyse_user(
    sessions: list[models.ChargingSession],
    capacity_by_vehicle_id: dict[str, float | None],
):
    """Je Fahrzeug: (Modell oder None, Fahrten, ohne Tempo, ohne Temperatur)."""
    by_vehicle: dict[str, list[models.ChargingSession]] = defaultdict(list)
    for session in sessions:
        by_vehicle[session.vehicle_id].append(session)
    out = {}
    for vehicle_id, vehicle_sessions in by_vehicle.items():
        ordered = sorted(vehicle_sessions, key=lambda s: s.start_time)
        results = compute_vehicle_consumptions(ordered, capacity_by_vehicle_id.get(vehicle_id))
        drives, without_speed, without_temp = collect_drives(ordered, results)
        out[vehicle_id] = (fit(drives), drives, without_speed, without_temp)
    return out
