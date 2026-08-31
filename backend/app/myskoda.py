"""Client fuer die offizielle MyŠkoda Public API.

Doku: https://public.api.connect.skoda-auto.cz/docs

Zweite, von Home Assistant unabhaengige Quelle fuer automatisch erkannte
Ladevorgaenge (der bestehende HA-Push ueber `/api/sessions/auto` bleibt
unveraendert bestehen). Authentifizierung ausschliesslich per API-Key aus der
MyŠkoda-App (https://go.skoda.eu/api-keys) - keine Zugangsdaten, keine
inoffiziellen Endpunkte.

Bewusst synchron (httpx.Client) wie der Rest der App - der Scheduler in
main.py ruft den Poller ueber `asyncio.to_thread()` auf, damit nirgends
async/await mit der komplett synchronen DB-Schicht gemischt werden muss
(gleiches Muster wie webdav_backup.py).

Zwei harte Eigenschaften der API, die den Rest des Designs bestimmen:

1. **Rate-Limit von 20 Anfragen pro Stunde und API-Key** - geteilt ueber alle
   Fahrzeuge und alle Befehle. Deshalb pollt der Poller adaptiv (selten im
   Leerlauf, haeufiger waehrend eines Ladevorgangs) und respektiert die
   `RateLimit-*`-Header. Laeuft parallel noch die Home-Assistant-Integration
   mit DEMSELBEN Key, teilen sich beide dieses Kontingent.
2. **Reines Polling, kein Push** - die Antwort ist ein zwischengespeicherter
   Cloud-Stand mit eigenem `carCapturedTimestamp`, der bei schlafendem
   Fahrzeug Stunden bis Tage alt sein kann. Der Zeitstempel wird deshalb
   ueberall dem lokalen Abfragezeitpunkt vorgezogen.

Die Antwort wird absichtlich als rohes dict durchgereicht (`raw`) und nur
lesend ueber Hilfs-Properties ausgewertet: die API ist noch in der Beta,
neue Felder sollen ohne Code-Aenderung im Debug-Log auftauchen.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE_URL = "https://public.api.connect.skoda-auto.cz"
API_KEY_HEADER = "X-API-Key"
API_KEY_EXPIRES_HEADER = "X-API-Key-Expires-At"
API_KEY_MANAGEMENT_URL = "https://go.skoda.eu/api-keys"

HEADER_RATELIMIT_LIMIT = "RateLimit-Limit"
HEADER_RATELIMIT_REMAINING = "RateLimit-Remaining"
HEADER_RATELIMIT_RESET = "RateLimit-Reset"
HEADER_RETRY_AFTER = "Retry-After"

REQUEST_TIMEOUT = 30.0

#: Ladezustaende, die "Kabel steckt" bedeuten. Ein Ladevorgang im Sinne des
#: Lademonitors ist das Einstecken bis zum Ausstecken - inklusive der Phasen,
#: in denen gerade kein Strom fliesst (Timer laeuft noch, Zielladestand
#: erreicht, Ladung unterbrochen). Deckungsgleich mit den "aktiven Zustaenden"
#: des Home-Assistant-Blueprints, damit beide Quellen dieselben Vorgaenge
#: schneiden.
PLUGGED_IN_STATES = frozenset(
    {"CHARGING", "CONSERVING", "READY_FOR_CHARGING", "CHARGING_INTERRUPTED"}
)
#: Kabel nicht verbunden. "CONNECT_CABLE" ist die Aufforderung, das Kabel
#: anzuschliessen - also der Ruhezustand, NICHT "verbunden".
UNPLUGGED_STATES = frozenset({"CONNECT_CABLE"})
#: Rueckspeisung (V2H/V2G) - kein Ladevorgang, wird wie ausgesteckt behandelt.
DISCHARGING_STATES = frozenset({"DISCHARGING"})


class MySkodaError(Exception):
    """Basisfehler fuer jede fehlgeschlagene API-Interaktion."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        problem_type: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.problem_type = problem_type
        self.retry_after = retry_after


class MySkodaConnectionError(MySkodaError):
    """Die API war gar nicht erreichbar (Timeout, DNS, TLS, ...)."""


