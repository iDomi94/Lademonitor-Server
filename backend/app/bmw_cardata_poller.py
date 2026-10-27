"""Ladevorgaenge aus der BMW-CarData-Ladehistorie uebernehmen.

Dritte Quelle fuer `source=AUTOMATIC`-Ladevorgaenge (neben dem
Home-Assistant-Push und dem MyŠkoda-Poller). Die Nachbearbeitung ist
dieselbe wie dort (Ladeort-Matching, Reverse-Geocoding, Anbieterpreis aus
`routers/sessions.py`), alle Vorgaenge bleiben `needs_review`.

## Warum hier KEINE Zustandsmaschine

BMW liefert abgeschlossene Ladevorgaenge als Liste (`/chargingHistory`) - mit
Start, Ende, SoC von/bis, Kilometerstand, Position und der aus dem Netz
bezogenen Energie. Der Poller muss also nicht wie beim MyŠkoda-Poller aus
wiederholten Momentaufnahmen einen Vorgang zusammensetzen, sondern nur
regelmaessig nachsehen, was neu ist. Damit entfallen auch Rueckdatierung und
Nacherkennung - die Schwaechen, die beim Polling einer Momentaufnahme
entstehen, gibt es hier nicht.

## Was aus einem Eintrag wird

- `energy_kwh` = `energyConsumedFromPowerGridKwh`, als **gemessen** und mit
  Messort "Ladesaeule": die Energie ist auf der Netzseite erfasst, enthaelt
  also die Ladeverluste - genau wie ein Wallbox-Zaehler. Damit zaehlen diese
  Vorgaenge in Akku-Index und Verlustrechnung (battery.py), anders als die
  geschaetzten aus HA-Push und MyŠkoda-Poller. Fehlt der Wert, schaetzt der
  Server wie bisher aus dem SoC-Hub.
- **Lade-Art abgeleitet:** die API nennt sie nicht. Liegt die Leistung eines
  Ladeabschnitts (oder der Durchschnitt) ueber 23 kW, war es DC - ein BMW
  laedt an Wechselstrom mit hoechstens 22 kW.
- **Anbieter:** zuerst ueber den Ladeort wie ueberall; sonst, wenn BMW einen
  oeffentlichen Ladepunkt erkannt hat, ein eigener Anbieter mit genau diesem
  Namen (Gross-/Kleinschreibung egal). Es wird nie ein Anbieter angelegt.
- Zeitstempel kommen als Epoch-Sekunden und werden wie ueberall in dieser App
  als naive LOKALZEIT gespeichert.

## Kontingent

50 Anfragen je 24 h und Konto. Der Poller zaehlt seine eigenen Anfragen je
UTC-Tag mit (`requests_today`) und laesst `RESERVE_REQUESTS` uebrig, damit
eine parallel laufende Home-Assistant-Integration am selben Konto nicht
leer ausgeht. Die Token-Erneuerung laeuft ueber den Anmeldedienst, nicht ueber
die Kunden-API, und wird nicht mitgezaehlt.

## Fenster und Cursor

Gelesen wird ab `history_cursor` in Fenstern von hoechstens `WINDOW_DAYS`
(BMW nennt keine Obergrenze fuer `from`/`to`; 30 Tage sind das, was andere
Clients nutzen). Liegt das Fensterende noch in der Vergangenheit (erster
Abruf mit langer Rueckschau), geht es nach `BACKFILL_DELAY_MINUTES` mit dem
naechsten Fenster weiter. Der Cursor bleibt `OVERLAP_DAYS` hinter dem
Fensterende: BMW reicht Vorgaenge teils verspaetet nach, und die
`external_session_id` macht ein zweites Lesen folgenlos.
"""

import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import bmw_cardata, models, notifications
from .bmw_cardata import (
    AuthorizationPending,
    CarDataAuthError,
    CarDataError,
    CarDataRateLimitError,
    HistorySession,
)
from .database import SessionLocal
from .session_merge import merge_or_none
from .routers.sessions import (
    apply_provider_price,
    estimate_energy_kwh,
    normalize_energy_meter,
    resolve_location,
)

logger = logging.getLogger(__name__)

#: Takt des Schedulers in main.py. Faellig ist ein Fahrzeug erst, wenn
#: `next_poll_at` erreicht ist - der Takt selbst kostet keine Anfragen.
SCHEDULER_INTERVAL_SECONDS = 5 * 60

