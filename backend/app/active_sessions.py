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

**Preisschaetzung (seit 0.55.0):** liegt eine Position vor (Poller:
`open_latitude/longitude`, HA: Koordinaten aus der Startmeldung) und passt
sie zu einem Ladeort mit Standardanbieter, gilt dessen letzter Preis der
Lade-Art - dieselbe Regel wie `routers/sessions.py::apply_provider_price()`
beim fertigen Vorgang. Sonst (kein Ort, kein Anbieter, kein Preis) der
Durchschnittspreis: kWh-gewichteter Saeulenpreis (Summe Gesamtpreis / Summe
kWh, wie `avg_price_per_kwh`, aber OHNE Grundgebuehren - die zeigt die Liste
auch beim fertigen Vorgang getrennt) der Vorgaenge DIESES Fahrzeugs aus den
letzten 365 Tagen, bevorzugt derselben Lade-Art; ohne solche alle Fahrzeuge.
`price_source` sagt, welcher Weg es war.

**Mehrere Quellen fuer dasselbe Fahrzeug (seit 0.55.1):** je Fahrzeug zeigt
die Liste nur die GENAUESTE Quelle, nach derselben Rangfolge wie beim
Zusammenfuehren fertiger Vorgaenge (`session_merge.TIME_SOC_RANK`: Home
Assistant vor MyŠkoda/Renault vor dem VW-Datenportal). Ausserdem verschwindet
ein offener Vorgang, sobald es fuer ihn schon einen FERTIGEN Ladevorgang gibt
(`session_merge.find_match()`, also genau der, in den die Quelle ihr Ende
spaeter einfliessen liesse): dann hat eine andere Quelle das Ladeende schon
gemeldet, und die langsamere zeigt nur noch einen veralteten Stand. Anlass:
Enyaq mit HA und VW-Datenportal (14.11.2026) - nach dem HA-Push stand der
Vorgang fertig in der Liste, darueber zeigte "laedt gerade" weiter den
Stunden alten Stand des Datenportals. Der Zustand des Pollers bleibt dabei
unberuehrt; er ergaenzt den fertigen Vorgang wie bisher, sobald er das
Ausstecken sieht.

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

# Zeitraum fuer den Durchschnittspreis: ein Jahr, damit alte Tarife nicht
# ewig mitschleppen, aber genug Vorgaenge fuer einen stabilen Wert.
AVERAGE_PRICE_WINDOW = timedelta(days=365)

PRICE_SOURCE_PROVIDER = "provider"
PRICE_SOURCE_AVERAGE = "average"

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


def _weighted_price(sessions: list[models.ChargingSession]) -> float | None:
    """Summe Gesamtpreis / Summe kWh - wie avg_price_per_kwh im Dashboard."""
    kwh = sum(s.energy_kwh for s in sessions)
    if kwh <= 0:
        return None
    return sum(s.price_total for s in sessions) / kwh


def average_price_per_kwh(
    db: Session,
    user_id: str,
    vehicle_id: str,
    charging_type: str | None,
    now: datetime,
) -> float | None:
    """Durchschnittspreis fuer die Schaetzung (Regel siehe Modul-Docstring)."""
    priced = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.user_id == user_id,
            models.ChargingSession.start_time >= now - AVERAGE_PRICE_WINDOW,
            models.ChargingSession.price_total.isnot(None),
            models.ChargingSession.energy_kwh > 0,
        )
        .all()
    )

    def of_type(sessions):
        if charging_type not in ("AC", "DC"):
            return sessions
        return [
            s for s in sessions
            if s.charging_type is not None and s.charging_type.value == charging_type
        ]

    own = [s for s in priced if s.vehicle_id == vehicle_id]
    for candidates in (of_type(own), own, of_type(priced), priced):
        price = _weighted_price(candidates)
        if price is not None:
            return price
    return None


