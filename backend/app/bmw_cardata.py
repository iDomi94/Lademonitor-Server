"""Client fuer BMW CarData (offizielle Kunden-API von BMW/MINI).

Doku: https://bmw-cardata.bmwgroup.com/customer/public/api-documentation
(Swagger "customer-api-v1"; Host `api-cardata.bmwgroup.com`).

Dritte Quelle fuer automatisch erfasste Ladevorgaenge, neben dem
Home-Assistant-Push und dem MyŠkoda-Poller. Anders als bei Škoda gibt es hier
einen **Endpunkt fuer die Ladehistorie** (`/chargingHistory`): BMW liefert
abgeschlossene Ladevorgaenge fix und fertig mit Start/Ende, SoC von/bis,
Kilometerstand, Position und - der eigentliche Gewinn - der aus dem Netz
bezogenen Energie (`energyConsumedFromPowerGridKwh`). Eine Zustandsmaschine
wie in myskoda_poller.py ist deshalb nicht noetig; der Poller holt nur
regelmaessig die Historie und legt an, was neu ist.

Anmeldung per **OAuth2 Device Code Flow mit PKCE**: der Nutzer legt im
MyBMW-Portal selbst eine Client-ID an (Abschnitt "BMW CarData"), der Server
fordert damit einen Geraetecode an, der Nutzer bestaetigt im Browser bei BMW,
und der Server tauscht den Code gegen Tokens. Zugangsdaten des BMW-Kontos
sieht der Server nie.

Zwei harte Eigenschaften der API:

1. **50 Anfragen pro 24 Stunden** pro Konto, geteilt ueber alle Fahrzeuge und
   alle Clients (also auch mit einer parallel laufenden HA-Integration
   `bmw-cardata-ha`). Fehler `CU-429`. Deshalb fragt der Poller nur alle paar
   Stunden und fuehrt selbst Buch ueber die Anfragen des Tages.
2. **Tokens laufen ab**: Access-Token nach 1 h, Refresh-Token nach 2 Wochen.
   Jeder Refresh liefert einen neuen Refresh-Token - solange der Server
   laeuft und mindestens alle zwei Wochen abfragt, bleibt die Anmeldung also
   bestehen. Danach (oder wenn die Client-ID im Portal geloescht wird) muss
   der Nutzer die Geraetebestaetigung wiederholen.

Bewusst synchron (httpx.Client) wie der Rest der App, siehe myskoda.py.
"""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AUTH_BASE_URL = "https://customer.bmwgroup.com"
DEVICE_CODE_URL = f"{AUTH_BASE_URL}/gcdm/oauth/device/code"
TOKEN_URL = f"{AUTH_BASE_URL}/gcdm/oauth/token"
API_BASE_URL = "https://api-cardata.bmwgroup.com"
API_VERSION = "v1"

#: Nur lesender Zugriff auf die REST-API. Der Datenstrom per MQTT
#: (`cardata:streaming:read`) wird bewusst nicht angefordert: die Ladehistorie
#: reicht fuer den Lademonitor, und BMW erlaubt nur EINE Stream-Verbindung je
#: Konto - die soll frei bleiben fuer eine Home-Assistant-Integration.
SCOPE = "authenticate_user openid cardata:api:read"

#: Grant-Type des Device Code Flows (RFC 8628).
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

REQUEST_TIMEOUT = 30.0

#: Mehr Seiten holt ein einzelner Abruf nicht - jede Seite ist eine Anfrage
#: aus dem Tageskontingent.
MAX_PAGES = 5