DAILY_REQUEST_LIMIT = 50
#: Bleibt fuer andere Clients am selben Konto (HA-Integration) uebrig.
RESERVE_REQUESTS = 10

WINDOW_DAYS = 30
OVERLAP_DAYS = 3
BACKFILL_DELAY_MINUTES = 2

#: Ein Access-Token, das in weniger als dieser Zeit ablaeuft, wird vorher
#: erneuert - ein Abruf mit mehreren Seiten soll nicht mittendrin scheitern.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

#: Abgelehnte Anmeldung heilt nicht von selbst; trotzdem gelegentlich erneut
#: versuchen, falls BMW nur kurz gestoert war.
AUTH_ERROR_BACKOFF_HOURS = 6
ERROR_BACKOFF_MINUTES = 60
RATE_LIMIT_BACKOFF_HOURS = 6

LOG_MAX_ENTRIES = 300

EXTERNAL_ID_PREFIX = "bmw"


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------

def log_event(
    db: Session,
    config: models.BmwCarDataConfig,
    event: str,
    message: str,
    *,
    level: str = "info",
    payload: dict | None = None,
) -> None:
    db.add(
        models.BmwCarDataLogEntry(
            user_id=config.user_id,
            vehicle_id=config.vehicle_id,
            level=level,
            event=event,
            message=message,
            payload=json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            if payload is not None
            else None,
        )
    )
    db.flush()
    old_ids = [
        row.id
        for row in db.query(models.BmwCarDataLogEntry.id)
        .filter(models.BmwCarDataLogEntry.vehicle_id == config.vehicle_id)
        .order_by(models.BmwCarDataLogEntry.created_at.desc())
        .offset(LOG_MAX_ENTRIES)
        .all()
    ]
    if old_ids:
        db.query(models.BmwCarDataLogEntry).filter(
            models.BmwCarDataLogEntry.id.in_(old_ids)
        ).delete(synchronize_session=False)


# --------------------------------------------------------------------------
# Kontingent
# --------------------------------------------------------------------------

def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def requests_left(config: models.BmwCarDataConfig) -> int:
    used = config.requests_today if config.requests_day == _today() else 0
    return max(DAILY_REQUEST_LIMIT - RESERVE_REQUESTS - (used or 0), 0)


def _count_requests(config: models.BmwCarDataConfig, count: int) -> None:
    if config.requests_day != _today():
        config.requests_day = _today()
        config.requests_today = 0
    config.requests_today = (config.requests_today or 0) + count


def _next_utc_day() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day) + timedelta(days=1, minutes=5)


# --------------------------------------------------------------------------
# Anmeldung
# --------------------------------------------------------------------------

def is_connected(config: models.BmwCarDataConfig) -> bool:
    return bool(config.refresh_token)


def _store_tokens(config: models.BmwCarDataConfig, tokens: bmw_cardata.Tokens) -> None:
    config.access_token = tokens.access_token
    config.refresh_token = tokens.refresh_token
    config.access_expires_at = tokens.access_expires_at
    config.refresh_expires_at = tokens.refresh_expires_at


def _clear_pending(config: models.BmwCarDataConfig) -> None:
    config.pending_device_code = None
    config.pending_code_verifier = None
    config.pending_user_code = None
    config.pending_verification_uri = None
    config.pending_expires_at = None
    config.pending_interval = None


def disconnect(config: models.BmwCarDataConfig) -> None:
    config.access_token = None
    config.refresh_token = None
    config.access_expires_at = None
    config.refresh_expires_at = None
    _clear_pending(config)


def start_authorization(
    db: Session, config: models.BmwCarDataConfig, *, client: httpx.Client | None = None
) -> bmw_cardata.DeviceCode:
    verifier = bmw_cardata.new_code_verifier()
    code = bmw_cardata.request_device_code(config.client_id, verifier, client=client)
    config.pending_device_code = code.device_code
    config.pending_code_verifier = verifier
    config.pending_user_code = code.user_code
    config.pending_verification_uri = code.verification_uri_complete or code.verification_uri
    config.pending_expires_at = datetime.utcnow() + timedelta(seconds=code.expires_in)
    config.pending_interval = code.interval
    log_event(db, config, "auth", "Geraetebestaetigung bei BMW gestartet.")
    db.commit()
    return code


