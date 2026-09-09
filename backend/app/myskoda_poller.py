"""Automatische Ladeerkennung ueber die MyŠkoda Public API.

Zweite Quelle fuer automatisch erfasste Ladevorgaenge, parallel zum
bestehenden Home-Assistant-Push auf `/api/sessions/auto` - beide legen
`source=AUTOMATIC`-Vorgaenge mit `needs_review=True` an und teilen sich
saemtliche Nachbearbeitung (Ladeort-Matching, Reverse-Geocoding,
kWh-Schaetzung aus dem SoC-Delta, Anbieterpreis) aus `routers/sessions.py`.

## Warum ueberhaupt eine Zustandsmaschine im Server?

Die Public API kennt keinen Push und keinen Endpunkt fuer Ladehistorie. Es
gibt genau einen lesenden Aufruf, der den aktuellen Zustand liefert - ein
Ladevorgang entsteht erst dadurch, dass jemand diesen Zustand wiederholt
abfragt und die Uebergaenge zusammensetzt. Genau das macht sonst die
HA-Automation; hier macht es der Server selbst.

## Schnitt eines Ladevorgangs

Identisch zum HA-Blueprint, damit beide Quellen dasselbe unter einem
"Ladevorgang" verstehen: Beginn = Uebergang "Kabel nicht verbunden" ->
"Kabel verbunden", Ende = der umgekehrte Uebergang. Die Phasen dazwischen,
in denen gerade kein Strom fliesst (Timer laeuft, Zielladestand erreicht,
Ladung unterbrochen), gehoeren mit zum Vorgang.

## Bekannte Ungenauigkeiten (bewusst so, nicht uebersehen)

- **Der Ladebeginn wird erst beim naechsten Abruf bemerkt.** Der Ladevorgang
  hat dann schon bis zu ein Abfrageintervall lang gelaufen. Bei AC-Laden ist
  das vernachlaessigbar (11 kW * 5 min ~ 1 kWh), bei DC-Schnellladen nicht
  (150 kW * 5 min ~ 12 kWh). Dagegen steht die Rueckdatierung, siehe unten.
- **Ein schlafendes Fahrzeug kann einen ganzen Vorgang verstecken.** Dagegen
  steht die Nacherkennung ueber einen SoC-Sprung (`detect_missed_sessions`).
- **Alle Zeitstempel stammen wenn moeglich aus `carCapturedTimestamp`**, nicht
  aus der lokalen Uhr - die Antwort kann ein aelterer Cloud-Stand sein.

## Rueckdatierung des Ladebeginns (`_backdated_start`)

Bis 2026-09 war `soc_start` immer der SoC beim ERSTEN Abruf, der "steckt"
meldet - mit der Begruendung, der SoC des vorherigen Abrufs waere bei einer
Fahrt zwischen den Abrufen "falsch in die andere Richtung". Das war nur zur
Haelfte richtig und ist bewusst revidiert:

Fahren SENKT den SoC. Solange nicht geladen wird, ist der SoC monoton
fallend, also gilt immer `soc_echt <= min(open_soc_before, open_soc_start)`.
Der vorherige Wert kann damit nie unter dem echten Startwert liegen - er ist
schlimmstenfalls (lange Fahrt dazwischen) zu wenig korrigiert, nie zu viel.
Der einzige Fall, in dem er wirklich in die falsche Richtung zeigt, ist
`open_soc_before > open_soc_start` (viel gefahren, dann DC gestartet) - und
der ist trivial erkennbar.

Belegt an zwei DC-Vorgaengen im Debug-Log (05./06.09.2026): erkannt wurden
30 %->77 % bzw. 64 %->80 %, tatsaechlich waren es 8 %->77 % und 48 %->80 %.
Der jeweils letzte Abruf davor stand punktgenau auf 8 % bzw. 48 % - beim
zweiten, obwohl dazwischen noch 5 km gefahren wurden (der 15 min alte Wert
enthielt die Fahrt bereits). Verschluckt wurden so 17 bzw. 12 kWh, was ueber
`estimate_energy_kwh` direkt auf Energie, Kosten und Verbrauchsstatistik
durchschlaegt.

`soc_start` ist deshalb jetzt `open_soc_before`, aber nur unter zwei
Waechtern - ohne sie waere ein komplett unbemerkter Ladevorgang zwischen den
Abrufen (fahren, woanders laden, heimkommen, einstecken) als Zuwachs DIESES
Vorgangs gezaehlt worden:

1. **Alter.** Nur wenn der vorherige Abruf hoechstens
   `backdate_max_gap_minutes` zurueckliegt (Default: das Doppelte des
   Leerlaufintervalls). Ist er aelter, kann dazwischen zu viel passiert sein.
2. **Physik.** Aus `open_max_power_kw` und `vehicle.battery_capacity_kwh`
   ergibt sich, wie viele Prozentpunkte in der Luecke ueberhaupt geladen
   worden sein KOENNEN - weiter zurueck als diese Untergrenze wird nicht
   korrigiert. Ohne hinterlegte Akkukapazitaet greift dieser Waechter nicht,
   dann bleibt nur der Alterswaechter.

Die **Startzeit** wird aus derselben Physik mitgezogen, sonst wuerde die
abgeleitete Durchschnittsleistung unphysikalisch (05.09.: 53 kWh in den
gemessenen 19 min waeren 168 kW avg bei 134 kW Peak). Zurueck geht es um die
Zeit, die das nachgetragene SoC-Delta bei der beobachteten Leistung braucht,
hoechstens aber bis zum vorherigen Abruf.

Was NICHT passiert: eine Korrektur um die Fahrstrecke zwischen den Abrufen
(`odometer`). Beim DC-Vorgang vom 06.09. lag der 15 min alte SoC trotz 5 km
Fahrt exakt richtig - die Fahrt steckte schon drin. Abziehen wuerde
ueberkorrigieren; der Kilometerstand dient nur der Diagnose in der Notiz.

Rueckdatierter Start und Rohwerte stehen weiterhin beide in der Notiz des
Ladevorgangs und im Debug-Log, und die Vorgaenge bleiben `needs_review`.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, notifications
from .database import SessionLocal
from .myskoda import (
    DISCHARGING_STATES,
    MySkodaAuthError,
    MySkodaClient,
    MySkodaError,
    MySkodaRateLimitError,
    VehicleSnapshot,
)
from .routers.sessions import (
    apply_provider_price,
    estimate_energy_kwh,
    resolve_location,
)

logger = logging.getLogger(__name__)

#: Wie oft der Scheduler nachsieht, ob ein Fahrzeug faellig ist. Deutlich
#: feiner als beim WebDAV-Backup (15 min), weil das aktive Intervall waehrend
#: eines Ladevorgangs bei wenigen Minuten liegt.
SCHEDULER_INTERVAL_SECONDS = 60

#: Zeilen pro Fahrzeug, danach werden die aeltesten geloescht.
LOG_MAX_ENTRIES = 500

#: So viele Anfragen des Stundenkontingents bleiben unangetastet, damit eine
#: manuelle Abfrage oder ein paralleler Client nicht ins Leere laeuft.
RATE_LIMIT_RESERVE = 2

#: Ein abgelehnter Key wird nicht im Minutentakt erneut probiert - er wird erst
#: wieder gueltig, wenn jemand in der MyŠkoda-App einen neuen erzeugt.
AUTH_ERROR_BACKOFF_MINUTES = 60
#: Netz-/Serverfehler: zuegig erneut versuchen, aber nicht im Sekundentakt.
ERROR_BACKOFF_MINUTES = 15

#: Zurueckdatiert wird nur, wenn der letzte Abruf vor dem Einstecken hoechstens
#: dieses Vielfache des Leerlaufintervalls zurueckliegt - sofern die Konfiguration
#: kein festes Fenster vorgibt (`backdate_max_gap_minutes = 0`). Zwei Intervalle,
#: damit ein einzelner ausgefallener Abruf die Korrektur nicht sofort verhindert.
BACKDATE_GAP_INTERVAL_FACTOR = 2


# --------------------------------------------------------------------------
# Debug-Log
# --------------------------------------------------------------------------

def log_event(
    db: Session,
    config: models.MySkodaConfig,
    event: str,
    message: str,
    *,
    level: str = "info",
    snapshot: VehicleSnapshot | None = None,
    force: bool = False,
) -> None:
    """Schreibt eine Zeile ins Debug-Protokoll.

    `force` schreibt auch bei abgeschaltetem Log - genutzt fuer die Ereignisse,
    die man auch ohne Debug-Absicht sehen will (angelegter/verworfener
    Ladevorgang, Fehler).
    """
    if not config.log_enabled and not force:
        return

    entry = models.MySkodaLogEntry(
        user_id=config.user_id,
        vehicle_id=config.vehicle_id,
        level=level,
        event=event,
        message=message,
    )
    if snapshot is not None:
        entry.charging_state = snapshot.charging_state
        entry.soc_percent = snapshot.soc_percent
        entry.charge_power_kw = snapshot.charge_power_kw
        entry.captured_at = snapshot.captured_at
        if config.log_raw_payload:
            entry.payload = json.dumps(
                {"vehicle": snapshot.raw, "errors": snapshot.errors, "summary": snapshot.summary()},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
    db.add(entry)
    db.flush()
    _prune_log(db, config.vehicle_id)


def _prune_log(db: Session, vehicle_id: str) -> None:
    """Begrenzt das Protokoll pro Fahrzeug auf LOG_MAX_ENTRIES Zeilen."""
    old_ids = [
        row.id
        for row in db.query(models.MySkodaLogEntry.id)
        .filter(models.MySkodaLogEntry.vehicle_id == vehicle_id)
        .order_by(models.MySkodaLogEntry.created_at.desc())
        .offset(LOG_MAX_ENTRIES)
        .all()
    ]
    if old_ids:
        db.query(models.MySkodaLogEntry).filter(
            models.MySkodaLogEntry.id.in_(old_ids)
        ).delete(synchronize_session=False)


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _charging_type(value: str | None) -> models.ChargingType | None:
    """"AC"/"DC" -> Enum. Alles andere (u.a. "OFF", das die API beim
    Nicht-Laden liefert) wird zu None, statt den Vorgang abzulehnen."""
    if not value:
        return None
    upper = value.upper()
    return models.ChargingType(upper) if upper in ("AC", "DC") else None


def _fmt_number(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "unbekannt"
    return f"{value:.{decimals}f}".replace(".", ",")


def _fmt_minutes(seconds: int | None) -> str:
    if seconds is None:
        return "unbekannt"
    minutes = round(seconds / 60)
    return f"{minutes} min"


def _backdate_limit_seconds(config: models.MySkodaConfig) -> int:
    """Wie alt der letzte Abruf vor dem Einstecken hoechstens sein darf, damit
    sein SoC noch als Startwert taugt."""
    configured = config.backdate_max_gap_minutes or 0
    if configured > 0:
        return configured * 60
    return BACKDATE_GAP_INTERVAL_FACTOR * max(1, config.poll_interval_idle_minutes) * 60


def _backdated_start(
    config: models.MySkodaConfig, capacity_kwh: float | None
) -> tuple[int | None, datetime | None, str | None]:
    """Korrigierter Ladebeginn: `(soc_start, start_time, Begruendung)`.

    Ohne Korrektur kommen die unveraenderten `open_*`-Werte zurueck und die
    Begruendung ist None. Ausfuehrliche Herleitung im Modul-Docstring,
    Abschnitt "Rueckdatierung des Ladebeginns".
    """
    soc_start = config.open_soc_start
    start_time = config.open_start_time
    before = config.open_soc_before
    gap = config.open_gap_before_seconds

    if not config.backdate_session_start:
        return soc_start, start_time, None
    if before is None or soc_start is None or gap is None or start_time is None:
        return soc_start, start_time, None
    if before >= soc_start:
        # Kein Zuwachs verschluckt. Ein HOEHERER Wert davor heisst: dazwischen
        # wurde gefahren, nicht geladen - dann ist der erkannte Wert der
        # richtige und der vorherige der falsche.
        return soc_start, start_time, None

    limit = _backdate_limit_seconds(config)
    if gap > limit:
        return soc_start, start_time, None

    # Physik-Waechter: mehr als das kann in der Luecke nicht geladen worden
    # sein. Ohne Akkukapazitaet am Fahrzeug nicht berechenbar - dann bleibt es
    # beim reinen Alterswaechter oben.
    power = config.open_max_power_kw or 0.0
    floor = float(before)
    if capacity_kwh and power > 0:
        max_gain_pct = power * (gap / 3600) / capacity_kwh * 100
        floor = max(floor, soc_start - max_gain_pct)
    elif capacity_kwh and power <= 0:
        # Nie Leistung gesehen -> es kann auch nichts verschluckt worden sein.
        return soc_start, start_time, None

    corrected = int(round(floor))
    if corrected >= soc_start:
        return soc_start, start_time, None

    # Startzeit aus derselben Physik mitziehen: sonst wird die aus Energie und
    # Dauer abgeleitete Durchschnittsleistung unphysikalisch. Hoechstens bis
    # zum vorherigen Abruf zurueck - davor stand das Kabel nachweislich nicht.
    offset = float(gap)
    if capacity_kwh and power > 0:
        hours = (soc_start - corrected) / 100 * capacity_kwh / power
        offset = min(offset, hours * 3600)
    # Auf ganze Sekunden, wie alle uebrigen Zeitstempel aus
    # `carCapturedTimestamp` - die Startzeit landet u.a. in der
    # `external_session_id` und in der Anzeige.
    corrected_time = (start_time - timedelta(seconds=offset)).replace(microsecond=0)

    reason = (
        f"Ladebeginn zurückdatiert: Startwert {soc_start} % → {corrected} % "
        f"(letzter Abruf davor vor {_fmt_minutes(gap)} mit {before} %), "
        f"Startzeit um {_fmt_minutes(int(offset))} vorgezogen."
    )
    return corrected, corrected_time, reason


def _reset_open_session(config: models.MySkodaConfig) -> None:
    config.open_start_time = None
    config.open_soc_start = None
    config.open_soc_before = None
    config.open_soc_last = None
    config.open_charging_type = None
    config.open_max_power_kw = None
    config.open_odometer_km = None
    config.open_latitude = None
    config.open_longitude = None
    config.open_poll_count = 0
    config.open_gap_before_seconds = None
    config.open_in_saved_location = None


def _absorb_meta(config: models.MySkodaConfig, snapshot: VehicleSnapshot) -> None:
    """Uebernimmt Kontingent- und Ablaufinfos aus einer Antwort."""
    if snapshot.rate_limit.limit is not None:
        config.rate_limit_limit = snapshot.rate_limit.limit
    if snapshot.rate_limit.remaining is not None:
        config.rate_limit_remaining = snapshot.rate_limit.remaining
    resets_at = snapshot.rate_limit.resets_at
    if resets_at is not None:
        config.rate_limit_resets_at = resets_at
    if snapshot.key_expires_at is not None:
        config.api_key_expires_at = snapshot.key_expires_at


def _schedule_next(config: models.MySkodaConfig, *, active: bool) -> None:
    """Setzt den naechsten Abfragezeitpunkt - adaptiv und mit Ruecksicht auf
    das Restkontingent des API-Keys."""
    now = datetime.utcnow()
    minutes = (
        config.poll_interval_active_minutes if active else config.poll_interval_idle_minutes
    )
    next_at = now + timedelta(minutes=max(1, minutes))

    # Kontingent fast aufgebraucht: bis zur Freigabe warten, statt in ein 429
    # zu laufen. Die Reserve bleibt fuer manuelle Abfragen uebrig.
    if (
        config.rate_limit_remaining is not None
        and config.rate_limit_remaining <= RATE_LIMIT_RESERVE
        and config.rate_limit_resets_at
        and config.rate_limit_resets_at > next_at
    ):
        next_at = config.rate_limit_resets_at + timedelta(seconds=5)

    config.next_poll_at = next_at


# --------------------------------------------------------------------------
# Ladevorgang anlegen
# --------------------------------------------------------------------------

def _create_session(
    db: Session,
    config: models.MySkodaConfig,
    *,
    start_time: datetime,
    end_time: datetime,
    soc_start: int | None,
    soc_end: int | None,
    charging_type: models.ChargingType | None,
    odometer_km: int | None,
    latitude: float | None,
    longitude: float | None,
    notes: str,
) -> models.ChargingSession | None:
    """Legt den Ladevorgang an - mit derselben Nachbearbeitung wie der
    Home-Assistant-Push (`routers/sessions.py::push_auto_session`).

    Gibt None zurueck, wenn zu diesem Start bereits ein Vorgang existiert. Die
    `external_session_id` enthaelt die Startzeit, ist also stabil ueber
    Neustarts hinweg - ein zweimal verarbeiteter Ladevorgang legt keine
    Dublette an.
    """
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    if not vehicle:
        return None

    # Vehicle.id (interne UUID) statt config.vin (siehe crypto.py) - die VIN
    # ist verschluesselt genau WEIL sie personenbezogen ist, waere hier aber
    # wieder im Klartext gelandet: external_session_id braucht per Dubletten-
    # check einen exakten SQL-Gleichheitsvergleich (siehe unten) und kann
    # deshalb selbst nicht verschluesselt werden. Vehicle.id erfuellt densel-
    # ben Zweck (pro Fahrzeug eindeutig) ohne die VIN preiszugeben.
    external_id = f"myskoda-{vehicle.id}-{start_time.strftime('%Y%m%dT%H%M%S')}"
    existing = (
        db.query(models.ChargingSession)
        .filter(
            models.ChargingSession.external_session_id == external_id,
            models.ChargingSession.user_id == config.user_id,
        )
        .first()
    )
    if existing:
        return None

    session = models.ChargingSession(
        vehicle_id=vehicle.id,
        user_id=config.user_id,
        start_time=start_time,
        end_time=end_time,
        charging_type=charging_type,
        soc_start=soc_start,
        soc_end=soc_end,
        odometer_km=odometer_km,
        latitude=latitude,
        longitude=longitude,
        external_session_id=external_id,
        source=models.SessionSource.AUTOMATIC,
        needs_review=True,
        notes=notes,
    )

    resolve_location(session, config.user_id, db)
    estimate_energy_kwh(session, vehicle)
    apply_provider_price(session, db)

    db.add(session)
    return session


# --------------------------------------------------------------------------
# Zustandsmaschine
# --------------------------------------------------------------------------

def _start_open_session(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> None:
    """Uebergang "nicht verbunden" -> "verbunden"."""
    gap_seconds = None
    if config.last_captured_at:
        gap_seconds = max(0, int((snapshot.captured_at - config.last_captured_at).total_seconds()))

    config.open_start_time = snapshot.captured_at
    config.open_soc_start = snapshot.soc_percent
    config.open_soc_before = config.last_soc
    config.open_soc_last = snapshot.soc_percent
    config.open_charging_type = snapshot.charge_type
    config.open_max_power_kw = snapshot.charge_power_kw
    config.open_odometer_km = snapshot.odometer_km
    config.open_poll_count = 1
    config.open_gap_before_seconds = gap_seconds
    config.open_in_saved_location = snapshot.is_in_saved_location
    coords = snapshot.coordinates
    if coords:
        config.open_latitude, config.open_longitude = coords

    log_event(
        db,
        config,
        "session_started",
        f"Ladevorgang begonnen (Zustand {snapshot.charging_state}), SoC {snapshot.soc_percent}%, "
        f"letzter Abruf davor vor {_fmt_minutes(gap_seconds)} mit SoC "
        f"{config.open_soc_before if config.open_soc_before is not None else 'unbekannt'}%.",
        snapshot=snapshot,
        force=True,
    )


def _update_open_session(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> None:
    """Laufender Vorgang - Maximalwerte und zuletzt bekannte Angaben mitfuehren."""
    config.open_poll_count = (config.open_poll_count or 0) + 1

    if snapshot.soc_percent is not None:
        # max(), nicht einfach ueberschreiben: faellt der SoC waehrend des
        # Steckens (Vorklimatisierung aus der Batterie), soll der erreichte
        # Hoechststand den Ladevorgang beschreiben.
        config.open_soc_last = max(config.open_soc_last or 0, snapshot.soc_percent)

    power = snapshot.charge_power_kw
    if power is not None:
        config.open_max_power_kw = max(config.open_max_power_kw or 0.0, power)

    # Die Lade-Art steht erst fest, sobald wirklich Strom fliesst - beim ersten
    # Abruf direkt nach dem Einstecken meldet die API oft noch "OFF".
    if _charging_type(config.open_charging_type) is None:
        resolved = _charging_type(snapshot.charge_type)
        if resolved:
            config.open_charging_type = resolved.value

    if snapshot.odometer_km is not None:
        config.open_odometer_km = snapshot.odometer_km

    # Position nur uebernehmen, solange sie zum Ladeort gehoert: beim Ausstecken
    # kann das Fahrzeug schon wieder unterwegs sein (parking_state IN_MOTION,
    # dann liefert die API ohnehin keine Koordinaten).
    coords = snapshot.coordinates
    if coords:
        config.open_latitude, config.open_longitude = coords


def _finish_open_session(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> None:
    """Uebergang "verbunden" -> "nicht verbunden": Ladevorgang abschliessen."""
    # Den vom Abfrageintervall verschluckten Anfang nachtragen, bevor irgendwas
    # daraus abgeleitet wird (Dauer, SoC-Delta, external_session_id).
    vehicle = db.get(models.Vehicle, config.vehicle_id)
    soc_start, backdated_time, backdate_reason = _backdated_start(
        config, vehicle.battery_capacity_kwh if vehicle else None
    )

    start_time = backdated_time or config.open_start_time or snapshot.captured_at
    end_time = snapshot.captured_at
    if end_time <= start_time:
        # Kann passieren, wenn die API einen aelteren Cloud-Stand liefert als
        # beim Ladebeginn - dann ist die lokale Uhr die ehrlichere Angabe.
        end_time = max(snapshot.fetched_at, start_time + timedelta(minutes=1))

    soc_end = config.open_soc_last
    if snapshot.soc_percent is not None:
        soc_end = max(soc_end or 0, snapshot.soc_percent)

    if backdate_reason:
        log_event(db, config, "session_backdated", backdate_reason, snapshot=snapshot)

    delta = (soc_end - soc_start) if (soc_start is not None and soc_end is not None) else None
    max_power = config.open_max_power_kw or 0.0

    # Eingesteckt, aber nie geladen (Zielladestand war schon erreicht, oder das
    # Kabel steckte nur kurz): kein Ladevorgang, sondern Rauschen.
    if (delta is None or delta <= 0) and max_power <= 0:
        log_event(
            db,
            config,
            "session_discarded",
            f"Kabel war verbunden, aber es wurde nichts geladen (SoC {soc_start}% -> {soc_end}%, "
            f"max. Leistung {_fmt_number(max_power)} kW) - kein Ladevorgang angelegt.",
            level="warning",
            snapshot=snapshot,
            force=True,
        )
        _reset_open_session(config)
        return

    notes = _detection_note(config, delta, backdate_reason)
    session = _create_session(
        db,
        config,
        start_time=start_time,
        end_time=end_time,
        soc_start=soc_start,
        soc_end=soc_end,
        charging_type=_charging_type(config.open_charging_type),
        odometer_km=snapshot.odometer_km or config.open_odometer_km,
        latitude=config.open_latitude,
        longitude=config.open_longitude,
        notes=notes,
    )

    duration_min = round((end_time - start_time).total_seconds() / 60)
    if session is None:
        log_event(
            db,
            config,
            "session_duplicate",
            f"Ladevorgang mit diesem Beginn existiert bereits - nicht erneut angelegt "
            f"(SoC {soc_start}% -> {soc_end}%).",
            level="warning",
            snapshot=snapshot,
            force=True,
        )
    else:
        log_event(
            db,
            config,
            "session_finished",
            f"Ladevorgang angelegt: SoC {soc_start}% -> {soc_end}%, Dauer {duration_min} min, "
            f"max. {_fmt_number(max_power)} kW, Lade-Art "
            f"{config.open_charging_type or 'unbekannt'}, {config.open_poll_count} Abfragen.",
            snapshot=snapshot,
            force=True,
        )

    _reset_open_session(config)


def _detection_note(
    config: models.MySkodaConfig, delta: int | None, backdate_reason: str | None = None
) -> str:
    """Diagnose-Notiz am Ladevorgang.

    Bewusst am Datensatz selbst und nicht nur im Log: beim Nachbearbeiten
    (needs_review) ist genau das die Information, mit der sich beurteilen
    laesst, wie zuverlaessig die automatisch erfassten Werte sind. Deshalb
    stehen hier immer die ROHWERTE beider Abrufe, auch wenn zurueckdatiert
    wurde - sonst laesst sich die Korrektur nachtraeglich nicht mehr pruefen.
    """
    lines = ["Automatisch erkannt über die MyŠkoda Public API."]

    if config.open_soc_before is not None and config.open_soc_start is not None:
        missed = config.open_soc_before - config.open_soc_start
        lines.append(
            f"SoC beim letzten Abruf vor dem Einstecken: {config.open_soc_before} % "
            f"(vor {_fmt_minutes(config.open_gap_before_seconds)}), "
            f"beim ersten Abruf mit steckendem Kabel: {config.open_soc_start} %."
        )
        if backdate_reason:
            # Nicht aus `missed` neu gerechnet: der Physik-Waechter kann weniger
            # als die volle Differenz nachgetragen haben, `backdate_reason`
            # nennt die tatsaechlich angesetzten Werte.
            lines.append(backdate_reason)
        elif missed < 0:
            lines.append(
                f"Achtung: dazwischen wurden bereits {abs(missed)} Prozentpunkte geladen, "
                "die in diesem Vorgang fehlen (Abfrageintervall)."
            )

    lines.append(
        f"Max. beobachtete Ladeleistung: {_fmt_number(config.open_max_power_kw)} kW, "
        f"{config.open_poll_count} Abfragen während des Vorgangs."
    )
    if delta is not None:
        lines.append(f"Erfasstes SoC-Delta: {delta} Prozentpunkte.")
    if config.open_in_saved_location is not None:
        lines.append(
            "Fahrzeug stand beim Einstecken an einem in der MyŠkoda-App gespeicherten Ort."
            if config.open_in_saved_location
            else "Fahrzeug stand beim Einstecken nicht an einem in der MyŠkoda-App "
            "gespeicherten Ort."
        )
    return "\n".join(lines)


def _detect_missed_session(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> bool:
    """Nacherkennung ueber einen SoC-Sprung.

    Greift nur, wenn zwischen zwei Abrufen gar kein "verbunden"-Zustand
    gesehen wurde - typischerweise weil das Fahrzeug geschlafen hat und die
    API zwischenzeitlich einen alten Stand ausgeliefert hat. Ergebnis ist
    zwangslaeufig grob (Start- und Endzeit sind nur die beiden Abrufe), aber
    besser als ein fehlender Vorgang; `needs_review` faengt das ab.
    """
    if not config.detect_missed_sessions:
        return False
    if config.last_soc is None or snapshot.soc_percent is None:
        return False
    if config.last_captured_at is None:
        return False

    delta = snapshot.soc_percent - config.last_soc
    if delta < max(1, config.missed_session_min_soc_delta):
        return False

    start_time = config.last_captured_at
    end_time = snapshot.captured_at
    if end_time <= start_time:
        return False

    notes = (
        "Automatisch nacherkannt über die MyŠkoda Public API.\n"
        f"Zwischen zwei Abfragen ist der SoC von {config.last_soc} % auf "
        f"{snapshot.soc_percent} % gestiegen, ohne dass ein Ladezustand beobachtet wurde "
        "(Fahrzeug hat vermutlich geschlafen).\n"
        "Start- und Endzeit sind deshalb nur die Zeitpunkte der beiden Abfragen, "
        "die Lade-Art ist unbekannt."
    )
    session = _create_session(
        db,
        config,
        start_time=start_time,
        end_time=end_time,
        soc_start=config.last_soc,
        soc_end=snapshot.soc_percent,
        charging_type=None,
        odometer_km=snapshot.odometer_km,
        latitude=snapshot.coordinates[0] if snapshot.coordinates else None,
        longitude=snapshot.coordinates[1] if snapshot.coordinates else None,
        notes=notes,
    )
    log_event(
        db,
        config,
        "missed_session",
        f"SoC-Sprung {config.last_soc}% -> {snapshot.soc_percent}% ohne beobachteten "
        + (
            "Ladezustand - Ladevorgang nachtraeglich angelegt."
            if session is not None
            else "Ladezustand - Vorgang existierte bereits."
        ),
        level="warning",
        snapshot=snapshot,
        force=True,
    )
    return session is not None


def _apply_snapshot(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> None:
    """Verarbeitet eine Antwort: Uebergaenge erkennen, Zustand fortschreiben."""
    state = snapshot.charging_state
    plugged_in = snapshot.is_plugged_in
    had_open_session = config.open_start_time is not None

    if state is None:
        # Kein Ladeteil in der Antwort (Fahrzeug ohne Ladeunterstuetzung, oder
        # der Teil war gerade nicht abrufbar). Nichts entscheiden, sonst wuerde
        # ein laufender Vorgang faelschlich beendet.
        log_event(
            db,
            config,
            "poll",
            "Antwort enthielt keinen Ladezustand"
            + (f" (Fehler: {', '.join(snapshot.error_types)})" if snapshot.error_types else "")
            + " - Zustand unveraendert uebernommen.",
            level="warning",
            snapshot=snapshot,
        )
        _absorb_meta(config, snapshot)
        _schedule_next(config, active=had_open_session)
        return

    if plugged_in and not had_open_session:
        _start_open_session(db, config, snapshot)
    elif plugged_in and had_open_session:
        _update_open_session(db, config, snapshot)
        log_event(
            db,
            config,
            "poll",
            f"Ladevorgang laeuft (Zustand {state}), SoC {snapshot.soc_percent}%, "
            f"{_fmt_number(snapshot.charge_power_kw)} kW.",
            snapshot=snapshot,
        )
    elif not plugged_in and had_open_session:
        _finish_open_session(db, config, snapshot)
    else:
        detected = _detect_missed_session(db, config, snapshot)
        if not detected:
            note = " (Rueckspeisung)" if state in DISCHARGING_STATES else ""
            log_event(
                db,
                config,
                "poll",
                f"Kein Ladevorgang (Zustand {state}{note}), SoC {snapshot.soc_percent}%.",
                snapshot=snapshot,
            )

    config.last_charging_state = state
    config.last_soc = snapshot.soc_percent
    config.last_captured_at = snapshot.captured_at
    _absorb_meta(config, snapshot)
    _schedule_next(config, active=config.open_start_time is not None)


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

def poll_vehicle(db: Session, config: models.MySkodaConfig) -> None:
    """Genau ein Abruf fuer ein Fahrzeug.

    Wirft bewusst NICHT weiter (wie `webdav_backup.run_backup_for_user`) -
    Aufrufer sind die Scheduler-Schleife ueber mehrere Fahrzeuge und der
    manuelle "Jetzt abfragen"-Endpunkt; beide lesen das Ergebnis aus
    `config.last_status`/`last_error` bzw. dem Debug-Log.
    """
    # Vor dem Ueberschreiben merken: gemeldet wird nur der UEBERGANG nach
    # auth_error, nicht jeder Abruf im Fehlerzustand (siehe notifications.py).
    previous_status = config.last_status
    config.last_poll_at = datetime.utcnow()
    try:
        snapshot = MySkodaClient(config.api_key).get_vehicle(config.vin)
    except MySkodaAuthError as exc:
        config.last_status = "auth_error"
        config.last_error = str(exc)
        if exc.expired:
            config.api_key_expires_at = config.api_key_expires_at or datetime.utcnow()
        config.next_poll_at = datetime.utcnow() + timedelta(minutes=AUTH_ERROR_BACKOFF_MINUTES)
        log_event(db, config, "api_error", str(exc), level="error", force=True)
    except MySkodaRateLimitError as exc:
        config.last_status = "rate_limited"
        config.last_error = str(exc)
        wait = exc.retry_after if exc.retry_after else 15 * 60
        config.next_poll_at = datetime.utcnow() + timedelta(seconds=wait)
        log_event(
            db,
            config,
            "rate_limit",
            f"{exc} - naechster Versuch in {_fmt_minutes(wait)}.",
            level="warning",
            force=True,
        )
    except MySkodaError as exc:
        config.last_status = "error"
        config.last_error = str(exc)
        config.next_poll_at = datetime.utcnow() + timedelta(minutes=ERROR_BACKOFF_MINUTES)
        log_event(db, config, "api_error", str(exc), level="error", force=True)
    except Exception as exc:  # pragma: no cover - Schutznetz, damit ein
        # unerwarteter Fehler nicht die Schleife ueber die uebrigen Fahrzeuge
        # abbricht
        logger.exception("Unerwarteter Fehler beim MyŠkoda-Abruf")
        config.last_status = "error"
        config.last_error = str(exc)[:500]
        config.next_poll_at = datetime.utcnow() + timedelta(minutes=ERROR_BACKOFF_MINUTES)
        log_event(db, config, "api_error", str(exc)[:500], level="error", force=True)
    else:
        config.last_status = "ok"
        config.last_error = None
        _apply_snapshot(db, config, snapshot)
    finally:
        db.commit()
        notifications.notify_myskoda_status(db, config, previous_status)


def test_connection(db: Session, config: models.MySkodaConfig) -> dict:
    """Einmaliger Testabruf OHNE Zustandsmaschine.

    Bewusst getrennt von `poll_vehicle`: ein Verbindungstest soll niemals einen
    Ladevorgang anlegen oder beenden. Kontingent- und Ablaufinfos werden
    trotzdem uebernommen, und die geparste Zusammenfassung geht direkt an die
    Web-UI zurueck - das ist der schnellste Weg zu sehen, welche Felder die
    API fuer dieses Fahrzeug ueberhaupt liefert.
    """
    try:
        snapshot = MySkodaClient(config.api_key).get_vehicle(config.vin)
    except MySkodaError as exc:
        config.last_status = "auth_error" if isinstance(exc, MySkodaAuthError) else "error"
        config.last_error = str(exc)
        log_event(db, config, "test", f"Testabruf fehlgeschlagen: {exc}", level="error", force=True)
        db.commit()
        return {"ok": False, "error": str(exc), "summary": None}

    _absorb_meta(config, snapshot)
    log_event(
        db,
        config,
        "test",
        f"Testabruf erfolgreich: Zustand {snapshot.charging_state}, SoC {snapshot.soc_percent}%.",
        snapshot=snapshot,
        force=True,
    )
    db.commit()
    return {"ok": True, "error": None, "summary": snapshot.summary()}


def run_due_polls() -> None:
    """Vom Scheduler-Task in main.py periodisch aufgerufen.

    Ein fehlschlagendes Fahrzeug blockiert die anderen nicht - `poll_vehicle`
    faengt selbst ab und schreibt den Fehler in die Konfiguration.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        configs = (
            db.query(models.MySkodaConfig)
            .filter(models.MySkodaConfig.enabled == True)  # noqa: E712
            .all()
        )
        for config in configs:
            if not config.api_key or not config.vin:
                continue
            if config.next_poll_at and config.next_poll_at > now:
                continue
            try:
                poll_vehicle(db, config)
            except Exception:
                logger.exception(
                    "Unerwarteter Fehler im MyŠkoda-Poller fuer Fahrzeug %s", config.vehicle_id
                )
                db.rollback()
    finally:
        db.close()