class CarDataError(Exception):
    """Basisfehler fuer jede fehlgeschlagene API-Interaktion."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class CarDataConnectionError(CarDataError):
    """Die API war gar nicht erreichbar (Timeout, DNS, TLS, ...)."""


class CarDataAuthError(CarDataError):
    """Anmeldung ungueltig: Refresh-Token abgelaufen/widerrufen, Client-ID
    geloescht oder ohne CarData-API-Freigabe. Heilt nicht von selbst - der
    Nutzer muss die Geraetebestaetigung wiederholen."""


class CarDataRateLimitError(CarDataError):
    """Tageskontingent aufgebraucht (HTTP 429 / CU-429)."""


class AuthorizationPending(CarDataError):
    """Device Code Flow: der Nutzer hat im Browser noch nicht bestaetigt."""


# --------------------------------------------------------------------------
# PKCE und Antwortauswertung
# --------------------------------------------------------------------------

def new_code_verifier() -> str:
    """Zufaelliger PKCE-Verifier (RFC 7636: 43-128 Zeichen)."""
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_code(body: Any) -> str | None:
    """BMW meldet Fehler der Kunden-API als `{"exveErrorId": "CU-429", ...}`,
    der Anmeldedienst nach OAuth-Art als `{"error": "invalid_grant"}`."""
    if not isinstance(body, dict):
        return None
    for key in ("exveErrorId", "errorId", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _error_detail(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("exveErrorMsg", "error_description", "message", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _api_error(response: httpx.Response) -> CarDataError:
    body = _json(response)
    code = _error_code(body)
    detail = _error_detail(body)
    status = response.status_code
    suffix = f" ({code}: {detail})" if code and detail else (f" ({code or detail})" if code or detail else "")

    if status == 429 or code == "CU-429":
        return CarDataRateLimitError(
            "Tageskontingent der CarData-API erreicht (50 Anfragen je 24 Stunden)" + suffix,
            status=status,
            code=code,
        )
    if status == 401 or code in {"CU-100", "CU-101", "CU-102", "CU-103"}:
        return CarDataAuthError(
            "BMW hat die Anmeldung nicht akzeptiert" + suffix, status=status, code=code
        )
    if status == 403 or code == "CU-104":
        return CarDataError(
            "Kein Zugriff auf dieses Fahrzeug - die FIN muss im MyBMW-Konto als "
            "Hauptnutzer (PRIMARY) zugeordnet sein und die Client-ID die CarData-API "
            "freigeschaltet haben" + suffix,
            status=status,
            code=code,
        )
    if code == "CU-120":
        return CarDataError("Ungueltige FIN" + suffix, status=status, code=code)
    return CarDataError(f"CarData-API antwortete mit HTTP {status}{suffix}", status=status, code=code)


# --------------------------------------------------------------------------
# Anmeldung (Device Code Flow)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(slots=True)
class Tokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def _post_form(url: str, data: dict[str, str], client: httpx.Client | None) -> httpx.Response:
    owns = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        return client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise CarDataConnectionError(f"BMW-Anmeldedienst nicht erreichbar: {exc}") from exc
    finally:
        if owns:
            client.close()


def request_device_code(
    client_id: str, verifier: str, *, client: httpx.Client | None = None
) -> DeviceCode:
    response = _post_form(
        DEVICE_CODE_URL,
        {
            "client_id": client_id,
            "response_type": "device_code",
            "scope": SCOPE,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        },
        client,
    )
    body = _json(response)
    if response.status_code >= 400 or not isinstance(body, dict) or not body.get("device_code"):
        code = _error_code(body)
        detail = _error_detail(body)
        raise CarDataError(
            "BMW hat keinen Geraetecode ausgegeben - Client-ID pruefen und im Portal "
            "\"CarData API\" freischalten"
            + (f" ({code or ''}{': ' if code and detail else ''}{detail or ''})" if code or detail else ""),
            status=response.status_code,
            code=code,
        )
    return DeviceCode(
        device_code=str(body["device_code"]),
        user_code=str(body.get("user_code") or ""),
        verification_uri=str(body.get("verification_uri") or ""),
        verification_uri_complete=body.get("verification_uri_complete"),
        expires_in=int(body.get("expires_in") or 300),
        interval=max(int(body.get("interval") or 5), 1),
    )


def _tokens_from(body: Any, now: datetime) -> Tokens:
    if not isinstance(body, dict) or not body.get("access_token") or not body.get("refresh_token"):
        raise CarDataError("Antwort des BMW-Anmeldedienstes enthielt keine Tokens")
    return Tokens(
        access_token=str(body["access_token"]),
        refresh_token=str(body["refresh_token"]),
        access_expires_at=now + timedelta(seconds=int(body.get("expires_in") or 3600)),
        # Nicht in jeder Antwort enthalten - die dokumentierte Laufzeit ist zwei Wochen.
        refresh_expires_at=now
        + timedelta(seconds=int(body.get("refresh_expires_in") or 14 * 24 * 3600)),
    )


def exchange_device_code(
    client_id: str, device_code: str, verifier: str, *, client: httpx.Client | None = None
) -> Tokens:
    """Ein einzelner Versuch. Wirft `AuthorizationPending`, solange der Nutzer
    im Browser noch nicht bestaetigt hat - der Aufrufer (die Web-UI) fragt im
    vorgegebenen Intervall erneut."""
    now = datetime.utcnow()
    response = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": DEVICE_GRANT_TYPE,
            "code_verifier": verifier,
        },
        client,
    )
    body = _json(response)
    if response.status_code < 400:
        return _tokens_from(body, now)
    code = _error_code(body)
    if code in {"authorization_pending", "slow_down"}:
        raise AuthorizationPending("Noch nicht bestaetigt", status=response.status_code, code=code)
    if code == "expired_token":
        raise CarDataAuthError(
            "Der Geraetecode ist abgelaufen - Anmeldung neu starten",
            status=response.status_code,
            code=code,
        )
    if code == "access_denied":
        raise CarDataAuthError(
            "Die Anmeldung wurde bei BMW abgelehnt", status=response.status_code, code=code
        )
    detail = _error_detail(body)
    raise CarDataError(
        f"Token-Abruf fehlgeschlagen (HTTP {response.status_code}"
        + (f", {code}" if code else "")
        + (f": {detail}" if detail else "")
        + ")",
        status=response.status_code,
        code=code,
    )


def refresh_tokens(
    client_id: str, refresh_token: str, *, client: httpx.Client | None = None
) -> Tokens:
    now = datetime.utcnow()
    response = _post_form(
        TOKEN_URL,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
        client,
    )
    body = _json(response)
    if response.status_code < 400:
        return _tokens_from(body, now)
    code = _error_code(body)
    if response.status_code in (400, 401) or code in {"invalid_grant", "invalid_client"}:
        raise CarDataAuthError(
            "Die BMW-Anmeldung ist abgelaufen oder wurde widerrufen - in den Einstellungen "
            "erneut mit BMW verbinden" + (f" ({code})" if code else ""),
            status=response.status_code,
            code=code,
        )
    raise CarDataError(
        f"Token-Erneuerung fehlgeschlagen (HTTP {response.status_code})",
        status=response.status_code,
        code=code,
    )


# --------------------------------------------------------------------------
# Kunden-API
# --------------------------------------------------------------------------

def _api_get(
    access_token: str,
    path: str,
    params: dict[str, str] | None,
    client: httpx.Client | None,
) -> Any:
    owns = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-version": API_VERSION,
        "Accept": "application/json",
    }
    try:
        response = client.get(f"{API_BASE_URL}{path}", params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise CarDataConnectionError(f"CarData-API nicht erreichbar: {exc}") from exc
    finally:
        if owns:
            client.close()
    if response.status_code >= 400:
        raise _api_error(response)
    body = _json(response)
    if body is None:
        raise CarDataError("Antwort der CarData-API war kein JSON")
    return body


def get_mappings(access_token: str, *, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Fahrzeuge des Kontos samt Zuordnungsart (PRIMARY/SECONDARY). Nur mit
    PRIMARY liefert BMW Daten."""
    body = _api_get(access_token, "/customers/vehicles/mappings", None, client)
    if isinstance(body, dict):
        body = body.get("mappings") or body.get("data") or []
    return [m for m in body if isinstance(m, dict)] if isinstance(body, list) else []