def finish_authorization(
    db: Session, config: models.BmwCarDataConfig, *, client: httpx.Client | None = None
) -> str:
    """Ein Versuch, den Geraetecode einzuloesen.

    Rueckgabe `pending` | `connected` | `expired`. Fehler, die nicht "noch
    nicht bestaetigt" sind, gehen als CarDataError an den Router.
    """
    if not config.pending_device_code or not config.pending_code_verifier:
        return "connected" if is_connected(config) else "expired"
    if config.pending_expires_at and config.pending_expires_at < datetime.utcnow():
        _clear_pending(config)
        db.commit()
        return "expired"
    try:
        tokens = bmw_cardata.exchange_device_code(
            config.client_id,
            config.pending_device_code,
            config.pending_code_verifier,
            client=client,
        )
    except AuthorizationPending:
        return "pending"
    except CarDataAuthError as exc:
        _clear_pending(config)
        log_event(db, config, "auth", str(exc), level="error")
        db.commit()
        return "expired"

    _store_tokens(config, tokens)
    _clear_pending(config)
    config.last_status = "connected"
    config.last_error = None
    log_event(db, config, "auth", "Mit BMW verbunden.")
    _check_vin(db, config, client=client)
    # Frisch verbunden: gleich abfragen, nicht erst nach dem Intervall.
    config.next_poll_at = None
    db.commit()
    return "connected"


def _check_vin(
    db: Session, config: models.BmwCarDataConfig, *, client: httpx.Client | None = None
) -> None:
    """Fahrzeuge des Kontos nachsehen (eine Anfrage): eine fehlende FIN wird
    uebernommen, wenn genau ein Fahrzeug als Hauptnutzer zugeordnet ist;
    eine eingetragene, die nicht dabei ist, wird gemeldet."""
    if requests_left(config) < 1:
        return
    try:
        mappings = bmw_cardata.get_mappings(config.access_token, client=client)
    except CarDataError as exc:
        log_event(db, config, "api_error", f"Fahrzeugliste nicht abrufbar: {exc}", level="warning")
        return
    finally:
        _count_requests(config, 1)
    vins = bmw_cardata.primary_vins(mappings)
    if not config.vin and len(vins) == 1:
        config.vin = vins[0]
        log_event(db, config, "auth", "FIN aus dem BMW-Konto uebernommen.")
    elif config.vin and vins and config.vin.upper() not in vins:
        config.last_error = (
            "Die eingetragene FIN ist im BMW-Konto nicht als Hauptnutzer zugeordnet "
            f"({len(vins)} andere Fahrzeuge gefunden)."
        )
        log_event(db, config, "auth", config.last_error, level="warning")


def _access_token(
    db: Session, config: models.BmwCarDataConfig, *, client: httpx.Client | None = None
) -> str:
    if (
        config.access_token
        and config.access_expires_at
        and config.access_expires_at - TOKEN_REFRESH_MARGIN > datetime.utcnow()
    ):
        return config.access_token
    if not config.refresh_token:
        raise CarDataAuthError("Nicht mit BMW verbunden")
    tokens = bmw_cardata.refresh_tokens(config.client_id, config.refresh_token, client=client)
    _store_tokens(config, tokens)
    db.flush()
    return tokens.access_token


# --------------------------------------------------------------------------
# Ladevorgaenge anlegen
# --------------------------------------------------------------------------

def _external_id(vehicle: models.Vehicle, item: HistorySession) -> str:
    # Vehicle.id statt FIN, aus demselben Grund wie im MyŠkoda-Poller: die
    # external_session_id muss Klartext bleiben (Dublettencheck per SQL).
    return f"{EXTERNAL_ID_PREFIX}-{vehicle.id}-{item.raw_start}"


def _note(item: HistorySession) -> str:
    parts = ["Erfasst ueber BMW CarData."]
    if item.address:
        parts.append(f"Ort laut BMW: {item.address}.")
    if item.max_power_kw is not None:
        parts.append(
            f"Leistung bis {item.max_power_kw:.1f} kW, Lade-Art daraus abgeleitet."
        )
    if item.provider_names:
        parts.append("Ladepunkt laut BMW: " + ", ".join(item.provider_names) + ".")
    if item.calculated_cost is not None:
        parts.append(
            f"Kostenschaetzung von BMW: {item.calculated_cost:.2f} {item.currency or ''}".rstrip()
            + "."
        )
    if item.energy_kwh is None:
        parts.append("BMW hat keine Energie gemeldet, kWh aus dem SoC geschaetzt.")
    return " ".join(parts)


