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

Die Zustandsmaschine selbst (Schnitt eines Ladevorgangs, Rueckdatierung des
Ladebeginns, Nacherkennung ueber einen SoC-Sprung) liegt seit v0.39.0 in
`charge_detection.py`, weil das VW-Datenportal (`vw_data_act_poller.py`)
dieselbe braucht. Hier bleibt, was nur MyŠkoda betrifft: Abruf,
Kontingent, Abfrageintervall und das Debug-Protokoll.
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
from .charge_detection import (
    DetectionSource,
    apply_snapshot,
    backdated_start,
    fmt_minutes,
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
    """Korrigierter Ladebeginn mit dem Zeitfenster dieser Quelle (siehe
    charge_detection.backdated_start)."""
    return backdated_start(config, capacity_kwh, _backdate_limit_seconds(config))


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


SOURCE = DetectionSource(
    label="die MyŠkoda Public API",
    id_prefix="myskoda",
    log=log_event,
    backdate_limit_seconds=_backdate_limit_seconds,
    discharging_states=frozenset(DISCHARGING_STATES),
    saved_location_label="MyŠkoda-App",
)


# --------------------------------------------------------------------------
# Zustandsmaschine (siehe charge_detection.py)
# --------------------------------------------------------------------------

def _apply_snapshot(
    db: Session, config: models.MySkodaConfig, snapshot: VehicleSnapshot
) -> None:
    """Verarbeitet eine Antwort: Uebergaenge erkennen, Zustand fortschreiben."""
    had_open_session = config.open_start_time is not None
    if not apply_snapshot(db, config, snapshot, SOURCE):
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
            f"{exc} - naechster Versuch in {fmt_minutes(wait)}.",
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