def primary_vins(mappings: list[dict[str, Any]]) -> list[str]:
    vins = []
    for mapping in mappings:
        vin = mapping.get("vin")
        kind = str(mapping.get("mappingType") or "").upper()
        if isinstance(vin, str) and vin and kind in {"", "PRIMARY"}:
            vins.append(vin.upper())
    return vins


def _iso(when: datetime) -> str:
    """Naives UTC -> `2026-10-11T06:00:00.000Z`, wie die API es erwartet."""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(slots=True)
class HistoryResult:
    sessions: list[dict[str, Any]]
    requests: int
    complete: bool


def get_charging_history(
    access_token: str,
    vin: str,
    since: datetime,
    until: datetime,
    *,
    client: httpx.Client | None = None,
    max_pages: int = MAX_PAGES,
) -> HistoryResult:
    """Abgeschlossene Ladevorgaenge im Zeitraum, ueber alle Seiten.

    `complete` ist False, wenn nach `max_pages` noch Seiten uebrig waren - der
    Aufrufer darf das Fenster dann nicht als erledigt abhaken.
    """
    sessions: list[dict[str, Any]] = []
    next_token: str | None = None
    requests = 0
    while True:
        params = {"from": _iso(since), "to": _iso(until)}
        if next_token:
            params["nextToken"] = next_token
        body = _api_get(access_token, f"/customers/vehicles/{vin}/chargingHistory", params, client)
        requests += 1
        data = body.get("data") if isinstance(body, dict) else None
        sessions.extend(s for s in (data or []) if isinstance(s, dict))
        next_token = body.get("next_token") or body.get("nextToken") if isinstance(body, dict) else None
        if not next_token:
            return HistoryResult(sessions=sessions, requests=requests, complete=True)
        if requests >= max_pages:
            return HistoryResult(sessions=sessions, requests=requests, complete=False)


# --------------------------------------------------------------------------
# Auswertung eines Eintrags der Ladehistorie
# --------------------------------------------------------------------------