def _apply_named_provider(
    session: models.ChargingSession, names: list[str], db: Session
) -> None:
    """Anbieter ueber den von BMW erkannten Ladepunkt, wenn ein eigener
    Anbieter genau so heisst. Preisvorschlag wie apply_provider_price()."""
    if session.provider_id or not names:
        return
    lowered = [n.lower() for n in names]
    provider = (
        db.query(models.Provider)
        .filter(
            models.Provider.user_id == session.user_id,
            func.lower(models.Provider.name).in_(lowered),
        )
        .first()
    )
    if not provider:
        return
    session.provider_id = provider.id
    price = (
        provider.last_price_dc_per_kwh
        if session.charging_type == models.ChargingType.DC
        else provider.last_price_ac_per_kwh
    )
    if price is not None:
        session.price_per_kwh = price
        if session.energy_kwh:
            session.price_total = round(price * session.energy_kwh, 2)


def import_history_item(
    db: Session,
    config: models.BmwCarDataConfig,
    vehicle: models.Vehicle,
    raw: dict,
) -> models.ChargingSession | None:
    """Legt einen Ladevorgang aus einem Eintrag der Historie an. None, wenn er
    schon existiert oder kein Ladevorgang ist."""
    item = HistorySession.parse(raw)
    if item is None:
        return None
    external_id = _external_id(vehicle, item)
    exists = (
        db.query(models.ChargingSession.id)
        .filter(
            models.ChargingSession.external_session_id == external_id,
            models.ChargingSession.user_id == config.user_id,
        )
        .first()
    )
    if exists:
        return None
    if not item.is_charge:
        log_event(
            db,
            config,
            "session_skipped",
            f"Eintrag vom {item.start_time:%d.%m.%Y %H:%M} ohne Energie und ohne "
            "SoC-Zuwachs - kein Ladevorgang.",
            payload=raw,
        )
        return None

    session = models.ChargingSession(
        vehicle_id=vehicle.id,
        user_id=config.user_id,
        start_time=item.start_time,
        end_time=item.end_time,
        charging_type=models.ChargingType(item.charging_type) if item.charging_type else None,
        soc_start=item.soc_start,
        soc_end=item.soc_end,
        odometer_km=item.odometer_km,
        latitude=item.latitude,
        longitude=item.longitude,
        energy_kwh=item.energy_kwh,
        energy_is_estimated=False,
        # Netzseitig gemessen, also wie an der Ladesaeule (siehe Modul-Doc).
        energy_meter="charger" if item.energy_kwh is not None else None,
        external_session_id=external_id,
        source=models.SessionSource.AUTOMATIC,
        needs_review=True,
        notes=_note(item),
    )
    resolve_location(session, config.user_id, db)
    estimate_energy_kwh(session, vehicle)
    apply_provider_price(session, db)
    _apply_named_provider(session, item.provider_names, db)
    normalize_energy_meter(session, db)
    # Laeuft fuer das Fahrzeug noch eine zweite Quelle (z.B. Home Assistant),
    # gibt es den Vorgang vielleicht schon - dann ergaenzen statt doppelt.
    match, changes = merge_or_none(db, session, "BMW CarData")
    if match is not None:
        if changes:
            log_event(
                db,
                config,
                "session_merged",
                f"Ladevorgang vom {item.start_time:%d.%m.%Y %H:%M} gibt es schon aus einer "
                "anderen Quelle - ergänzt: " + ", ".join(changes) + ".",
                payload=raw,
            )
        return None
    db.add(session)
    db.flush()
    log_event(
        db,
        config,
        "session_created",
        f"Ladevorgang vom {item.start_time:%d.%m.%Y %H:%M} angelegt: "
        f"SoC {item.soc_start if item.soc_start is not None else '?'} -> "
        f"{item.soc_end if item.soc_end is not None else '?'} %, "
        f"{session.energy_kwh if session.energy_kwh is not None else '?'} kWh.",
        payload=raw,
    )
    return session


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

