"""Automatische Ladeerkennung ueber das VW-Datenportal nach EU Data Act.

Das Portal (siehe vw_data_act.py) legt alle 15 Minuten ein ZIP mit dem
Fahrzeugzustand bereit. Dieser Poller holt die Liste, laedt jedes noch nicht
verarbeitete ZIP in zeitlicher Reihenfolge und schickt es als Bericht durch
dieselbe Zustandsmaschine wie die MyŠkoda Public API
(`charge_detection.py`): Einstecken, Laden, Ausstecken, Rueckdatierung,
Nacherkennung ueber einen SoC-Sprung.

## Unterschiede zu MyŠkoda

- **Ein Bericht je ZIP, nicht je Abruf.** Ein Abruf kann mehrere ZIPs
  nachholen (Server war aus, Fahrzeug hat viel auf einmal gesendet). Jedes
  wird einzeln verarbeitet, aelteste zuerst; der Cursor
  (`last_dataset_created`/`last_dataset_name`) rueckt erst NACH der
  Verarbeitung eines ZIPs vor - ein Fehler mittendrin verliert nichts, das
  ZIP kommt beim naechsten Abruf erneut. Ein ZIP enthaelt mehrere Berichte
  ohne Reihenfolge; daraus wird EIN Bericht mit dem jeweils frischesten Wert
  je Feld (siehe vw_data_act.Dataset).
- **Gemessene Energie.** Das Portal liefert den Energiezaehler des Fahrzeugs
  (`battery_state_report.charge_energy`) - die Ladevorgaenge bekommen damit
  echte kWh statt der Schaetzung aus dem SoC (Messort Fahrzeug).
- **Keine Position.** Ohne GPS greift das Geo-Matching nicht, Ladeort und
  Anbieter traegt man beim Pruefen nach (`needs_review` bleibt ohnehin).
- **Erster Abruf:** liest nur das juengste ZIP als Ausgangszustand. Die
  rollierende Liste kann Tage zurueckreichen - sie komplett nachzuspielen
  wuerde alte Vorgaenge anlegen, die es per Hand vielleicht schon gibt.
- **Kein Kontingent bekannt.** Ein 429 wird beachtet (`Retry-After`), sonst
  15 Minuten wie der Takt, in dem das Portal ohnehin nur neue Daten hat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, notifications
from .charge_detection import DetectionSource, apply_snapshot, fmt_minutes
from .database import SessionLocal
from .vw_data_act import (
    DISCHARGING_STATES,
    NO_CONTENT_SUFFIX,
    DataActAuthError,
    DataActClient,
    DataActError,
    DataActNotFoundError,
    DataActRateLimitError,
    DataActSnapshot,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL_SECONDS = 60
LOG_MAX_ENTRIES = 500

#: Falsches Passwort oder offene Zustimmung bei VW: nicht im Minutentakt neu
#: probieren - das kann zur Sperre des Kontos fuehren.
AUTH_ERROR_BACKOFF_MINUTES = 60
#: Keine Datenanfrage im Portal - wird erst wieder gueltig, wenn der Nutzer
#: eine anlegt.
NO_REQUEST_BACKOFF_MINUTES = 60
#: Netz-/Serverfehler: 5, 15, dann 30 Minuten.
ERROR_BACKOFF_MINUTES = (5, 15, 30)
#: Hoechstens so viele ZIPs je Abruf - der Rest kommt beim naechsten.
MAX_DATASETS_PER_POLL = 24
#: Rueckdatierung des Ladebeginns: der vorherige Bericht darf hoechstens zwei
#: Lieferintervalle alt sein (siehe charge_detection.backdated_start).
BACKDATE_GAP_INTERVAL_FACTOR = 2


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------

def log_event(
    db: Session,
    config: models.VwDataActConfig,
    event: str,
    message: str,
    *,
    level: str = "info",
    snapshot: DataActSnapshot | None = None,
    force: bool = False,
) -> None:
    if not config.log_enabled and not force:
        return
    entry = models.VwDataActLogEntry(
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
                {"summary": snapshot.summary(), "dataset": snapshot.raw},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
    db.add(entry)
    db.flush()
    old_ids = [
        row.id
        for row in db.query(models.VwDataActLogEntry.id)
        .filter(models.VwDataActLogEntry.vehicle_id == config.vehicle_id)
        .order_by(models.VwDataActLogEntry.created_at.desc())
        .offset(LOG_MAX_ENTRIES)
        .all()
    ]
    if old_ids:
        db.query(models.VwDataActLogEntry).filter(
            models.VwDataActLogEntry.id.in_(old_ids)
        ).delete(synchronize_session=False)


def _backdate_limit_seconds(config: models.VwDataActConfig) -> int:
    configured = config.backdate_max_gap_minutes or 0
    if configured > 0:
        return configured * 60
    return BACKDATE_GAP_INTERVAL_FACTOR * max(15, config.poll_interval_minutes) * 60


SOURCE = DetectionSource(
    label="das VW-Datenportal (EU Data Act)",
    id_prefix="vwda",
    log=log_event,
    backdate_limit_seconds=_backdate_limit_seconds,
    discharging_states=DISCHARGING_STATES,
    charging_states=frozenset({"CHARGING_HV_BATTERY", "CHARGING"}),
)


# --------------------------------------------------------------------------
# Angemeldete Clients zwischen zwei Abrufen aufheben
# --------------------------------------------------------------------------

_clients: dict[str, tuple[str, DataActClient]] = {}
_clients_lock = threading.Lock()


def _fingerprint(config: models.VwDataActConfig) -> str:
    raw = "\0".join(
        str(v) for v in (config.email, config.password, config.brand, config.country, config.language)
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _client_for(config: models.VwDataActConfig) -> DataActClient:
    """Derselbe Client (also dasselbe Session-Cookie) fuer dieselben
    Zugangsdaten - sonst meldete sich der Server alle 15 Minuten neu bei VW an.
    Geaenderte Zugangsdaten verwerfen ihn."""
    fingerprint = _fingerprint(config)
    with _clients_lock:
        cached = _clients.get(config.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            cached[1].close()
        client = DataActClient(
            config.email or "",
            config.password or "",
            brand=config.brand,
            country=config.country or "de",
            language=config.language or "de",
        )
        _clients[config.id] = (fingerprint, client)
        return client


def forget_client(config_id: str) -> None:
    with _clients_lock:
        cached = _clients.pop(config_id, None)
    if cached:
        cached[1].close()


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

def _dataset_key(item: dict) -> tuple[datetime, str]:
    return (parse_timestamp(item.get("createdOn")) or datetime.min, str(item.get("name")))


def _pending_datasets(config: models.VwDataActConfig, files: list[dict]) -> list[dict]:
    """Noch nicht verarbeitete ZIPs, aelteste zuerst. Beim ersten Abruf nur
    das juengste (siehe Modul-Docstring)."""
    ordered = sorted(files, key=_dataset_key)
    if config.last_dataset_name is None and config.last_dataset_created is None:
        return ordered[-1:]
    cursor = (config.last_dataset_created or datetime.min, config.last_dataset_name or "")
    return [f for f in ordered if _dataset_key(f) > cursor]


def _advance_cursor(config: models.VwDataActConfig, item: dict) -> None:
    created, name = _dataset_key(item)
    config.last_dataset_name = name
    config.last_dataset_created = created if created != datetime.min else None


def _process_datasets(db: Session, config: models.VwDataActConfig, client: DataActClient) -> str:
    """Holt Liste und neue ZIPs, gibt eine Kurzbeschreibung fuer das Protokoll
    zurueck. Wirft die Fehler des Clients weiter."""
    if not config.identifier:
        config.identifier = client.get_identifier(config.vin)
    files = client.list_datasets(config.vin, config.identifier)
    if not files:
        # Leere Liste: entweder noch nichts geliefert, oder die Datenanfrage
        # wurde im Portal neu angelegt und hat eine andere Kennung.
        fresh = client.get_identifier(config.vin)
        if fresh != config.identifier:
            config.identifier = fresh
            files = client.list_datasets(config.vin, config.identifier)

    pending = _pending_datasets(config, files)
    truncated = len(pending) > MAX_DATASETS_PER_POLL
    pending = pending[:MAX_DATASETS_PER_POLL]
    processed = empty = 0
    for item in pending:
        name = str(item["name"])
        if name.endswith(NO_CONTENT_SUFFIX):
            empty += 1
            _advance_cursor(config, item)
            continue
        try:
            payload = client.download_dataset(config.vin, config.identifier, name)
        except DataActNotFoundError:
            # Zwischen Liste und Download aus dem rollierenden Fenster gefallen.
            log_event(db, config, "dataset", f"Datensatz {name} nicht mehr abrufbar - uebersprungen.",
                      level="warning", force=True)
            _advance_cursor(config, item)
            continue
        snapshot = DataActSnapshot.from_json(
            payload, name=name, created_on=parse_timestamp(item.get("createdOn"))
        )
        _apply(db, config, snapshot)
        _advance_cursor(config, item)
        config.last_dataset_at = snapshot.captured_at
        processed += 1
        # Nach jedem ZIP festschreiben: bricht der naechste Download ab, ist
        # dieser Fortschritt nicht verloren.
        db.commit()

    if truncated:
        # Rest gleich im Anschluss, nicht erst in einer Viertelstunde.
        config.next_poll_at = datetime.utcnow() + timedelta(minutes=1)
    return (
        f"{len(files)} Datensaetze im Portal, {processed} neu verarbeitet"
        + (f", {empty} leer" if empty else "")
        + (" - weitere folgen." if truncated else ".")
    )


def _apply(db: Session, config: models.VwDataActConfig, snapshot: DataActSnapshot) -> None:
    # Die ZIPs kommen nach `createdOn` sortiert, ihr Inhalt aber nicht immer:
    # beim ID.3 (19.10.2026) enthielt ein spaeteres ZIP einen Stand von 05:24,
    # nachdem schon einer von 06:02 verarbeitet war, und meldete darin noch das
    # laengst gezogene Kabel. Ausgewertet haette das einen Vorgang mit
    # Startzeit 05:24 eroeffnet. Ein aelterer Stand als der zuletzt verarbeitete
    # bringt nichts Neues - nur protokollieren.
    if (
        config.last_captured_at is not None
        and snapshot.captured_at < config.last_captured_at
    ):
        log_event(
            db,
            config,
            "dataset",
            f"Datensatz {snapshot.name} enthaelt einen aelteren Stand "
            f"({snapshot.captured_at:%d.%m. %H:%M:%S}) als der zuletzt verarbeitete "
            f"({config.last_captured_at:%d.%m. %H:%M:%S}) - uebersprungen.",
            snapshot=snapshot,
        )
        return
    if not apply_snapshot(db, config, snapshot, SOURCE):
        log_event(
            db,
            config,
            "dataset",
            f"Datensatz {snapshot.name} ohne Lade- oder Steckerzustand - nichts entschieden.",
            snapshot=snapshot,
        )


def poll_vehicle(db: Session, config: models.VwDataActConfig) -> None:
    """Ein Abruf fuer ein Fahrzeug. Wirft nie weiter (wie die anderen Poller);
    das Ergebnis steht in `last_status`/`last_error` und im Protokoll."""
    previous_status = config.last_status
    now = datetime.utcnow()
    config.last_poll_at = now
    config.next_poll_at = now + timedelta(minutes=max(5, config.poll_interval_minutes))
    try:
        client = _client_for(config)
        summary = _process_datasets(db, config, client)
    except DataActAuthError as exc:
        db.rollback()
        forget_client(config.id)
        _fail(db, config, "auth_error", str(exc), AUTH_ERROR_BACKOFF_MINUTES * 60)
    except DataActRateLimitError as exc:
        db.rollback()
        wait = exc.retry_after or 15 * 60
        _fail(db, config, "rate_limited", f"{exc} Naechster Versuch in {fmt_minutes(wait)}.", wait)
    except DataActNotFoundError as exc:
        db.rollback()
        config.identifier = None
        _fail(db, config, "no_request", str(exc), NO_REQUEST_BACKOFF_MINUTES * 60)
    except DataActError as exc:
        db.rollback()
        _fail(db, config, "error", str(exc), None)
    except Exception as exc:  # pragma: no cover - Schutznetz
        logger.exception("Unerwarteter Fehler beim Abruf aus dem VW-Datenportal")
        db.rollback()
        _fail(db, config, "error", str(exc)[:500], None)
    else:
        config.last_status = "ok"
        config.last_error = None
        config.error_streak = 0
        log_event(db, config, "poll", summary)
    finally:
        db.commit()
        notifications.notify_vw_data_act_status(db, config, previous_status)


def _fail(
    db: Session, config: models.VwDataActConfig, status: str, message: str, wait_seconds: int | None
) -> None:
    config.last_poll_at = datetime.utcnow()
    config.last_status = status
    config.last_error = message
    config.error_streak = (config.error_streak or 0) + 1
    if wait_seconds is None:
        step = min(config.error_streak, len(ERROR_BACKOFF_MINUTES)) - 1
        wait_seconds = ERROR_BACKOFF_MINUTES[step] * 60
    config.next_poll_at = datetime.utcnow() + timedelta(seconds=wait_seconds)
    log_event(
        db,
        config,
        "rate_limit" if status == "rate_limited" else "api_error",
        message,
        level="warning" if status == "rate_limited" else "error",
        force=True,
    )


def test_connection(db: Session, config: models.VwDataActConfig) -> dict:
    """Anmelden, Fahrzeuge und Datenanfrage pruefen - OHNE Zustandsmaschine,
    legt also nie einen Ladevorgang an. Uebernimmt die FIN, wenn keine
    eingetragen ist und das Konto genau ein Fahrzeug hat."""
    result: dict = {"ok": False, "error": None, "vehicles": [], "identifier": None,
                    "datasets": None, "summary": None}
    try:
        client = _client_for(config)
        client.login()
        vehicles = client.list_vehicles()
        result["vehicles"] = [v["vin"] for v in vehicles]
        if not config.vin and len(vehicles) == 1:
            config.vin = vehicles[0]["vin"]
        if not config.vin:
            raise DataActError(
                "Im Portal sind mehrere oder keine Fahrzeuge freigegeben - FIN bitte eintragen."
            )
        config.identifier = client.get_identifier(config.vin)
        result["identifier"] = True
        files = client.list_datasets(config.vin, config.identifier)
        result["datasets"] = len(files)
        filled = [f for f in sorted(files, key=_dataset_key) if not str(f["name"]).endswith(NO_CONTENT_SUFFIX)]
        if filled:
            name = str(filled[-1]["name"])
            snapshot = DataActSnapshot.from_json(
                client.download_dataset(config.vin, config.identifier, name),
                name=name,
                created_on=parse_timestamp(filled[-1].get("createdOn")),
            )
            result["summary"] = snapshot.summary()
            log_event(db, config, "test", f"Testabruf erfolgreich ({name}).", snapshot=snapshot, force=True)
        else:
            log_event(db, config, "test", "Anmeldung erfolgreich, noch keine Daten im Portal.", force=True)
        result["ok"] = True
        config.last_status = "ok"
        config.last_error = None
    except DataActError as exc:
        if isinstance(exc, DataActAuthError):
            forget_client(config.id)
            config.last_status = "auth_error"
        elif isinstance(exc, DataActNotFoundError):
            config.last_status = "no_request"
        else:
            config.last_status = "error"
        config.last_error = str(exc)
        result["error"] = str(exc)
        log_event(db, config, "test", f"Testabruf fehlgeschlagen: {exc}", level="error", force=True)
    db.commit()
    return result


def run_due_polls() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        configs = (
            db.query(models.VwDataActConfig)
            .filter(models.VwDataActConfig.enabled == True)  # noqa: E712
            .all()
        )
        for config in configs:
            if not (config.email and config.password and config.vin):
                continue
            if config.next_poll_at and config.next_poll_at > now:
                continue
            try:
                poll_vehicle(db, config)
            except Exception:
                logger.exception("Unerwarteter Fehler im VW-Poller fuer Fahrzeug %s", config.vehicle_id)
                db.rollback()
    finally:
        db.close()