def estimate_price(
    db: Session,
    vehicle: models.Vehicle,
    *,
    charging_type: str | None,
    energy_kwh: float | None,
    latitude: float | None,
    longitude: float | None,
    now: datetime,
) -> dict:
    """Preisschaetzung fuer einen laufenden Vorgang, siehe Modul-Docstring."""
    from .routers.locations import match_location

    location = None
    provider = None
    price = None
    source = None
    if latitude is not None and longitude is not None:
        location = match_location(db, vehicle.user_id, latitude, longitude)
        if location is not None and location.default_provider_id:
            provider = db.get(models.Provider, location.default_provider_id)
    if provider is not None:
        price = (
            provider.last_price_dc_per_kwh
            if charging_type == "DC"
            else provider.last_price_ac_per_kwh
        )
        if price is not None:
            source = PRICE_SOURCE_PROVIDER
    if price is None:
        price = average_price_per_kwh(db, vehicle.user_id, vehicle.id, charging_type, now)
        if price is not None:
            source = PRICE_SOURCE_AVERAGE
    return {
        "location_name": location.name if location is not None else None,
        "provider_name": provider.name if provider is not None else None,
        "price_per_kwh": round(price, 3) if price is not None else None,
        "price_total": (
            round(price * energy_kwh, 2)
            if price is not None and energy_kwh is not None
            else None
        ),
        "price_source": source,
    }


def _entry(
    db: Session,
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
    latitude: float | None,
    longitude: float | None,
    now: datetime,
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
        **estimate_price(
            db,
            vehicle,
            charging_type=charging_type,
            energy_kwh=energy,
            latitude=latitude,
            longitude=longitude,
            now=now,
        ),
    }


#: Quelle der Anzeige -> Schluessel in session_merge.TIME_SOC_RANK.
_MERGE_KEYS = {
    SOURCE_HOME_ASSISTANT: "ha",
    "myskoda": "myskoda",
    "renault": "renault",
    "vw_data_act": "vwda",
}


def _already_finished(db: Session, user_id: str, entry: dict) -> bool:
    """Gibt es fuer diesen offenen Vorgang schon einen fertigen Ladevorgang?"""
    from .session_merge import find_match

    if entry["start_time"] is None:
        return False
    match = find_match(db, user_id, entry["vehicle_id"], entry["start_time"], None)
    return match is not None and match.end_time is not None


def list_active(db: Session, user: models.User, now: datetime | None = None) -> list[dict]:
    """Alle laufenden Vorgaenge des Nutzers, neuester Beginn zuerst.

    Je Fahrzeug hoechstens einer, aus der genauesten Quelle; offene Vorgaenge,
    die schon als fertiger Ladevorgang vorliegen, fallen weg (siehe
    Modul-Docstring).
    """
    # Lokal importiert: charge_detection und session_merge importieren
    # routers/sessions.py, das wiederum dieses Modul braucht.
    from .charge_detection import utc_to_local
    from .session_merge import TIME_SOC_RANK

    now_utc = now or datetime.utcnow()
    now_local = utc_to_local(now_utc)
    vehicles = {
        v.id: v
        for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id)
    }
    candidates: list[dict] = []

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
            if vehicle is None:
                continue
            soc_now = config.last_soc if config.last_soc is not None else config.open_soc_last
            candidates.append(_entry(
                db,
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
                latitude=config.open_latitude,
                longitude=config.open_longitude,
                now=now_local,
            ))

    reports = (
        db.query(models.ActiveChargeReport)
        .filter(models.ActiveChargeReport.user_id == user.id)
        .all()
    )
    for report in reports:
        if now_utc - report.updated_at > HA_REPORT_MAX_AGE:
            continue
        vehicle = vehicles.get(report.vehicle_id)
        if vehicle is None:
            continue
        candidates.append(_entry(
            db,
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
            latitude=report.latitude,
            longitude=report.longitude,
            now=now_local,
        ))

    by_vehicle: dict[str, dict] = {}
    for entry in sorted(
        candidates, key=lambda e: TIME_SOC_RANK.get(_MERGE_KEYS[e["source"]], 9)
    ):
        if entry["vehicle_id"] in by_vehicle or _already_finished(db, user.id, entry):
            continue
        by_vehicle[entry["vehicle_id"]] = entry

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