#: Ein BMW laedt an Wechselstrom mit hoechstens 22 kW (meist 11 kW) - liegt die
#: Leistung eines Ladeabschnitts darueber, war es Gleichstrom.
DC_POWER_THRESHOLD_KW = 23.0

MILES_TO_KM = 1.609344


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    """Epoch-Sekunden -> naive LOKALZEIT (wie `start_time` in der ganzen App,
    siehe weather._to_utc). Ein Wert in Millisekunden wird erkannt."""
    number = _num(value)
    if number is None or number <= 0:
        return None
    if number > 10**11:
        number /= 1000
    return datetime.fromtimestamp(number)


def utc_from_epoch(value: Any) -> datetime | None:
    """Epoch-Sekunden -> naives UTC (fuer den Cursor, nicht fuer start_time)."""
    number = _num(value)
    if number is None or number <= 0:
        return None
    if number > 10**11:
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class HistorySession:
    start_time: datetime
    end_time: datetime | None
    soc_start: int | None
    soc_end: int | None
    energy_kwh: float | None
    odometer_km: int | None
    latitude: float | None
    longitude: float | None
    address: str | None
    charging_type: str | None
    max_power_kw: float | None
    provider_names: list[str]
    calculated_cost: float | None
    currency: str | None
    preconditioning: bool
    raw_start: int

    @classmethod
    def parse(cls, item: dict[str, Any]) -> "HistorySession | None":
        start = _timestamp(item.get("startTime"))
        if start is None:
            return None
        end = _timestamp(item.get("endTime"))

        energy = _num(item.get("energyConsumedFromPowerGridKwh"))
        if energy is not None and not 0 < energy <= 250:
            energy = None

        mileage = _num(item.get("mileage"))
        if mileage is not None and str(item.get("mileageUnits", "")).upper().endswith("MI"):
            mileage *= MILES_TO_KM
        odometer = int(round(mileage)) if mileage and mileage > 0 else None

        location = item.get("chargingLocation") if isinstance(item.get("chargingLocation"), dict) else {}
        lat = _num(location.get("mapMatchedLatitude"))
        lon = _num(location.get("mapMatchedLongitude"))
        if lat is None or lon is None or (lat == 0 and lon == 0):
            lat = lon = None
        address = location.get("formattedAddress") or location.get("municipality") or None

        powers = [
            p
            for block in item.get("chargingBlocks") or []
            if isinstance(block, dict) and (p := _num(block.get("averagePowerGridKw"))) is not None
        ]
        duration = _num(item.get("totalChargingDurationSec"))
        if energy and duration and duration > 0:
            powers.append(energy / (duration / 3600))
        max_power = round(max(powers), 1) if powers else None
        charging_type = None
        if max_power is not None:
            charging_type = "DC" if max_power > DC_POWER_THRESHOLD_KW else "AC"

        providers: list[str] = []
        point = item.get("publicChargingPoint")
        if isinstance(point, dict):
            for match in point.get("potentialChargingPointMatches") or []:
                name = match.get("providerName") if isinstance(match, dict) else None
                if isinstance(name, str) and name.strip() and name.strip() not in providers:
                    providers.append(name.strip())

        cost_info = item.get("chargingCostInformation")
        cost = currency = None
        if isinstance(cost_info, dict):
            cost = _num(cost_info.get("calculatedChargingCost"))
            currency = cost_info.get("currency") if isinstance(cost_info.get("currency"), str) else None

        def _soc(key: str) -> int | None:
            value = _num(item.get(key))
            return int(round(value)) if value is not None and 0 <= value <= 100 else None

        return cls(
            start_time=start,
            end_time=end,
            soc_start=_soc("displayedStartSoc"),
            soc_end=_soc("displayedSoc"),
            energy_kwh=round(energy, 2) if energy is not None else None,
            odometer_km=odometer,
            latitude=lat,
            longitude=lon,
            address=address,
            charging_type=charging_type,
            max_power_kw=max_power,
            provider_names=providers,
            calculated_cost=cost,
            currency=currency,
            preconditioning=bool(item.get("isPreconditioningActivated")),
            raw_start=int(_num(item.get("startTime")) or 0),
        )

    @property
    def is_charge(self) -> bool:
        """Ein Eintrag ohne Energie UND ohne SoC-Zuwachs ist kein Ladevorgang
        (z.B. nur Vorklimatisierung am Kabel)."""
        if self.energy_kwh is not None and self.energy_kwh >= 0.3:
            return True
        return (
            self.soc_start is not None
            and self.soc_end is not None
            and self.soc_end > self.soc_start
        )
