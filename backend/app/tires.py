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
from .consumption import ConsumptionResult
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


def size_label(tire_set: models.TireSet) -> str:
    """Die Groesse als EIN Text - bei Mischbereifung beide Achsen.

    Ohne Hinterachsen-Groesse gilt `size` fuer alle vier Raeder, dann steht
    sie allein da. Sonst "vorne / hinten" in genau dieser Reihenfolge; die
    Achse dazuzuschreiben waere in der Liste nur Ballast, die Reihenfolge ist
    bei Mischbereifung die uebliche Lesart.
    """
    if tire_set.size and tire_set.size_rear:
        return f"{tire_set.size} / {tire_set.size_rear}"
    return tire_set.size or tire_set.size_rear or ""


def set_label(tire_set: models.TireSet) -> str:
    """Marke, Modell und Groesse zu einer Zeile - leere Felder fallen weg."""
    parts = [part for part in (tire_set.brand, tire_set.model, size_label(tire_set)) if part]
    return " ".join(parts)


def _signature(tire_set: models.TireSet) -> str:
    """Schluessel, unter dem Wiedermontagen desselben Satzes zusammenfallen.

    Eine Zeile ist eine Montage (siehe models.TireSet) - derselbe Satz im
    naechsten Winter waere sonst eine zweite, halb so grosse Gruppe.
    """
    # Die Hinterachsen-Groesse gehoert dazu: zwei Saetze, die sich NUR darin
    # unterscheiden (einmal rundum gleich, einmal Mischbereifung), sind
    # verschiedene Reifen und duerfen nicht zu einer Gruppe verschmelzen.
    return "|".join(
        (
            tire_set.kind.value,
            tire_set.brand or "",
            tire_set.model or "",
            tire_set.size or "",
            tire_set.size_rear or "",
        )
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


@dataclass(slots=True)
class MountingStat:
    """Eine Montage mit dem, was auf ihr gefahren wurde.

    Die Zahlen beantworten "wie lange liegt der Satz schon drauf und wieviel
    hat er gesehen" - die Frage vor dem Reifenkauf, nicht die nach dem
    Verbrauch (dafuer gibt es den Vergleich).
    """

    tire_set_id: str
    vehicle_id: str
    kind: str
    label: str
    installed_on: datetime
    # Ende der Montage = der naechste Wechsel an DIESEM Fahrzeug. None, solange
    # der Satz noch draufliegt (es gibt bewusst kein Enddatum, siehe
    # models.TireSet).
    removed_on: datetime | None
    is_current: bool
    days: int
    drives: int
    km: float
    # Woher die Kilometer stammen: "odometer" = Differenz der beiden
    # Kilometerstaende (exakt, enthaelt auch die Fahrt ueber den Wechsel),
    # "drives" = Summe der zugeordneten Fahrten (laesst genau die aussen vor).
    km_source: str
    energy_kwh: float
    avg_consumption: float | None = None


@dataclass(slots=True)
class SetStat:
    """Ein Satz ueber ALLE seine Montagen hinweg - was der Reifen erlebt hat.

    Wiedermontagen fallen hier zusammen (`_signature()`), sonst waere die
    Laufleistung eines Satzes ueber zwei Winter auf zwei Zeilen verteilt und
    die Frage "wieviel km sind da drauf" nicht zu beantworten.
    """

    key: str
    label: str
    kind: str
    mountings: int
    first_installed_on: datetime
    # Tage seit der ersten Montage - der Reifen altert auch im Keller
    # (Gummi), deshalb steht das neben den Tagen, die er wirklich drauf war.
    age_days: int
    days_mounted: int
    drives: int
    km: float
    # "odometer" nur, wenn JEDE Montage dieses Satzes ihre Kilometer aus
    # Kilometerstaenden hat - sonst "drives", weil die Summe dann mindestens
    # teilweise die ungenauere Quelle enthaelt.
    km_source: str
    energy_kwh: float
    is_current: bool
    avg_consumption: float | None = None


@dataclass(slots=True)
class TireOverview:
    mountings: list[MountingStat] = field(default_factory=list)
    sets: list[SetStat] = field(default_factory=list)
    # Fahrten, die keiner Montage zugerechnet werden konnten - dieselbe Regel
    # wie beim Vergleich, damit beide Ansichten dieselben Zahlen meinen.
    drives_without_set: int = 0
    drives_spanning_change: int = 0


def _drive_km(previous: models.ChargingSession, current: models.ChargingSession) -> float | None:
    """Strecke zwischen zwei Ladevorgaengen, sofern der Kilometerstand sie hergibt."""
    if previous.odometer_km is None or current.odometer_km is None:
        return None
    km = current.odometer_km - previous.odometer_km
    return km if km > 0 else None


def overview(
    sessions: list[models.ChargingSession],
    tire_sets: list[models.TireSet],
    consumptions: dict[str, ConsumptionResult] | None = None,
    now: datetime | None = None,
) -> TireOverview:
    """Laufleistung, Dauer und Fahrten je Montage und je Satz.

    Gezaehlt werden - wie beim Vergleich - nur Fahrten, die GANZ auf einer
    Montage lagen. Die eine Fahrt ueber den Wechsel hinweg lief auf beiden
    Saetzen; sie hier dem neuen zuzuschlagen waere eine erfundene Genauigkeit,
    und sie zu halbieren erst recht. Sie wird deshalb ausgewiesen statt
    verteilt.
    """
    now = now or datetime.utcnow()
    consumptions = consumptions or {}
    assigned, spanning = sets_for_drives(sessions, tire_sets)

    drives: dict[str, int] = {}
    km: dict[str, float] = {}
    energy: dict[str, float] = {}
    consumption_pairs: dict[str, list[tuple[float, float]]] = {}

    by_vehicle: dict[str, list[models.ChargingSession]] = {}
    for session in sessions:
        by_vehicle.setdefault(session.vehicle_id, []).append(session)

    for vehicle_sessions in by_vehicle.values():
        ordered = sorted(vehicle_sessions, key=lambda s: s.start_time)
        for previous, current in zip(ordered, ordered[1:]):
            tire_set = assigned.get(current.id)
            if tire_set is None:
                continue
            key = tire_set.id
            drives[key] = drives.get(key, 0) + 1
            energy[key] = energy.get(key, 0.0) + (current.energy_kwh or 0.0)
            distance = _drive_km(previous, current)
            if distance is not None:
                km[key] = km.get(key, 0.0) + distance
            result = consumptions.get(current.id)
            if result and result.value is not None and result.km:
                consumption_pairs.setdefault(key, []).append((result.value, result.km))

    # Das Ende einer Montage ist der naechste Wechsel an DIESEM Fahrzeug.
    next_change: dict[str, datetime | None] = {}
    next_odometer: dict[str, float | None] = {}
    sets_by_vehicle: dict[str, list[models.TireSet]] = {}
    for tire_set in sorted(tire_sets, key=lambda t: t.installed_on):
        sets_by_vehicle.setdefault(tire_set.vehicle_id, []).append(tire_set)
    # Fuer den noch montierten Satz ist das Ende "jetzt" - der beste bekannte
    # Kilometerstand ist dann der des letzten Ladevorgangs.
    last_odometer: dict[str, float] = {}
    for vehicle_id, vehicle_sessions in by_vehicle.items():
        known = [s.odometer_km for s in vehicle_sessions if s.odometer_km is not None]
        if known:
            last_odometer[vehicle_id] = max(known)
    for vehicle_id, mounted in sets_by_vehicle.items():
        for current, following in zip(mounted, mounted[1:]):
            next_change[current.id] = following.installed_on
            next_odometer[current.id] = following.odometer_km
        next_change[mounted[-1].id] = None
        next_odometer[mounted[-1].id] = last_odometer.get(vehicle_id)

    result = TireOverview(drives_spanning_change=spanning)
    for tire_set in sorted(tire_sets, key=lambda t: t.installed_on, reverse=True):
        removed_on = next_change.get(tire_set.id)
        end = removed_on or now
        pairs = consumption_pairs.get(tire_set.id) or []
        # Kilometerstaende zuerst: ihre Differenz enthaelt auch die Fahrt, die
        # ueber den Wechsel hinweg lief und deshalb keinem Satz zugeordnet
        # werden kann. Nur wenn sie fehlen (oder nicht aufsteigend sind, z.B.
        # ein Tippfehler), wird ueber die Fahrten gezaehlt.
        distance = km.get(tire_set.id, 0.0)
        km_source = "drives"
        end_odometer = next_odometer.get(tire_set.id)
        if tire_set.odometer_km is not None and end_odometer is not None:
            measured = end_odometer - tire_set.odometer_km
            if measured >= 0:
                distance = measured
                km_source = "odometer"
        result.mountings.append(
            MountingStat(
                tire_set_id=tire_set.id,
                vehicle_id=tire_set.vehicle_id,
                kind=tire_set.kind.value,
                label=set_label(tire_set),
                installed_on=tire_set.installed_on,
                removed_on=removed_on,
                is_current=removed_on is None,
                days=max((end - tire_set.installed_on).days, 0),
                drives=drives.get(tire_set.id, 0),
                km=round(distance, 1),
                km_source=km_source,
                energy_kwh=round(energy.get(tire_set.id, 0.0), 2),
                avg_consumption=round(_weighted(pairs), 1) if pairs else None,
            )
        )

    grouped: dict[str, list[MountingStat]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for tire_set in tire_sets:
        signature = _signature(tire_set)
        labels.setdefault(signature, (set_label(tire_set), tire_set.kind.value))
    by_id = {m.tire_set_id: m for m in result.mountings}
    for tire_set in tire_sets:
        grouped.setdefault(_signature(tire_set), []).append(by_id[tire_set.id])

    for signature, group in grouped.items():
        pairs = [
            pair
            for mounting in group
            for pair in consumption_pairs.get(mounting.tire_set_id, [])
        ]
        first = min(m.installed_on for m in group)
        result.sets.append(
            SetStat(
                key=signature,
                label=labels[signature][0],
                kind=labels[signature][1],
                mountings=len(group),
                first_installed_on=first,
                age_days=max((now - first).days, 0),
                days_mounted=sum(m.days for m in group),
                drives=sum(m.drives for m in group),
                km=round(sum(m.km for m in group), 1),
                km_source=(
                    "odometer"
                    if all(m.km_source == "odometer" for m in group)
                    else "drives"
                ),
                energy_kwh=round(sum(m.energy_kwh for m in group), 2),
                is_current=any(m.is_current for m in group),
                avg_consumption=round(_weighted(pairs), 1) if pairs else None,
            )
        )
    # Montierte zuerst, danach nach Laufleistung - die Frage ist meistens
    # "was liegt drauf und wieviel hat es schon".
    result.sets.sort(key=lambda s: (not s.is_current, -s.km))

    total_drives = sum(1 for _ in _drive_pairs(by_vehicle))
    result.drives_without_set = max(total_drives - len(assigned) - spanning, 0)
    return result


def _drive_pairs(by_vehicle: dict[str, list[models.ChargingSession]]):
    """Alle Fahrten (Paare aufeinanderfolgender Ladevorgaenge) eines Bestands."""
    for vehicle_sessions in by_vehicle.values():
        ordered = sorted(vehicle_sessions, key=lambda s: s.start_time)
        yield from zip(ordered, ordered[1:])


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