class MySkodaAuthError(MySkodaError):
    """API-Key ungueltig oder abgelaufen (HTTP 401)."""

    def __init__(self, *args: Any, expired: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.expired = expired


class MySkodaRateLimitError(MySkodaError):
    """Kontingent aufgebraucht oder Fahrzeug lehnt gerade ab (HTTP 429)."""


def _parse_dt(value: Any) -> datetime | None:
    """ISO-8601 aus der API -> naives UTC-datetime.

    Die gesamte DB dieser App speichert naive UTC-Zeitstempel
    (`datetime.utcnow()`), die API liefert dagegen durchgaengig mit Zeitzone
    ("...Z"). Ohne diese Normalisierung waeren Vergleiche zwischen beiden
    Welten ein TypeError.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _header_int(headers: Any, name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class RateLimit:
    """Stand der `RateLimit-*`-Header der letzten Antwort.

    Das Kontingent haengt am API-Key, nicht am Fahrzeug - bei mehreren
    Fahrzeugen mit demselben Key zaehlt also jede Abfrage auf denselben
    Zaehler.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_in_seconds: int | None = None

    @property
    def resets_at(self) -> datetime | None:
        if self.reset_in_seconds is None:
            return None
        return datetime.utcnow() + timedelta(seconds=self.reset_in_seconds)


@dataclass(slots=True)
class VehicleSnapshot:
    """Eine Antwort von `GET /api/v1/vehicles/{vin}`, lesend ausgewertet."""

    vin: str
    raw: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    rate_limit: RateLimit = field(default_factory=RateLimit)
    key_expires_at: datetime | None = None

    @classmethod
    def from_response(cls, vin: str, payload: dict[str, Any], **kwargs: Any) -> "VehicleSnapshot":
        vehicle = payload.get("vehicle")
        if not isinstance(vehicle, dict):
            vehicle = {}
        errors = payload.get("errors")
        if not isinstance(errors, list):
            errors = []
        return cls(
            vin=vehicle.get("vin") or vin,
            raw=vehicle,
            errors=[e for e in errors if isinstance(e, dict)],
            **kwargs,
        )

    def _part(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    # --- Ladezustand ---------------------------------------------------------

    @property
    def charging_state(self) -> str | None:
        """CONNECT_CABLE | CHARGING | CONSERVING | READY_FOR_CHARGING |
        DISCHARGING | CHARGING_INTERRUPTED - oder None, wenn das Fahrzeug
        gerade keinen Ladeteil geliefert hat."""
        state = self._part("charging").get("status", {}).get("state")
        return state if isinstance(state, str) else None

    @property
    def is_plugged_in(self) -> bool:
        return (self.charging_state or "") in PLUGGED_IN_STATES

    @property
    def soc_percent(self) -> int | None:
        battery = self._part("charging").get("status", {}).get("battery", {})
        return _as_int(battery.get("stateOfChargeInPercent")) if isinstance(battery, dict) else None

    @property
    def charge_power_kw(self) -> float | None:
        return _as_float(self._part("charging").get("status", {}).get("chargePowerInKw"))

    @property
    def charge_type(self) -> str | None:
        """AC | DC | OFF - bzw. None. Die API weist ausdruecklich darauf hin,
        dass spaeter neue Werte dazukommen koennen, deshalb wird der Wert
        nicht validiert, sondern nur weitergereicht."""
        value = self._part("charging").get("status", {}).get("chargeType")
        return value if isinstance(value, str) else None

    @property
    def target_soc_percent(self) -> int | None:
        return _as_int(
            self._part("charging").get("settings", {}).get("targetStateOfChargeInPercent")
        )

    @property
    def is_in_saved_location(self) -> bool | None:
        value = self._part("charging").get("isVehicleInSavedLocation")
        return value if isinstance(value, bool) else None

    @property
    def charging_captured_at(self) -> datetime | None:
        return _parse_dt(self._part("charging").get("carCapturedTimestamp"))

    # --- Uebriger Fahrzeugzustand --------------------------------------------

    @property
    def odometer_km(self) -> int | None:
        return _as_int(self._part("odometer").get("mileageInKm"))

    @property
    def odometer_captured_at(self) -> datetime | None:
        return _parse_dt(self._part("odometer").get("carCapturedTimestamp"))

    @property
    def parking_state(self) -> str | None:
        """PARKED | IN_MOTION - GPS-Koordinaten gibt es nur bei PARKED."""
        value = self._part("parkingPosition").get("state")
        return value if isinstance(value, str) else None

    @property
    def coordinates(self) -> tuple[float, float] | None:
        gps = self._part("parkingPosition").get("gpsCoordinates")
        if not isinstance(gps, dict):
            return None
        lat = _as_float(gps.get("latitude"))
        lon = _as_float(gps.get("longitude"))
        if lat is None or lon is None:
            return None
        return lat, lon

    @property
    def error_types(self) -> list[str]:
        return [str(e.get("type")) for e in self.errors if e.get("type")]

    @property
    def captured_at(self) -> datetime:
        """Bester verfuegbarer "Stand vom"-Zeitstempel fuer diesen Abruf.

        Der Ladeteil ist die relevanteste Quelle (er bestimmt Start/Ende eines
        Vorgangs); fehlt er, bleibt nur der lokale Abfragezeitpunkt.
        """
        return self.charging_captured_at or self.fetched_at

    def summary(self) -> dict[str, Any]:
        """Kompakte, menschenlesbare Zusammenfassung fuer Log und Web-UI."""
        coords = self.coordinates
        return {
            "vin": self.vin,
            "charging_state": self.charging_state,
            "soc_percent": self.soc_percent,
            "charge_power_kw": self.charge_power_kw,
            "charge_type": self.charge_type,
            "target_soc_percent": self.target_soc_percent,
            "is_in_saved_location": self.is_in_saved_location,
            "odometer_km": self.odometer_km,
            "parking_state": self.parking_state,
            "latitude": coords[0] if coords else None,
            "longitude": coords[1] if coords else None,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "errors": self.error_types,
            "rate_limit_remaining": self.rate_limit.remaining,
            "rate_limit_limit": self.rate_limit.limit,
        }


def _error_for(response: httpx.Response) -> MySkodaError:
    """Uebersetzt eine Fehlerantwort in einen typisierten Fehler.

    Die API antwortet nach RFC 9457 (`application/problem+json`) mit `type`
    und `detail` - beides wird uebernommen, damit im Debug-Log der echte
    Grund steht und nicht nur der Statuscode.
    """
    problem_type = None
    detail = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        problem_type = body.get("type")
        detail = body.get("detail") or body.get("title")

    status = response.status_code
    retry_after = _header_int(response.headers, HEADER_RETRY_AFTER)
    suffix = f" ({detail})" if detail else ""

    if status == 401:
        expired = isinstance(problem_type, str) and problem_type.endswith("api-key-expired")
        message = (
            "API-Key ist abgelaufen - in der MyŠkoda-App einen neuen erzeugen"
            if expired
            else "API-Key wurde nicht akzeptiert"
        )
        return MySkodaAuthError(
            message + suffix, status=status, problem_type=problem_type, expired=expired
        )
    if status == 403:
        return MySkodaError(
            "Der API-Key deckt dieses Fahrzeug nicht ab - beim Erzeugen des Keys "
            "muss die FIN ausgewaehlt worden sein" + suffix,
            status=status,
            problem_type=problem_type,
        )
    if status == 404:
        return MySkodaError(
            "Kein Fahrzeug zu dieser FIN gefunden" + suffix,
            status=status,
            problem_type=problem_type,
        )
    if status == 429:
        return MySkodaRateLimitError(
            "Rate-Limit erreicht (20 Anfragen/Stunde pro API-Key)" + suffix,
            status=status,
            problem_type=problem_type,
            retry_after=retry_after,
        )
    return MySkodaError(
        f"MyŠkoda-API antwortete mit HTTP {status}{suffix}",
        status=status,
        problem_type=problem_type,
        retry_after=retry_after,
    )


class MySkodaClient:
    """Duenner Client - genau ein lesender Endpunkt wird benoetigt.

    Fernbefehle (Laden starten/stoppen, Klima) bleiben bewusst aussen vor:
    der Lademonitor zeichnet auf, er steuert nicht.
    """

    def __init__(self, api_key: str, timeout: float = REQUEST_TIMEOUT) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def get_vehicle(self, vin: str) -> VehicleSnapshot:
        """Ein Abruf = eine Anfrage aus dem Stundenkontingent.

        `include` wird bewusst weggelassen: ohne den Parameter fehlen von der
        API nicht unterstuetzte Teile einfach, statt als Fehler in `errors`
        aufzutauchen.
        """
        url = f"{API_BASE_URL}/api/v1/vehicles/{vin}"
        headers = {API_KEY_HEADER: self._api_key, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise MySkodaConnectionError(f"MyŠkoda-API nicht erreichbar: {exc}") from exc

        rate_limit = RateLimit(
            limit=_header_int(response.headers, HEADER_RATELIMIT_LIMIT),
            remaining=_header_int(response.headers, HEADER_RATELIMIT_REMAINING),
            reset_in_seconds=_header_int(response.headers, HEADER_RATELIMIT_RESET),
        )
        key_expires_at = _parse_dt(response.headers.get(API_KEY_EXPIRES_HEADER))

        if response.status_code >= 400:
            error = _error_for(response)
            # Kontingent-/Ablaufinfos auch im Fehlerfall mitnehmen - gerade bei
            # 429 ist "wann wird wieder freigegeben" die interessante Angabe.
            error.rate_limit = rate_limit  # type: ignore[attr-defined]
            error.key_expires_at = key_expires_at  # type: ignore[attr-defined]
            raise error

        try:
            payload = response.json()
        except ValueError as exc:
            raise MySkodaError("Antwort der MyŠkoda-API war kein JSON") from exc
        if not isinstance(payload, dict):
            raise MySkodaError("Antwort der MyŠkoda-API hatte ein unerwartetes Format")

        return VehicleSnapshot.from_response(
            vin,
            payload,
            rate_limit=rate_limit,
            key_expires_at=key_expires_at,
        )
