"""Die Fallback-Kette der Verbrauchsberechnung (consumption.py).

Reine Funktionstests ohne Datenbank: `compute_vehicle_consumptions()` bekommt
Objekte und gibt Werte zurueck. Getestet wird jede der fuenf Methoden an einem
Fall, in dem sie greifen MUSS - und mindestens einer, in dem sie es nicht darf.
Die fachliche Herleitung steht in CLAUDE.md, hier steht nur, was daraus folgt.
"""

from datetime import datetime, timedelta

import pytest

from app import models
from app.consumption import compute_vehicle_consumptions

BASE = datetime(2026, 1, 1, 8, 0, 0)


def session(idx, *, soc_end=None, odo=None, kwh=None, estimated=False):
    """Baut ein ChargingSession-Objekt, ohne es in die DB zu schreiben -
    compute_vehicle_consumptions() liest ausschliesslich Attribute."""
    return models.ChargingSession(
        id=f"s{idx}",
        vehicle_id="v1",
        start_time=BASE + timedelta(days=idx),
        soc_end=soc_end,
        odometer_km=odo,
        energy_kwh=kwh,
        energy_is_estimated=estimated,
    )


def test_without_predecessor_nothing_can_be_computed():
    results = compute_vehicle_consumptions([session(0, soc_end=80, odo=1000, kwh=40)], 77.0)

    assert results["s0"].value is None
    assert results["s0"].method == "unavailable"


def test_naive_without_soc_or_capacity():
    """Nur geladene kWh und Kilometerstaende bekannt: einfache Rechnung ohne
    SoC-Korrektur - und ausdruecklich als `naive` markiert."""
    sessions = [session(0, odo=1000, kwh=30), session(1, odo=1200, kwh=40)]

    result = compute_vehicle_consumptions(sessions, None)["s1"]

    assert result.method == "naive"
    assert result.value == pytest.approx(20.0)  # 40 kWh auf 200 km
    assert result.km == 200


def test_soc_corrected_subtracts_the_battery_delta():
    """Der SoC-Term ist eine KORREKTUR, nicht die Groessenordnung: endet der
    Vorgang mit mehr Ladung im Akku als der vorige, ist entsprechend weniger
    der geladenen Energie tatsaechlich gefahren worden."""
    sessions = [
        session(0, soc_end=50, odo=1000, kwh=30),
        session(1, soc_end=60, odo=1200, kwh=40),
    ]

    result = compute_vehicle_consumptions(sessions, 77.0)["s1"]

    # 40 kWh - 77 kWh * (60-50)/100 = 32.3 kWh auf 200 km -> 16.15, auf eine
    # Nachkommastelle gerundet wie alles, was die Oberflaeche anzeigt.
    assert result.method == "soc_corrected"
    assert result.value == pytest.approx(16.15, abs=0.06)


def test_estimated_energy_gets_its_own_method_tag():
    """Gleiche Rechnung wie soc_corrected, aber eigener Tag - eine Schaetzung
    aus dem SoC-Delta in eine Verbrauchsrechnung zu stecken potenziert den
    Fehler, und das soll die Oberflaeche kennzeichnen koennen."""
    sessions = [
        session(0, soc_end=50, odo=1000, kwh=30),
        session(1, soc_end=60, odo=1200, kwh=40, estimated=True),
    ]

    assert compute_vehicle_consumptions(sessions, 77.0)["s1"].method == "estimated_energy"


def test_full_charge_interval_covers_every_session_in_between():
    """Goldstandard: zwischen zwei Vollladungen ist die Summe exakt bekannt -
    und gilt fuer ALLE Vorgaenge im Intervall, nicht nur fuer die Vollladung."""
    sessions = [
        session(0, soc_end=100, odo=1000, kwh=20),
        session(1, soc_end=60, odo=1300, kwh=30),
        session(2, soc_end=100, odo=1500, kwh=40),
    ]

    results = compute_vehicle_consumptions(sessions, 77.0)

    assert results["s1"].method == "full_charge_interval"
    assert results["s2"].method == "full_charge_interval"
    # Die Einzelwerte muessen wieder exakt die bekannte Intervall-Summe ergeben
    # (70 kWh auf 500 km = 14.0 kWh/100km im Mittel).
    total = sum(results[key].value * results[key].km / 100 for key in ("s1", "s2"))
    assert total == pytest.approx(70.0, abs=0.1)


def test_falling_odometer_is_not_a_consumption():
    """Ein Kilometerstand, der nicht steigt (Tippfehler, Fahrzeugwechsel),
    darf keine Zahl liefern - eine negative oder unendliche Strecke waere
    schlimmer als gar kein Wert."""
    sessions = [session(0, odo=1500, kwh=30), session(1, odo=1200, kwh=40)]

    assert compute_vehicle_consumptions(sessions, 77.0)["s1"].method == "unavailable"


def test_missing_energy_yields_unavailable():
    sessions = [session(0, odo=1000, kwh=30), session(1, odo=1200, kwh=None)]

    assert compute_vehicle_consumptions(sessions, 77.0)["s1"].method == "unavailable"


def test_order_comes_from_start_time_not_input_order():
    """Die Funktion bekommt die Liste so, wie die Datenbank sie liefert -
    die Reihenfolge muss sie selbst herstellen, sonst waere der Vorgaenger
    ein beliebiger anderer Vorgang."""
    ordered = [session(0, odo=1000, kwh=30), session(1, odo=1200, kwh=40)]

    forward = compute_vehicle_consumptions(ordered, None)
    backward = compute_vehicle_consumptions(list(reversed(ordered)), None)

    assert forward["s1"].value == backward["s1"].value
