"""Automatische Ladeerkennung ueber die Fahrzeug-API der Renault-Gruppe.

Der Client (`renault.py`) liefert je Abruf einen Bericht, die
Zustandsmaschine ist dieselbe wie bei MyŠkoda und dem VW-Datenportal
(`charge_detection.py`): Einstecken, Laden, Ausstecken, Rueckdatierung,
Nacherkennung ueber einen SoC-Sprung.

## Sparsam mit dem Kontingent

Renault drosselt je Konto (Home Assistant rechnet mit 60 Anfragen je Stunde
und pausiert nach `err.func.wired.overloaded` 15 Minuten). Home Assistant
fragt sieben Endpunkte in festem Takt ab; die Ladeerkennung braucht davon
fast immer nur einen:

- **Jeder Abruf:** `battery-status` (Ladestand, Stecker, Ladezustand,
  Leistung). Daraus allein laesst sich jeder Uebergang erkennen.
- **Nur wenn sich etwas tut** (Stecker anders als der offene Vorgang, erster
  Abruf, SoC-Sprung fuer die Nacherkennung): zusaetzlich `cockpit`
  (Kilometerstand), `location` (Position fuer das Geo-Matching) und
  `hvac-status` (Aussentemperatur). Genau in diesen Momenten braucht die
  Zustandsmaschine diese Werte - beim Einstecken Position und Temperatur,
  beim Ausstecken den Kilometerstand.

Unterstuetzt ein Fahrzeug einen der drei Zusatzabrufe nicht (z.B. keine
Position bei der ZOE Phase 1), fehlt nur dieser Wert.

Laeuft parallel die Renault-Integration von Home Assistant mit demselben
Konto, teilen sich beide das Kontingent; ein `rate_limited` hier ist dann
erwartbar und heilt nach 15 Minuten von selbst.

## AC/DC

Die API nennt die Lade-Art nicht. Abgeleitet wird sie aus der Leistung (ab
23 kW Gleichstrom); zeigt ein spaeterer Abruf mehr als 23 kW, wird ein zuerst
als AC erkannter Vorgang auf DC hochgestuft (die Leistung steigt am
Schnelllader erst nach ein paar Sekunden).

## Anmeldung

Gespeichert wird das Login-Token von Gigya (siehe renault.py), nicht nur das
Passwort - sonst meldete sich der Server nach jedem Neustart mit dem Passwort
an und Renault verlangte jedes Mal einen Code per E-Mail. Verlangt Renault
trotzdem einen Code (Token abgelaufen, Geraet vergessen), geht der Abruf auf
`tfa_required` und der Nutzer bestaetigt in den Einstellungen. Den Code
schickt der Server dabei nie von sich aus an - nur auf "Anmelden" hin.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, notifications
from .charge_detection import DetectionSource, apply_snapshot, fmt_minutes
from .database import SessionLocal
from .renault import (
    DISCHARGING_STATES,
    PendingTwoFactor,
    RenaultAuthError,
    RenaultClient,
    RenaultError,
    RenaultInvalidCode,
    RenaultNotSupportedError,
    RenaultRateLimitError,
    RenaultSnapshot,
    RenaultTwoFactorRequired,
)

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL_SECONDS = 60
LOG_MAX_ENTRIES = 500

#: Falsches Passwort / Code noetig: nicht im Minutentakt neu probieren - zu
#: viele Fehlversuche sperren bei Gigya das Konto.
AUTH_ERROR_BACKOFF_MINUTES = 60
#: Nach `err.func.wired.overloaded` - dieselbe Pause wie Home Assistant
#: (COOLING_UPDATES_SECONDS).
RATE_LIMIT_BACKOFF_MINUTES = 15
#: Netz-/Serverfehler: 5, 15, dann 30 Minuten.
ERROR_BACKOFF_MINUTES = (5, 15, 30)
#: So lange gilt ein verschickter Bestaetigungscode hier als offen.
PENDING_TFA_SECONDS = 15 * 60
#: Rueckdatierung: der vorherige Abruf darf hoechstens zwei Leerlaufintervalle
#: alt sein (wie bei MyŠkoda).
BACKDATE_GAP_INTERVAL_FACTOR = 2


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------

def log_event(
    db: Session,
    config: models.RenaultConfig,
    event: str,
    message: str,
    *,
    level: str = "info",
    snapshot: RenaultSnapshot | None = None,
    force: bool = False,
) -> None:
    if not config.log_enabled and not force:
        return
    entry = models.RenaultLogEntry(
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
                {"summary": snapshot.summary(), "responses": snapshot.raw},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
    db.add(entry)
    db.flush()
    old_ids = [
        row.id
        for row in db.query(models.RenaultLogEntry.id)
        .filter(models.RenaultLogEntry.vehicle_id == config.vehicle_id)
        .order_by(models.RenaultLogEntry.created_at.desc())
        .offset(LOG_MAX_ENTRIES)
        .all()
    ]
    if old_ids:
        db.query(models.RenaultLogEntry).filter(
            models.RenaultLogEntry.id.in_(old_ids)
        ).delete(synchronize_session=False)


def _backdate_limit_seconds(config: models.RenaultConfig) -> int:
    configured = config.backdate_max_gap_minutes or 0
    if configured > 0:
        return configured * 60
    return BACKDATE_GAP_INTERVAL_FACTOR * max(5, config.poll_interval_idle_minutes) * 60


SOURCE = DetectionSource(
    label="die Renault-API (My Renault)",
    id_prefix="renault",
    log=log_event,
    backdate_limit_seconds=_backdate_limit_seconds,
    discharging_states=DISCHARGING_STATES,
)


# --------------------------------------------------------------------------
# Clients zwischen zwei Abrufen aufheben
# --------------------------------------------------------------------------

_clients: dict[str, tuple[str, RenaultClient]] = {}
_pending: dict[str, tuple[RenaultClient, PendingTwoFactor]] = {}
_lock = threading.Lock()


def _fingerprint(config: models.RenaultConfig) -> str:
    raw = "\0".join(str(v) for v in (config.email, config.password, config.locale))
    return hashlib.sha256(raw.encode()).hexdigest()


def _device_ids(config: models.RenaultConfig) -> dict[str, str]:
    try:
        value = json.loads(config.device_ids or "{}")
    except ValueError:
        return {}
    return {k: str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _new_client(config: models.RenaultConfig) -> RenaultClient:
    return RenaultClient(
        config.email or "",
        config.password or "",
        locale=config.locale or "de_DE",
        login_token=config.login_token,
        device_ids=_device_ids(config),
        account_id=config.account_id,
    )


def _client_for(config: models.RenaultConfig) -> RenaultClient:
    """Derselbe Client (also derselbe JWT) fuer dieselben Zugangsdaten."""
    fingerprint = _fingerprint(config)
    with _lock:
        cached = _clients.get(config.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            cached[1].close()
        client = _new_client(config)
        _clients[config.id] = (fingerprint, client)
        return client


def forget_client(config_id: str) -> None:
    with _lock:
        cached = _clients.pop(config_id, None)
        pending = _pending.pop(config_id, None)
    if cached:
        cached[1].close()
    if pending and (not cached or pending[0] is not cached[1]):
        pending[0].close()


def _store_credentials(config: models.RenaultConfig, client: RenaultClient) -> None:
    """Was der Client bei der Anmeldung gelernt hat, in die Zeile zurueck -
    damit ein Neustart ohne Passwort-Anmeldung weitermacht."""
    if client.login_token != config.login_token:
        config.login_token = client.login_token
    if client.device_ids:
        encoded = json.dumps(client.device_ids, sort_keys=True)
        if encoded != config.device_ids:
            config.device_ids = encoded
    if client.account_id and client.account_id != config.account_id:
        config.account_id = client.account_id


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

def _needs_details(config: models.RenaultConfig, snapshot: RenaultSnapshot) -> bool:
    """Ob zu diesem Abruf Kilometerstand, Position und Temperatur gebraucht
    werden (siehe Modul-Docstring, "Sparsam mit dem Kontingent")."""
    plugged = snapshot.is_plugged_in
    if plugged is None:
        return False
    if config.last_captured_at is None:
        return True
    had_open = config.open_start_time is not None
    if plugged != had_open:
        return True
    if not plugged and config.detect_missed_sessions:
        soc, last = snapshot.soc_percent, config.last_soc
        if soc is not None and last is not None and soc - last >= max(1, config.missed_session_min_soc_delta):
            return True
    return False


def _fetch_snapshot(db: Session, config: models.RenaultConfig, client: RenaultClient) -> RenaultSnapshot:
    vin = config.vin or ""
    try:
        battery = client.battery_status(vin)
    except RenaultNotSupportedError as exc:
        raise RenaultError(
            f"Renault liefert für dieses Fahrzeug keinen Akkustatus ({exc}) - "
            "nur Elektro- und Plug-in-Fahrzeuge haben einen."
        ) from exc
    snapshot = RenaultSnapshot(battery, model_code=config.model_code)
    if not _needs_details(config, snapshot):
        return snapshot

    missing = []
    for attr, fetch, label in (
        ("cockpit", client.cockpit, "Kilometerstand"),
        ("location", client.location, "Position"),
        ("hvac", client.hvac_status, "Außentemperatur"),
    ):
        try:
            setattr(snapshot, attr, fetch(vin))
        except RenaultNotSupportedError:
            missing.append(label)
    if missing:
        log_event(db, config, "poll", "Vom Fahrzeug nicht geliefert: " + ", ".join(missing) + ".",
                  level="warning")
    return snapshot


def _apply(db: Session, config: models.RenaultConfig, snapshot: RenaultSnapshot) -> None:
    if not apply_snapshot(db, config, snapshot, SOURCE):
        log_event(
            db,
            config,
            "poll",
            "Abruf ohne erkennbaren Steckerzustand - nichts entschieden.",
            snapshot=snapshot,
        )
        return
    # Zuerst als AC erkannt, dann mehr als 23 kW: das war ein Schnelllader.
    if config.open_start_time is not None and snapshot.charge_type == "DC":
        config.open_charging_type = "DC"


def _next_poll(config: models.RenaultConfig) -> datetime:
    minutes = (
        config.poll_interval_active_minutes
        if config.open_start_time is not None
        else config.poll_interval_idle_minutes
    )
    return datetime.utcnow() + timedelta(minutes=max(3, minutes or 15))


def poll_vehicle(db: Session, config: models.RenaultConfig) -> None:
    """Ein Abruf fuer ein Fahrzeug. Wirft nie weiter (wie die anderen Poller);
    das Ergebnis steht in `last_status`/`last_error` und im Protokoll."""
    previous_status = config.last_status
    config.last_poll_at = datetime.utcnow()
    client: RenaultClient | None = None
    try:
        client = _client_for(config)
        snapshot = _fetch_snapshot(db, config, client)
        _apply(db, config, snapshot)
    except RenaultTwoFactorRequired as exc:
        db.rollback()
        forget_client(config.id)
        client = None
        config.login_token = None
        _fail(db, config, "tfa_required", str(exc), AUTH_ERROR_BACKOFF_MINUTES * 60)
    except RenaultAuthError as exc:
        db.rollback()
        forget_client(config.id)
        client = None
        config.login_token = None
        _fail(db, config, "auth_error", str(exc), AUTH_ERROR_BACKOFF_MINUTES * 60)
    except RenaultRateLimitError as exc:
        db.rollback()
        wait = RATE_LIMIT_BACKOFF_MINUTES * 60
        _fail(db, config, "rate_limited", f"{exc} Nächster Versuch in {fmt_minutes(wait)}.", wait)
    except RenaultError as exc:
        db.rollback()
        _fail(db, config, "error", str(exc), None)
    except Exception as exc:  # pragma: no cover - Schutznetz
        logger.exception("Unerwarteter Fehler beim Abruf bei Renault")
        db.rollback()
        _fail(db, config, "error", str(exc)[:500], None)
    else:
        config.last_status = "ok"
        config.last_error = None
        config.error_streak = 0
        config.next_poll_at = _next_poll(config)
    finally:
        if client is not None:
            _store_credentials(config, client)
        db.commit()
        notifications.notify_renault_status(db, config, previous_status)


def _fail(
    db: Session, config: models.RenaultConfig, status: str, message: str, wait_seconds: int | None
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


# --------------------------------------------------------------------------
# Anmeldung mit Bestaetigungscode (aus der Web-UI)
# --------------------------------------------------------------------------

def start_login(db: Session, config: models.RenaultConfig) -> dict:
    """Mit E-Mail und Passwort anmelden. Verlangt Renault einen Code, wird er
    an die Adresse des Kontos geschickt und `code_sent` zurueckgegeben -
    danach `complete_login` mit dem Code."""
    forget_client(config.id)
    config.login_token = None
    client = _new_client(config)
    try:
        client.login()
    except RenaultTwoFactorRequired as exc:
        try:
            pending = client.start_two_factor(exc.reg_token)
        except RenaultError as start_exc:
            client.close()
            return _login_failed(db, config, start_exc)
        with _lock:
            _pending[config.id] = (client, pending)
        _store_credentials(config, client)
        config.last_status = "tfa_required"
        config.last_error = None
        log_event(db, config, "auth", "Renault verlangt einen Bestätigungscode - an "
                  f"{pending.email_hint or 'die E-Mail-Adresse des Kontos'} geschickt.", force=True)
        db.commit()
        return {"status": "code_sent", "email_hint": pending.email_hint, "error": None}
    except RenaultError as exc:
        client.close()
        return _login_failed(db, config, exc)
    return _login_succeeded(db, config, client)


def complete_login(db: Session, config: models.RenaultConfig, code: str) -> dict:
    with _lock:
        entry = _pending.get(config.id)
    if not entry or time.monotonic() - entry[1].created > PENDING_TFA_SECONDS:
        forget_client(config.id)
        return {"status": "expired", "email_hint": None,
                "error": "Kein offener Bestätigungscode (abgelaufen) - bitte erneut „Anmelden“."}
    client, pending = entry
    try:
        client.complete_two_factor(pending, code)
    except RenaultInvalidCode as exc:
        # Derselbe Code-Vorgang bleibt offen, der richtige Code kann folgen.
        return {"status": "code_sent", "email_hint": pending.email_hint, "error": str(exc)}
    except RenaultError as exc:
        with _lock:
            _pending.pop(config.id, None)
        client.close()
        return _login_failed(db, config, exc)
    with _lock:
        _pending.pop(config.id, None)
    return _login_succeeded(db, config, client)


def _login_succeeded(db: Session, config: models.RenaultConfig, client: RenaultClient) -> dict:
    _store_credentials(config, client)
    with _lock:
        old = _clients.get(config.id)
        _clients[config.id] = (_fingerprint(config), client)
    if old and old[1] is not client:
        old[1].close()
    config.last_status = None if config.last_status in ("auth_error", "tfa_required") else config.last_status
    config.last_error = None
    config.error_streak = 0
    config.next_poll_at = None
    log_event(db, config, "auth", "Anmeldung bei Renault erfolgreich.", force=True)
    db.commit()
    return {"status": "connected", "email_hint": None, "error": None}


def _login_failed(db: Session, config: models.RenaultConfig, exc: RenaultError) -> dict:
    config.last_status = "auth_error" if isinstance(exc, RenaultAuthError) else "error"
    config.last_error = str(exc)
    log_event(db, config, "auth", f"Anmeldung fehlgeschlagen: {exc}", level="error", force=True)
    db.commit()
    return {"status": "failed", "email_hint": None, "error": str(exc)}


def test_connection(db: Session, config: models.RenaultConfig) -> dict:
    """Fahrzeuge und Akkustatus lesen - OHNE Zustandsmaschine, legt also nie
    einen Ladevorgang an. Uebernimmt die FIN, wenn keine eingetragen ist und
    das Konto genau ein Fahrzeug hat. Meldet sich nicht von sich aus mit
    Code an (das bleibt "Anmelden" vorbehalten)."""
    result: dict = {"ok": False, "error": None, "vehicles": [], "summary": None}
    client = _client_for(config)
    try:
        vehicles = client.list_vehicles()
        result["vehicles"] = [
            {"vin": v["vin"], "model": " ".join(p for p in (v["brand"], v["model"]) if p) or None}
            for v in vehicles
        ]
        if not config.vin and len(vehicles) == 1:
            config.vin = vehicles[0]["vin"]
        if not config.vin:
            raise RenaultError(
                "Im Renault-Konto sind mehrere oder keine Fahrzeuge - FIN bitte eintragen."
            )
        match = next((v for v in vehicles if v["vin"] == config.vin), None)
        if match is None:
            raise RenaultError("Diese FIN gehört nicht zum angemeldeten Renault-Konto.")
        config.model_code = match["model_code"]
        config.model_label = " ".join(p for p in (match["brand"], match["model"]) if p) or None
        snapshot = RenaultSnapshot(client.battery_status(config.vin), model_code=config.model_code)
        result["summary"] = snapshot.summary()
        log_event(db, config, "test", "Testabruf erfolgreich.", snapshot=snapshot, force=True)
        result["ok"] = True
        config.last_status = "ok"
        config.last_error = None
    except RenaultError as exc:
        if isinstance(exc, RenaultTwoFactorRequired):
            forget_client(config.id)
            config.login_token = None
            config.last_status = "tfa_required"
        elif isinstance(exc, RenaultAuthError):
            forget_client(config.id)
            config.login_token = None
            config.last_status = "auth_error"
        elif isinstance(exc, RenaultRateLimitError):
            config.last_status = "rate_limited"
        else:
            config.last_status = "error"
        config.last_error = str(exc)
        result["error"] = str(exc)
        log_event(db, config, "test", f"Testabruf fehlgeschlagen: {exc}", level="error", force=True)
    else:
        _store_credentials(config, client)
    db.commit()
    return result


def run_due_polls() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        configs = (
            db.query(models.RenaultConfig)
            .filter(models.RenaultConfig.enabled == True)  # noqa: E712
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
                logger.exception("Unerwarteter Fehler im Renault-Poller fuer Fahrzeug %s", config.vehicle_id)
                db.rollback()
    finally:
        db.close()
