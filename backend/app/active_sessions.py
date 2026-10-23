"""Laufende Ladevorgaenge ("laedt gerade") fuer Liste und Dashboard.

Ein laufender Vorgang ist noch KEIN ChargingSession-Datensatz - der entsteht
erst beim Ausstecken. Bis dahin kommen die Angaben aus zwei Stellen:

* **Poller** (MyŠkoda, VW-Datenportal, Renault): der offene Vorgang steht in
  den `open_*`-Spalten der jeweiligen Konfiguration (siehe
  charge_detection.py). Der aktuelle SoC ist der des letzten Berichts
  (`last_soc`), `last_captured_at` der Stand - beim VW-Datenportal gern
  Stunden alt, deshalb liefert die Antwort ihn immer mit.
* **Home Assistant**: meldet den Start per `POST /api/sessions/active`
  (models.ActiveChargeReport), der Push am Ladeende raeumt ihn weg.

BMW CarData liefert nur abgeschlossene Vorgaenge und taucht hier nie auf.

Es wird nichts gespeichert und nichts korrigiert: Rueckdatierung und
Start-SoC aus der Energie (charge_detection.py) greifen erst beim Anlegen des
Vorgangs. Die Anzeige zeigt die Rohwerte - sie ist ein Zwischenstand.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models

# Eine HA-Meldung, deren Ladeende nie ankam (Automation gestoppt, Fahrzeug
# geloescht in HA), soll nicht ewig "laedt gerade" anzeigen. Laenger als drei
# Tage am Kabel ist keine Ladung mehr, sondern ein vergessener Eintrag.
HA_REPORT_MAX_AGE = timedelta(days=3)

SOURCE_HOME_ASSISTANT = "home_assistant"

_POLLER_SOURCES = (
    ("myskoda", models.MySkodaConfig),
    ("vw_data_act", models.VwDataActConfig),
    ("renault", models.RenaultConfig),
)


def _estimate_kwh(
    soc_start: int | None, soc_now: int | None, vehicle: models.Vehicle
) -> float | None:
    """SoC-Zuwachs x Akkukapazitaet, wie routers/sessions.py::estimate_energy_kwh."""
    if soc_start is None or soc_now is None or not vehicle.battery_capacity_kwh:
        return None
    delta = soc_now - soc_start
    if delta <= 0:
        return None
    return round(vehicle.battery_capacity_kwh * delta / 100, 2)


def _entry(
    vehicle: models.Vehicle,
    *,
    source: str,
    start_time: datetime | None,
    soc_start: int | None,
    soc_current: int | None,
    measured_kwh: float | None,
    charging_type: str | None,
    max_power_kw: float | None,
    odometer_km: int | None,
    outside_temp_c: float | None,
    updated_at: datetime | None,
    charging_state: str | None,
) -> dict:
    energy = measured_kwh if measured_kwh is not None else _estimate_kwh(
        soc_start, soc_current, vehicle
    )
    return {
        "vehicle_id": vehicle.id,
        "vehicle_name": vehicle.name,
        "source": source,
        "start_time": start_time,
        "charging_type": charging_type if charging_type in ("AC", "DC") else None,
        "soc_start": soc_start,
        "soc_current": soc_current,
        "energy_kwh": energy,
        "energy_is_estimated": measured_kwh is None and energy is not None,
        "max_power_kw": max_power_kw,
        "odometer_km": odometer_km,
        "outside_temp_c": outside_temp_c,
        "updated_at": updated_at,
        "charging_state": charging_state,
    }


def list_active(db: Session, user: models.User, now: datetime | None = None) -> list[dict]:
    """Alle laufenden Vorgaenge des Nutzers, neuester Beginn zuerst.

    Je Fahrzeug hoechstens einer: meldet Home Assistant UND ein Poller fuer
    dasselbe Fahrzeug (eigentlich ein Konfigurationsfehler, siehe CLAUDE.md),
    gewinnt der Poller - er hat den aktuellen SoC.
    """
    # Lokal importiert: charge_detection importiert routers/sessions.py, das
    # wiederum dieses Modul braucht.
    from .charge_detection import utc_to_local

    now_utc = now or datetime.utcnow()
    vehicles = {
        v.id: v
        for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id)
    }
    by_vehicle: dict[str, dict] = {}

    for source, config_cls in _POLLER_SOURCES:
        configs = (
            db.query(config_cls)
            .filter(
                config_cls.user_id == user.id,
                config_cls.enabled.is_(True),
                config_cls.open_start_time.isnot(None),
            )
            .all()
        )
        for config in configs:
            vehicle = vehicles.get(config.vehicle_id)
            if vehicle is None or vehicle.id in by_vehicle:
                continue
            soc_now = config.last_soc if config.last_soc is not None else config.open_soc_last
            by_vehicle[vehicle.id] = _entry(
                vehicle,
                source=source,
                # Die Zustandsmaschine rechnet in naiver UTC, die Liste in
                # Ortszeit (wie charge_detection.create_session()).
                start_time=utc_to_local(config.open_start_time),
                soc_start=config.open_soc_start,
                soc_current=soc_now,
                measured_kwh=config.open_energy_kwh,
                charging_type=config.open_charging_type,
                max_power_kw=config.open_max_power_kw,
                odometer_km=config.open_odometer_km,
                outside_temp_c=config.open_outside_temp_c,
                updated_at=(
                    utc_to_local(config.last_captured_at) if config.last_captured_at else None
                ),
                charging_state=config.last_charging_state,
            )

    reports = (
        db.query(models.ActiveChargeReport)
        .filter(models.ActiveChargeReport.user_id == user.id)
        .all()
    )
    for report in reports:
        if now_utc - report.updated_at > HA_REPORT_MAX_AGE:
            continue
        vehicle = vehicles.get(report.vehicle_id)
        if vehicle is None or vehicle.id in by_vehicle:
            continue
        by_vehicle[vehicle.id] = _entry(
            vehicle,
            source=SOURCE_HOME_ASSISTANT,
            start_time=report.start_time,
            soc_start=report.soc_start,
            soc_current=report.soc_current,
            measured_kwh=report.energy_kwh,
            charging_type=report.charging_type,
            max_power_kw=None,
            odometer_km=report.odometer_km,
            outside_temp_c=report.outside_temp_c,
            updated_at=utc_to_local(report.updated_at),
            charging_state=None,
        )

    return sorted(
        by_vehicle.values(),
        key=lambda e: e["start_time"] or datetime.min,
        reverse=True,
    )


def clear_report(db: Session, user_id: str, vehicle_id: str) -> None:
    """Raeumt die HA-Meldung eines Fahrzeugs weg (Ladeende oder Abbruch)."""
    db.query(models.ActiveChargeReport).filter(
        models.ActiveChargeReport.user_id == user_id,
        models.ActiveChargeReport.vehicle_id == vehicle_id,
    ).delete(synchronize_session=False)