def poll_vehicle(
    db: Session, config: models.BmwCarDataConfig, *, client: httpx.Client | None = None
) -> None:
    """Ein Abruf der Ladehistorie. Wirft nie weiter (wie der MyŠkoda-Poller),
    das Ergebnis steht in `last_status`/`last_error` und im Protokoll."""
    previous_status = config.last_status
    now = datetime.utcnow()
    config.last_poll_at = now
    try:
        if not is_connected(config):
            config.last_status = "not_connected"
            config.last_error = "Noch nicht mit BMW verbunden."
            config.next_poll_at = None
            return
        if not config.vin:
            config.last_status = "error"
            config.last_error = "Keine FIN eingetragen."
            config.next_poll_at = now + timedelta(hours=config.poll_interval_hours)
            return
        budget = requests_left(config)
        if budget < 1:
            config.last_status = "rate_limited"
            config.last_error = (
                f"Eigenes Tageskontingent verbraucht ({DAILY_REQUEST_LIMIT - RESERVE_REQUESTS} "
                "Anfragen), weiter am naechsten Tag."
            )
            config.next_poll_at = _next_utc_day()
            return

        vehicle = db.get(models.Vehicle, config.vehicle_id)
        if vehicle is None:
            return
        token = _access_token(db, config, client=client)

        since = config.history_cursor or now - timedelta(days=config.initial_history_days)
        until = min(since + timedelta(days=WINDOW_DAYS), now)
        try:
            result = bmw_cardata.get_charging_history(
                token,
                config.vin,
                since,
                until,
                client=client,
                max_pages=min(bmw_cardata.MAX_PAGES, budget),
            )
        except CarDataError:
            _count_requests(config, 1)
            raise
        _count_requests(config, result.requests)

        created = 0
        for raw in result.sessions:
            if import_history_item(db, config, vehicle, raw) is not None:
                created += 1

        backfilling = until < now
        if result.complete:
            config.history_cursor = until - timedelta(days=OVERLAP_DAYS) if not backfilling else until
        elif result.sessions:
            # Mehr Seiten als erlaubt: bis zum juengsten gelesenen Vorgang weiter.
            latest = max(
                (bmw_cardata.utc_from_epoch(s.get("startTime")) for s in result.sessions),
                default=None,
                key=lambda value: value or datetime.min,
            )
            if latest and latest > since:
                config.history_cursor = latest
        config.last_status = "ok"
        config.last_error = None
        config.last_imported_count = created
        config.next_poll_at = (
            now + timedelta(minutes=BACKFILL_DELAY_MINUTES)
            if backfilling or not result.complete
            else now + timedelta(hours=config.poll_interval_hours)
        )
        log_event(
            db,
            config,
            "poll",
            f"Ladehistorie {since:%d.%m.%Y} bis {until:%d.%m.%Y} gelesen: "
            f"{len(result.sessions)} Eintraege, {created} neu angelegt, "
            f"{result.requests} Anfrage(n).",
        )
    except CarDataAuthError as exc:
        config.last_status = "auth_error"
        config.last_error = str(exc)
        config.next_poll_at = now + timedelta(hours=AUTH_ERROR_BACKOFF_HOURS)
        log_event(db, config, "api_error", str(exc), level="error")
    except CarDataRateLimitError as exc:
        config.last_status = "rate_limited"
        config.last_error = str(exc)
        config.next_poll_at = now + timedelta(hours=RATE_LIMIT_BACKOFF_HOURS)
        log_event(db, config, "rate_limit", str(exc), level="warning")
    except CarDataError as exc:
        config.last_status = "error"
        config.last_error = str(exc)
        config.next_poll_at = now + timedelta(minutes=ERROR_BACKOFF_MINUTES)
        log_event(db, config, "api_error", str(exc), level="error")
    except Exception as exc:  # pragma: no cover - Schutznetz fuer die Schleife
        logger.exception("Unerwarteter Fehler beim CarData-Abruf")
        config.last_status = "error"
        config.last_error = str(exc)[:500]
        config.next_poll_at = now + timedelta(minutes=ERROR_BACKOFF_MINUTES)
        log_event(db, config, "api_error", str(exc)[:500], level="error")
    finally:
        db.commit()
        notifications.notify_cardata_status(db, config, previous_status)


def run_due_polls() -> None:
    """Vom Scheduler in main.py aufgerufen."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        configs = (
            db.query(models.BmwCarDataConfig)
            .filter(models.BmwCarDataConfig.enabled == True)  # noqa: E712
            .all()
        )
        for config in configs:
            if not config.refresh_token or not config.vin:
                continue
            if config.next_poll_at and config.next_poll_at > now:
                continue
            try:
                poll_vehicle(db, config)
            except Exception:
                logger.exception(
                    "Unerwarteter Fehler im CarData-Poller fuer Fahrzeug %s", config.vehicle_id
                )
                db.rollback()
    finally:
        db.close()
