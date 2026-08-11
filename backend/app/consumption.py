"""Verbrauchsberechnung (kWh/100km) pro Ladevorgang.

Wird bei jedem Abruf frisch berechnet (nicht in der DB gespeichert), damit
nachtraegliche Korrekturen an SoC/Kilometerstand/kWh sich automatisch
korrekt auswirken. Das Fahrzeug wird fast nie vollgeladen, es gibt also
keinen festen Referenzpunkt - daher die priorisierte Fallback-Kette unten.
Siehe CLAUDE.md fuer die fachliche Herleitung.
"""

from dataclasses import dataclass

from . import models


@dataclass
class ConsumptionResult:
    value: float | None
    method: str | None


def _local_energy(
    session: models.ChargingSession,
    predecessor: models.ChargingSession,
    battery_capacity_kwh: float | None,
) -> float | None:
    """Verbrauchte Energie (kWh, noch nicht durch km geteilt) fuer einen
    Einzelvorgang - SoC-korrigiert wenn moeglich, sonst die rohe geladene
    Energie. geladene_kWh bleibt die primaere Basis, der SoC-Term ist nur
    eine Korrektur."""
    if session.energy_kwh is None:
        return None
    soc_known = session.soc_end is not None and predecessor.soc_end is not None
    if soc_known and battery_capacity_kwh is not None:
        return session.energy_kwh - battery_capacity_kwh * (
            session.soc_end - predecessor.soc_end
        ) / 100
    return session.energy_kwh


def _compute_single(
    session: models.ChargingSession,
    predecessor: models.ChargingSession | None,
    battery_capacity_kwh: float | None,
) -> ConsumptionResult:
    """Methoden 2-5: SoC-korrigierte/naive/geschaetzte Einzelvorgangs-Rechnung."""
    if predecessor is None:
        return ConsumptionResult(None, "unavailable")
    if session.odometer_km is None or predecessor.odometer_km is None:
        return ConsumptionResult(None, "unavailable")
    km_driven = session.odometer_km - predecessor.odometer_km
    if km_driven <= 0:
        return ConsumptionResult(None, "unavailable")

    energy = _local_energy(session, predecessor, battery_capacity_kwh)
    if energy is None:
        return ConsumptionResult(None, "unavailable")

    soc_known = session.soc_end is not None and predecessor.soc_end is not None
    can_correct = soc_known and battery_capacity_kwh is not None

    if session.energy_is_estimated:
        method = "estimated_energy"
    elif can_correct:
        method = "soc_corrected"
    else:
        method = "naive"

    return ConsumptionResult(round(energy / km_driven * 100, 1), method)


def _compute_interval(
    interval: list[models.ChargingSession],
    predecessor_by_id: dict[str, models.ChargingSession],
    total_kwh: float,
    km_diff: float,
    battery_capacity_kwh: float | None,
) -> dict[str, ConsumptionResult]:
    """Verteilt den exakten Intervall-Verbrauch individuell auf die
    Einzelvorgaenge: pro Vorgang eine SoC-korrigierte Einzelschaetzung,
    Abweichung zur bekannten Intervall-Summe (total_kwh) km-gewichtet auf
    alle Vorgaenge im Intervall verteilt - damit die Einzelwerte wieder
    exakt zum bekannten Gesamtwert aufsummieren, statt fuer alle denselben
    Durchschnittswert zu zeigen."""
    flat_value = round(total_kwh / km_diff * 100, 1)

    local_km: dict[str, float] = {}
    local_energy: dict[str, float] = {}
    for s in interval:
        predecessor = predecessor_by_id[s.id]
        if s.odometer_km is None or predecessor.odometer_km is None:
            break
        km = s.odometer_km - predecessor.odometer_km
        if km <= 0:
            break
        energy = _local_energy(s, predecessor, battery_capacity_kwh)
        if energy is None:
            break
        local_km[s.id] = km
        local_energy[s.id] = energy
    else:
        # Nur wenn ALLE Vorgaenge im Intervall eine lokale Einzelschaetzung
        # haben, lohnt sich die Kalibrierung - sonst bleibt der bekannte
        # Durchschnitt die sicherere Aussage
        diff = total_kwh - sum(local_energy.values())
        total_local_km = sum(local_km.values())
        return {
            s.id: ConsumptionResult(
                round(
                    (local_energy[s.id] + diff * (local_km[s.id] / total_local_km))
                    / local_km[s.id]
                    * 100,
                    1,
                ),
                "full_charge_interval",
            )
            for s in interval
        }

    return {s.id: ConsumptionResult(flat_value, "full_charge_interval") for s in interval}


def compute_vehicle_consumptions(
    sessions: list[models.ChargingSession],
    battery_capacity_kwh: float | None,
) -> dict[str, ConsumptionResult]:
    """Erwartet ALLE Sessions eines Fahrzeugs (nicht gefiltert/limitiert) -
    Vorgaenger und Vollladungs-Intervalle muessen ueber die komplette
    Historie bestimmt werden, sonst wird beim Filtern/Paginieren der
    falsche Vorgaenger herangezogen."""
    ordered = sorted(sessions, key=lambda s: s.start_time)
    predecessor_by_id = {s.id: ordered[i - 1] for i, s in enumerate(ordered) if i > 0}
    results: dict[str, ConsumptionResult] = {}

    # Methode 1: Vollladungs-zu-Vollladungs-Intervalle (Goldstandard) - gilt
    # fuer ALLE Ladevorgaenge im Intervall, nicht nur die Vollladung selbst
    anchors = [s for s in ordered if s.soc_end == 100 and s.odometer_km is not None]
    for f1, f2 in zip(anchors, anchors[1:]):
        km_diff = f2.odometer_km - f1.odometer_km
        if km_diff <= 0:
            continue
        interval = [s for s in ordered if f1.start_time < s.start_time <= f2.start_time]
        if not interval or any(s.energy_kwh is None for s in interval):
            continue
        total_kwh = sum(s.energy_kwh for s in interval)
        results.update(
            _compute_interval(interval, predecessor_by_id, total_kwh, km_diff, battery_capacity_kwh)
        )

    # Methoden 2-5: alles, was kein Vollladungs-Intervall abdeckt
    for session in ordered:
        if session.id in results:
            continue
        predecessor = predecessor_by_id.get(session.id)
        results[session.id] = _compute_single(session, predecessor, battery_capacity_kwh)

    return results
