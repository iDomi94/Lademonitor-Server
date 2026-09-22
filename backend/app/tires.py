"""Reifensaetze und ihr Einfluss auf den Verbrauch (temperaturbereinigt).

Die Frage lautet: "brauchen meine Winterreifen mehr als die Sommerreifen?" -
und der naheliegende Weg, die Saison-Durchschnitte zu vergleichen, beantwortet
sie NICHT. Winterreifen werden im Winter gefahren; ein solcher Vergleich
schreibt den gesamten Winter-Mehrverbrauch (Heizung, kalter Akku, hoeherer
Rollwiderstand bei Kaelte) den Reifen zu. Das waere die Jahreszeit unter
anderem Namen.

Deshalb wird hier gegen die KURVE gerechnet, die `temperature.py` ohnehin
fittet: fuer jede Fahrt gibt es einen bei ihrer Temperatur erwarteten
Verbrauch, und verglichen werden die Abweichungen davon. "Dieser Satz liegt
bei gleicher Temperatur um X % darueber" ist eine Aussage ueber Reifen, der
rohe Saison-Durchschnitt ist es nicht.

**Die Grenze dieser Rechnung steht bewusst mit in der Antwort.** Sie traegt
nur, wenn beide Saetze ueber einen gemeinsamen Temperaturbereich gefahren
wurden (`overlap_span_c`). Ohne Ueberlappung extrapoliert das Modell in einen
Bereich, in dem der andere Satz nie gefahren ist - dann ist die Bereinigung
eine Hochrechnung, keine Messung. Die Uebergangsmonate um den Wechsel herum
(meist Maerz und Oktober, 5-15 Grad) liefern diese Ueberlappung; wer punktgenau
zum ersten Frost wechselt, bekommt sie nie.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from . import models
from .temperature import TempPoint, Trend, build_trend

# Unter so vielen Fahrten wird eine Gruppe nicht ausgewiesen - aus zwei Fahrten
# laesst sich kein Reifeneffekt ablesen, nur Streuung.
MIN_DRIVES_PER_GROUP = 3

# Wieviel gemeinsamen Temperaturbereich brauchen zwei Saetze, damit die
# Bereinigung mehr ist als eine Hochrechnung?
MIN_OVERLAP_SPAN_C = 5.0

# Durchgaenge der wechselseitigen Schaetzung (siehe _neutral_trend). Mehr
# bringen an echten Datenmengen nichts mehr - die Faktoren stehen nach drei
# bis vier Runden auf der vierten Nachkommastelle still.
NEUTRALISATION_ROUNDS = 5


@dataclass(slots=True)
class GroupStat:
    """Eine Gruppe von Fahrten - entweder eine Reifenart oder ein Satz."""

    key: str
    label: str
    kind: str
    drives: int
    km: float
    avg_consumption: float
    avg_temp_c: float
    min_temp_c: float
    max_temp_c: float
    # Verbrauch, auf die gemeinsame Referenztemperatur umgerechnet - erst diese
    # beiden Zahlen sind zwischen den Gruppen vergleichbar.
    adjusted_consumption: float | None = None
    delta_pct_vs_model: float | None = None


@dataclass(slots=True)
class TireComparison:
    by_kind: list[GroupStat] = field(default_factory=list)
    by_set: list[GroupStat] = field(default_factory=list)
    reference_temp_c: float | None = None
    model: str | None = None
    r2: float | None = None
    # Fahrten, die herausfallen - beide Zahlen gehoeren in die Anzeige, sonst
    # sieht man der Auswertung nicht an, auf wie duenner Grundlage sie steht.
    drives_without_set: int = 0
    drives_spanning_change: int = 0
    overlap_span_c: float | None = None
    overlap_ok: bool = False
    # Die eigentliche Antwort: Mehrverbrauch Winter gegenueber Sommer bei
    # gleicher Temperatur. None, solange eine der beiden Gruppen fehlt.
    winter_vs_summer_pct: float | None = None


def set_at(tire_sets: list[models.TireSet], when: datetime) -> models.TireSet | None:
    """Der Satz, der zu diesem Zeitpunkt montiert war - der letzte Wechsel davor.

    `tire_sets` muss zum Fahrzeug gehoeren und nach `installed_on` aufsteigend
    sortiert sein. Vor dem ersten eingetragenen Wechsel ist nichts bekannt;
    dann gibt es bewusst keinen Satz statt des aeltesten (der war damals
    gerade NICHT montiert).
    """
    mounted = None
    for tire_set in tire_sets:
        if tire_set.installed_on <= when:
            mounted = tire_set
        else:
            break
    return mounted


def sets_for_drives(
    sessions: list[models.ChargingSession], tire_sets: list[models.TireSet]
) -> tuple[dict[str, models.TireSet], int]:
    """Welcher Satz trug die GANZE Fahrt vor einem Ladevorgang?

    Der Verbrauch eines Vorgangs N stammt aus der Strecke zwischen N-1 und N
    (siehe consumption.py). Faellt ein Reifenwechsel in genau dieses Intervall,
    ist die Fahrt auf beiden Saetzen gelaufen und gehoert zu keinem von beiden -
    sie wird verworfen und gezaehlt (zweiter Rueckgabewert). Bei zwei Wechseln
    im Jahr trifft das hoechstens zwei Fahrten, kostet also fast nichts und
    haelt die Gruppen sauber.
    """
    by_vehicle: dict[str, list[models.ChargingSession]] = {}
    for session in sessions:
        by_vehicle.setdefault(session.vehicle_id, []).append(session)

    sets_by_vehicle: dict[str, list[models.TireSet]] = {}
    for tire_set in sorted(tire_sets, key=lambda t: t.installed_on):
        sets_by_vehicle.setdefault(tire_set.vehicle_id, []).append(tire_set)

    assigned: dict[str, models.TireSet] = {}
    spanning = 0
    for vehicle_id, vehicle_sessions in by_vehicle.items():
        mounted = sets_by_vehicle.get(vehicle_id) or []
        if not mounted:
            continue
        ordered = sorted(vehicle_sessions, key=lambda s: s.start_time)
        for previous, current in zip(ordered, ordered[1:]):
            at_start = set_at(mounted, previous.start_time)
            at_end = set_at(mounted, current.start_time)
            if at_start is None or at_end is None:
                continue
            if at_start.id != at_end.id:
                spanning += 1
                continue
            assigned[current.id] = at_end
    return assigned, spanning


def set_label(tire_set: models.TireSet) -> str:
    """Marke, Modell und Groesse zu einer Zeile - leere Felder fallen weg."""
    parts = [part for part in (tire_set.brand, tire_set.model, tire_set.size) if part]
    return " ".join(parts)


def _signature(tire_set: models.TireSet) -> str:
    """Schluessel, unter dem Wiedermontagen desselben Satzes zusammenfallen.

    Eine Zeile ist eine Montage (siehe models.TireSet) - derselbe Satz im
    naechsten Winter waere sonst eine zweite, halb so grosse Gruppe.
    """
    return "|".join(
        (tire_set.kind.value, tire_set.brand or "", tire_set.model or "", tire_set.size or "")
    )


def _expected(trend: Trend, temp_c: float) -> float:
    """Erwarteter Verbrauch laut Kurve - linear entlang ihrer Eckpunkte.

    Das funktioniert fuer beide Modelle ohne Fallunterscheidung: bei der
    Geraden hat `curve` zwei Eckpunkte, beim Knickmodell drei, und zwischen
    Eckpunkten ist die Kurve in beiden Faellen exakt linear. Ausserhalb wird
    der Randwert gehalten - kommt hier nicht vor, weil die Kurve genau ueber
    denselben Punkten aufgespannt ist, die hier ausgewertet werden.
    """
    vertices = trend.curve
    if temp_c <= vertices[0].temp_c:
        return vertices[0].consumption
    for left, right in zip(vertices, vertices[1:]):
        if temp_c <= right.temp_c:
            span = right.temp_c - left.temp_c
            if span <= 0:
                return right.consumption
            share = (temp_c - left.temp_c) / span
            return left.consumption + (right.consumption - left.consumption) * share
    return vertices[-1].consumption


def _weighted(values: list[tuple[float, float]]) -> float:
    total = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / total


def _group(
    key: str, label: str, kind: str, points: list[TempPoint], trend: Trend | None
) -> GroupStat:
    pairs = [(p.consumption, p.km) for p in points]
    temps = [p.temp_c for p in points]
    stat = GroupStat(
        key=key,
        label=label,
        kind=kind,
        drives=len(points),
        km=round(sum(p.km for p in points), 1),
        avg_consumption=round(_weighted(pairs), 2),
        avg_temp_c=round(_weighted([(p.temp_c, p.km) for p in points]), 1),
        min_temp_c=round(min(temps), 1),
        max_temp_c=round(max(temps), 1),
    )
    if trend is not None:
        # Km-gewichtetes Mittel der relativen Abweichung vom Modell. Relativ
        # und nicht absolut, weil "5 % ueber der Kurve" bei jeder Temperatur
        # dasselbe bedeutet - ein absoluter Abstand nicht.
        deltas = [
            (p.consumption / _expected(trend, p.temp_c) - 1.0, p.km)
            for p in points
            if _expected(trend, p.temp_c) > 0
        ]
        if deltas:
            stat.delta_pct_vs_model = round(_weighted(deltas) * 100, 1)
    return stat


def _neutral_trend(
    points: list[TempPoint], group_key: Callable[[TempPoint], str]
) -> Trend | None:
    """Die Temperaturkurve OHNE den Reifeneffekt darin.

    Die Kurve einfach ueber alle verglichenen Fahrten zu fitten, reicht nicht:
    die Saetze werden in verschiedenen Temperaturbaendern gefahren, also zieht
    ein durchweg durstigerer Satz die Kurve in seinem Band mit nach oben. Die
    Kurve enthielte dann genau den Effekt, den sie herausrechnen soll, und die
    gemessene Abweichung faellt zu klein aus (an synthetischen Daten mit 10 %
    Aufschlag kamen so nur gut 5 % heraus).

    Deshalb wechselseitig: Kurve fitten, daraus je Satz einen Niveaufaktor
    schaetzen, die Fahrten damit auf ein gemeinsames Niveau bringen, neu
    fitten. Nach jeder Runde werden die Faktoren km-gewichtet auf 1 normiert,
    sonst waere nur ihr Verhaeltnis bestimmt und das Niveau der Kurve triebe
    weg.

    Trennen laesst sich beides nur, wo die Baender einander UEBERLAPPEN - ohne
    Ueberlappung ist jede Aufteilung gleich gut und die Rechnung schiebt sie
    willkuerlich. Genau darauf weist `overlap_ok` hin.
    """
    trend = build_trend(points)
    if trend is None:
        return None

    factors: dict[str, float] = {}
    for _ in range(NEUTRALISATION_ROUNDS):
        levelled = [
            replace(point, consumption=point.consumption / factors.get(group_key(point), 1.0))
            for point in points
        ]
        trend = build_trend(levelled) or trend

        ratios: dict[str, list[tuple[float, float]]] = {}
        for point in points:
            expected = _expected(trend, point.temp_c)
            if expected > 0:
                ratios.setdefault(group_key(point), []).append(
                    (point.consumption / expected, point.km)
                )
        if not ratios:
            break
        factors = {key: _weighted(values) for key, values in ratios.items()}
        mean = _weighted(
            [(factors[group_key(p)], p.km) for p in points if group_key(p) in factors]
        )
        if mean > 0:
            factors = {key: value / mean for key, value in factors.items()}
    return trend


def compare(
    sessions: list[models.ChargingSession],
    tire_sets: list[models.TireSet],
    points: list[TempPoint],
) -> TireComparison:
    """Verbrauch je Reifenart und je Satz, auf eine Temperatur umgerechnet.

    `points` kommt aus `temperature.collect_points()` - dieselbe Quelle wie das
    Dashboard, damit Reifen- und Temperaturauswertung nicht unterschiedliche
    Verbrauchswerte zeigen koennen.
    """
    result = TireComparison()
    assigned, result.drives_spanning_change = sets_for_drives(sessions, tire_sets)

    compared = [p for p in points if p.session_id in assigned]
    result.drives_without_set = len(points) - len(compared) - result.drives_spanning_change
    if result.drives_without_set < 0:
        # Fahrten, die schon in der Temperaturauswertung fehlen, koennen hier
        # nicht doppelt abgezogen werden.
        result.drives_without_set = 0
    if not compared:
        return result

    # Die Referenzkurve wird ueber GENAU die verglichenen Fahrten gefittet -
    # sonst waere der Bezugspunkt der Abweichungen ein anderer als die Menge,
    # deren Abweichungen gemessen werden - und dabei um den Reifeneffekt
    # bereinigt (siehe _neutral_trend).
    trend = _neutral_trend(compared, lambda p: _signature(assigned[p.session_id]))
    if trend is not None:
        result.model = trend.model
        result.r2 = trend.r2
        result.reference_temp_c = round(
            _weighted([(p.temp_c, p.km) for p in compared]), 1
        )

    by_kind: dict[str, list[TempPoint]] = {}
    by_set: dict[str, list[TempPoint]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for point in compared:
        tire_set = assigned[point.session_id]
        by_kind.setdefault(tire_set.kind.value, []).append(point)
        signature = _signature(tire_set)
        by_set.setdefault(signature, []).append(point)
        labels.setdefault(signature, (set_label(tire_set), tire_set.kind.value))

    result.by_kind = [
        _group(kind, kind, kind, group, trend)
        for kind, group in sorted(by_kind.items())
        if len(group) >= MIN_DRIVES_PER_GROUP
    ]
    result.by_set = sorted(
        (
            _group(signature, labels[signature][0], labels[signature][1], group, trend)
            for signature, group in by_set.items()
            if len(group) >= MIN_DRIVES_PER_GROUP
        ),
        key=lambda g: (g.kind, -g.km),
    )

    if trend is not None and result.reference_temp_c is not None:
        expected_at_reference = _expected(trend, result.reference_temp_c)
        for stat in result.by_kind + result.by_set:
            if stat.delta_pct_vs_model is not None:
                stat.adjusted_consumption = round(
                    expected_at_reference * (1 + stat.delta_pct_vs_model / 100), 2
                )

    result.overlap_span_c, result.overlap_ok = _overlap(result.by_kind)

    summer = next((g for g in result.by_kind if g.kind == models.TireKind.SUMMER.value), None)
    winter = next((g for g in result.by_kind if g.kind == models.TireKind.WINTER.value), None)
    if (
        summer is not None
        and winter is not None
        and summer.adjusted_consumption
        and winter.adjusted_consumption
    ):
        result.winter_vs_summer_pct = round(
            (winter.adjusted_consumption / summer.adjusted_consumption - 1) * 100, 1
        )
    return result


def _overlap(groups: list[GroupStat]) -> tuple[float | None, bool]:
    """Gemeinsamer Temperaturbereich der Gruppen.

    Ohne ihn vergleicht die Bereinigung zwei Bereiche, die sich nie beruehrt
    haben - das Modell muesste den einen Satz in Temperaturen hochrechnen, in
    denen er nie gefahren ist. Der Wert kann negativ werden (echte Luecke
    zwischen den Bereichen); genau dann ist er am aussagekraeftigsten.
    """
    if len(groups) < 2:
        return None, False
    lower = max(g.min_temp_c for g in groups)
    upper = min(g.max_temp_c for g in groups)
    span = round(upper - lower, 1)
    return span, span >= MIN_OVERLAP_SPAN_C
