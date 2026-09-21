"""Rueckdatierung des Ladebeginns (myskoda_poller._backdated_start).

Die Korrektur ist der wirtschaftlich folgenreichste Rechenschritt im Poller:
an zwei echten DC-Vorgaengen (05./06.09.2026) fehlten ohne sie 17 bzw. 12 kWh,
die ueber `estimate_energy_kwh` direkt in Energie, Kosten UND
Verbrauchsstatistik durchschlagen. Gleichzeitig darf sie nicht zu weit greifen,
sonst zaehlt sie einen woanders stattgefundenen Ladevorgang diesem zu. Beide
Waechter (Alter und Physik) haben hier deshalb einen eigenen Test.

Die Werte der beiden echten Vorgaenge stehen im Modul-Docstring von
myskoda_poller.py und sind hier als Fall nachgebaut.
"""

from datetime import datetime, timedelta

from app import models
from app.myskoda_poller import _backdated_start

START = datetime(2026, 9, 5, 14, 30, 0)


def config(**overrides):
    values = dict(
        backdate_session_start=True,
        backdate_max_gap_minutes=0,     # 0 = automatisch (2x Leerlaufintervall)
        poll_interval_idle_minutes=20,
        open_start_time=START,
        open_soc_start=30,
        open_soc_before=8,
        open_gap_before_seconds=15 * 60,
        open_max_power_kw=134.0,
    )
    values.update(overrides)
    return models.MySkodaConfig(**values)


def test_real_dc_case_is_corrected_back_to_the_previous_reading():
    """Der Fall vom 05.09.2026: erkannt 30 %, tatsaechlich 8 %."""
    soc_start, start_time, reason = _backdated_start(config(), capacity_kwh=77.0)

    assert soc_start == 8
    assert start_time < START
    assert reason and "8" in reason


def test_start_time_moves_back_only_as_far_as_the_physics_allow():
    """Ohne Mitziehen der Startzeit waere die abgeleitete Durchschnittsleistung
    unphysikalisch (53 kWh in 19 min = 168 kW bei 134 kW Peak). Zurueck geht es
    um die Zeit, die das nachgetragene Delta bei der beobachteten Leistung
    braucht - und nie weiter als bis zum vorherigen Abruf."""
    _, start_time, _ = _backdated_start(config(), capacity_kwh=77.0)

    # 22 Prozentpunkte von 77 kWh = 16.94 kWh; bei 134 kW sind das ~7.6 min.
    moved_back = (START - start_time).total_seconds()
    assert 400 < moved_back < 500
    assert moved_back <= 15 * 60


def test_age_guard_rejects_a_stale_previous_reading():
    """Zu alter Abruf davor: dazwischen kann ein ganzer fremder Ladevorgang
    liegen (fahren, woanders laden, heimkommen, einstecken)."""
    stale = config(open_gap_before_seconds=10 * 3600)

    soc_start, start_time, reason = _backdated_start(stale, capacity_kwh=77.0)

    assert (soc_start, start_time, reason) == (30, START, None)


def test_physics_guard_limits_how_far_back_it_goes():
    """Bei kleiner Ladeleistung kann in der Luecke gar nicht so viel
    dazugekommen sein - dann wird nur bis zur physikalischen Untergrenze
    korrigiert, nicht bis zum alten Messwert."""
    slow = config(open_max_power_kw=11.0, open_gap_before_seconds=15 * 60)

    soc_start, _, _ = _backdated_start(slow, capacity_kwh=77.0)

    # 11 kW * 0.25 h = 2.75 kWh = 3.6 Prozentpunkte von 77 kWh.
    assert soc_start == 26
    assert soc_start > 8


def test_no_power_ever_seen_means_nothing_was_swallowed():
    never_charged = config(open_max_power_kw=0.0)

    assert _backdated_start(never_charged, capacity_kwh=77.0)[2] is None


def test_higher_value_before_means_driving_not_charging():
    """`before > soc_start` heisst: dazwischen wurde GEFAHREN. Dann ist der
    erkannte Wert der richtige - hier darf nichts korrigiert werden, sonst
    wuerde der Startwert nach OBEN verfaelscht."""
    driven = config(open_soc_before=64, open_soc_start=48)

    soc_start, start_time, reason = _backdated_start(driven, capacity_kwh=77.0)

    assert (soc_start, start_time, reason) == (48, START, None)


def test_switch_off_disables_the_correction():
    off = config(backdate_session_start=False)

    assert _backdated_start(off, capacity_kwh=77.0) == (30, START, None)


def test_without_battery_capacity_only_the_age_guard_applies():
    """Ohne hinterlegte Akkukapazitaet ist die Physik nicht berechenbar -
    dann bleibt der Messwert davor der Startwert."""
    soc_start, start_time, reason = _backdated_start(config(), capacity_kwh=None)

    assert soc_start == 8
    assert reason is not None
    # Ohne Physik kann die Startzeit nur bis zum vorherigen Abruf zurueck.
    assert (START - start_time) == timedelta(seconds=15 * 60)
