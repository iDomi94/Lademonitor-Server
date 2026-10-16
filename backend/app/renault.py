"""Client fuer die Fahrzeugdaten der Renault-Gruppe (Renault, Dacia, Alpine).

Fuenfte Quelle fuer automatisch erfasste Ladevorgaenge neben Home Assistant,
MyŠkoda, BMW CarData und dem VW-Datenportal. Wie bei MyŠkoda gibt es nur
ZUSTAENDE (Ladestand, Stecker, Ladeleistung), keine verlaesslichen fertigen
Ladevorgaenge - die Zustandsmaschine dafuer liegt in `charge_detection.py`,
dieser Client liefert ihr je Abruf einen Bericht (`RenaultSnapshot`).

## Herkunft

Nachgebaut nach `renault-api` (https://github.com/hacf-fr/renault-api), der
Bibliothek hinter der Renault-Integration von Home Assistant
(`homeassistant/components/renault`). Uebernommen sind die oeffentlichen
API-Schluessel und Adressen je Land (`const.py`), der Ablauf der Anmeldung
(Gigya, inklusive der seit 2026 verlangten Bestaetigung per E-Mail-Code,
`gigya/__init__.py`, `renault_session.py`) und die Pfade der
Fahrzeugdaten (`kamereon/__init__.py`, `kamereon/models.py`). Die Bibliothek
steht unter der MIT-Lizenz:

    Copyright (c) 2020 epenet and the renault-api contributors
    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions: The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Bewusst nachgebaut statt als Abhaengigkeit eingebunden: `renault-api` ist
asynchron (aiohttp) und bringt marshmallow mit, der Rest dieser App ist
synchrones httpx. Gebraucht werden nur vier lesende Abrufe.

## Ablauf

1. **Gigya** (`accounts.login`, E-Mail + Passwort) liefert ein langlebiges
   Login-Token (`sessionInfo.cookieValue`). Das wird gespeichert, damit nicht
   bei jedem Neustart mit dem Passwort angemeldet werden muss - genau so macht
   es Home Assistant. Verlangt Gigya eine Zwei-Faktor-Bestaetigung
   (Fehlercode 403101), geht ein Code an die E-Mail-Adresse des Kontos; danach
   merkt sich Gigya das "Geraet" (Cookies `gmid`/`ucid`) rund 30 Tage.
2. Aus dem Login-Token wird je Viertelstunde ein JWT (`accounts.getJWT`).
3. **Kamereon** (Renaults Fahrzeug-API) mit JWT und API-Schluessel:
   Person -> Konto (`MYRENAULT`) -> Fahrzeuge -> Daten je FIN.

## Kontingent

Home Assistant rechnet mit 60 Abrufen je Stunde und Konto
(`MAX_CALLS_PER_HOURS`) und pausiert nach `err.func.wired.overloaded` 15
Minuten. Der Poller hier ist sparsamer (siehe renault_poller.py): normal nur
`battery-status`, die drei uebrigen Abrufe (Kilometerstand, Position,
Aussentemperatur) nur, wenn sich am Stecker etwas aendert.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .changelog import VERSION

logger = logging.getLogger(__name__)

USER_AGENT = f"lademonitor-server/{VERSION} (+https://github.com/iDomi94/Lademonitor-Server)"
REQUEST_TIMEOUT = 30.0

#: Nur fuer die Tests: ersetzt den HTTP-Transport (siehe tests/test_renault.py).
TRANSPORT: httpx.BaseTransport | None = None

# --------------------------------------------------------------------------
# Adressen und Schluessel je Land (aus renault-api, const.py)
# --------------------------------------------------------------------------

GIGYA_URL_EU = "https://accounts.eu1.gigya.com"
GIGYA_URL_US = "https://accounts.us1.gigya.com"
GIGYA_URL_AU = "https://gigya-prod-au1.renaultgroup.com"
GIGYA_KEY_EU = "3_VgdkgtIRH3AdHvJm-cjV2ug2EFE0lxt0IJzMC4MFqZjFpn_GYFXVdNZ19L7wZX0N"
KAMEREON_APIKEY = "YjkKtHmGfaceeuExUDKGxrLZGGvtVS0J"
KAMEREON_URL_EU = "https://api-wired-prod-1-euw1.wrd-aws.com"
KAMEREON_URL_US = "https://api-wired-prod-1-usw2.wrd-aws.com"
KAMEREON_URL_AU = "https://api-wired-prod-1-apn1.wrd-aws.com"


@dataclass(frozen=True)
class Locale:
    key: str
    gigya_url: str = GIGYA_URL_EU
    gigya_key: str = GIGYA_KEY_EU
    kamereon_url: str = KAMEREON_URL_EU

    @property
    def country(self) -> str:
        return self.key[-2:].upper()

    @property
    def language(self) -> str:
        return self.key.split("_")[0]


_EU_LOCALES = (
    "bg_BG", "cs_CZ", "da_DK", "de_DE", "de_AT", "de_CH", "en_GB", "en_IE",
    "es_ES", "fi_FI", "fr_FR", "fr_BE", "fr_CH", "hr_HR", "hu_HU", "it_IT",
    "it_CH", "nl_NL", "nl_BE", "no_NO", "pl_PL", "pt_PT", "ro_RO", "ru_RU",
    "sk_SK", "sl_SI", "sv_SE",
)

LOCALES: dict[str, Locale] = {key: Locale(key) for key in _EU_LOCALES}
LOCALES["fr_LU"] = Locale(
    "fr_LU", gigya_key="3_zt44Wl_wT9mnqn-BHrR19PvXj3wYRPQKLcPbGWawlatFR837KdxSZZStbBTDaqnb"
)
LOCALES["en_AU"] = Locale(
    "en_AU", gigya_url=GIGYA_URL_AU, gigya_key="4_GC9iGd3sH8sMJpz2x0q0CQ", kamereon_url=KAMEREON_URL_AU
)
LOCALES["es_MX"] = Locale(
    "es_MX", gigya_url=GIGYA_URL_US, gigya_key="4_yTFqPSsGxVyRXPZUM7t1Iw", kamereon_url=KAMEREON_URL_US
)

#: Gigya-Fehlercodes (gigya/enums.py, gigya/models.py, renault_session.py).
GIGYA_INVALID_CREDENTIALS = 403042
GIGYA_PENDING_TFA = 403101
GIGYA_INVALID_CODE = 400006
#: Login-Token abgelaufen oder widerrufen - neu anmelden.
GIGYA_TOKEN_EXPIRED = (403005, 403013)

#: Gueltigkeit eines JWT laut Anfrage (`expiration`); erneuert wird etwas frueher.
JWT_LIFETIME_SECONDS = 900
JWT_REFRESH_MARGIN_SECONDS = 120

#: Modelle, die die Ladeleistung in Watt statt kW melden (kamereon/models.py,
#: "reports-in-watts"): ZOE Phase 1.
MODELS_REPORTING_WATTS = frozenset({"X101VE"})

#: Ab dieser Leistung gilt ein Vorgang als Gleichstrom - ein Renault laedt AC
#: mit hoechstens 22 kW (ZOE). Dieselbe Grenze wie bei BMW CarData.
DC_POWER_THRESHOLD_KW = 23.0


# --------------------------------------------------------------------------
# Fehler
# --------------------------------------------------------------------------

class RenaultError(Exception):
    """Netz-, Server- oder Formatfehler."""


class RenaultAuthError(RenaultError):
    """Anmeldung abgelehnt (Passwort, abgelaufenes Login-Token)."""


class RenaultTwoFactorRequired(RenaultAuthError):
    """Gigya verlangt einen per E-Mail geschickten Code. `reg_token` startet
    die Bestaetigung (bewusst nicht in `str()`, damit er nicht ins Protokoll
    geraet)."""

    def __init__(self, message: str, reg_token: str) -> None:
        super().__init__(message)
        self.reg_token = reg_token


class RenaultInvalidCode(RenaultAuthError):
    """Der eingegebene Bestaetigungscode war falsch - derselbe Vorgang kann mit
    dem richtigen Code wiederholt werden."""


class RenaultRateLimitError(RenaultError):
    """`err.func.wired.overloaded` - das Kontingent des Kontos ist erschoepft."""


class RenaultNotSupportedError(RenaultError):
    """Das Fahrzeug unterstuetzt diesen Abruf nicht (501, 403, notFound) -
    bei Kilometerstand/Position/Temperatur kein Grund, den Abruf scheitern zu
    lassen."""


#: Kamereon-Fehlercodes -> Fehlerart (kamereon/models.py, COMMON_ERRRORS).
_KAMEREON_NOT_SUPPORTED = frozenset({
    "err.func.403",
    "err.tech.501",
    "err.func.wired.notFound",
    "err.func.wired.not-found",
    "err.func.wired.forbidden",
    "err.func.privacy.on",
})
_KAMEREON_UNAUTHORIZED = "err.func.wired.unauthorized"
_KAMEREON_QUOTA = "err.func.wired.overloaded"


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------

def parse_timestamp(raw: Any) -> datetime | None:
    """ISO-Zeitstempel ("2020-11-17T09:06:48+01:00", "...Z") -> naives UTC,
    wie alle Zeitstempel der Ladeerkennung."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kamereon_errors(payload: Any) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    result = []
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            result.append((str(error.get("errorCode") or ""), str(error.get("errorMessage") or "")))
    return result


def _error_detail(message: str) -> str:
    """Kamereon versteckt Details gern als JSON im errorMessage."""
    try:
        inner = json.loads(message)
    except (TypeError, ValueError):
        return message
    if not isinstance(inner, dict):
        return message
    parts = []
    for err in inner.get("errors") or []:
        if isinstance(err, dict):
            parts.append(" ".join(str(err[k]) for k in ("title", "detail") if err.get(k)))
    return ", ".join(p for p in parts if p) or message


# --------------------------------------------------------------------------
# Bericht
# --------------------------------------------------------------------------

#: `chargingStatus` (kamereon/enums.py, ChargeState).
CHARGE_STATES: dict[float, str] = {
    0.0: "NOT_IN_CHARGE",
    0.1: "WAITING_FOR_A_PLANNED_CHARGE",
    0.2: "CHARGE_ENDED",
    0.3: "WAITING_FOR_CURRENT_CHARGE",
    0.4: "ENERGY_FLAP_OPENED",
    1.0: "CHARGE_IN_PROGRESS",
    -1.3: "V2G_CHARGING_WAITING",
    -1.4: "V2L_CONNECTED",
    -1.5: "V2G_DISCHARGING",
    -1.6: "V2G_CHARGING_NORMAL",
    -1.0: "CHARGE_ERROR",
    -1.1: "UNAVAILABLE",
}
#: Zustaende, die ohne Steckerangabe "Kabel steckt" bedeuten.
PLUGGED_STATES = frozenset({
    "WAITING_FOR_A_PLANNED_CHARGE",
    "CHARGE_ENDED",
    "WAITING_FOR_CURRENT_CHARGE",
    "CHARGE_IN_PROGRESS",
    "V2G_CHARGING_WAITING",
    "V2G_CHARGING_NORMAL",
})
#: Rueckspeisung (V2L/V2G) zaehlt wie bei MyŠkoda als "nicht verbunden" - es
#: wird nicht geladen, sondern entladen.
DISCHARGING_STATES = frozenset({"V2L_CONNECTED", "V2G_DISCHARGING"})

#: `plugStatus` (kamereon/enums.py, PlugState).
PLUG_UNPLUGGED = 0
PLUG_PLUGGED = 1


@dataclass
class RenaultSnapshot:
    """Ein Abruf, uebersetzt in die Eigenschaften, die
    `charge_detection.apply_snapshot` erwartet (dieselben wie
    `myskoda.VehicleSnapshot`). `battery` ist die Antwort von
    `battery-status`, die uebrigen drei sind optional (nicht jeder Abruf holt
    sie, nicht jedes Fahrzeug liefert sie)."""

    battery: dict
    cockpit: dict | None = None
    location: dict | None = None
    hvac: dict | None = None
    model_code: str | None = None
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    is_in_saved_location = None
    charged_energy_kwh = None

    @property
    def charge_state(self) -> str | None:
        value = _number(self.battery.get("chargingStatus"))
        if value is None:
            return None
        return CHARGE_STATES.get(round(value, 1))

    @property
    def plug_connected(self) -> bool | None:
        value = _number(self.battery.get("plugStatus"))
        if value == PLUG_PLUGGED:
            return True
        if value == PLUG_UNPLUGGED:
            return False
        # PLUG_ERROR (-1), PLUG_UNKNOWN (-2147483648) oder fehlt
        return None

    @property
    def is_plugged_in(self) -> bool | None:
        state = self.charge_state
        if state in DISCHARGING_STATES:
            return False
        plug = self.plug_connected
        if plug is not None:
            return plug
        if state in PLUGGED_STATES:
            return True
        # NOT_IN_CHARGE, Fehler, unbekannt: mit oder ohne Kabel moeglich.
        return None

    @property
    def charging_state(self) -> str | None:
        """Lesbarer Zustand fuer Protokoll und Anzeige. None, wenn sich nicht
        sagen laesst, ob das Kabel steckt - dann entscheidet die
        Zustandsmaschine bewusst nichts (ein laufender Vorgang wuerde sonst
        faelschlich beendet)."""
        if self.is_plugged_in is None:
            return None
        state = self.charge_state
        if state:
            return state
        return "PLUGGED" if self.is_plugged_in else "UNPLUGGED"

    @property
    def soc_percent(self) -> int | None:
        value = _number(self.battery.get("batteryLevel"))
        if value is None or not 0 <= value <= 100:
            return None
        return int(round(value))

    @property
    def charge_power_kw(self) -> float | None:
        value = _number(self.battery.get("chargingInstantaneousPower"))
        if value is None or value < 0:
            return None
        # ZOE Phase 1 meldet Watt. Ohne Modellkennung faengt die Groessenordnung
        # das ab: kein Renault laedt mit mehr als 400 kW.
        if self.model_code in MODELS_REPORTING_WATTS or value > 400:
            value = value / 1000
        return round(value, 2)

    @property
    def charge_type(self) -> str | None:
        """Die API nennt AC/DC nicht. Abgeleitet aus der Leistung wie bei BMW
        CarData; ohne fliessenden Strom unbekannt."""
        power = self.charge_power_kw
        if not power:
            return None
        return "DC" if power > DC_POWER_THRESHOLD_KW else "AC"

    @property
    def odometer_km(self) -> int | None:
        value = _number((self.cockpit or {}).get("totalMileage"))
        if value is None or value <= 0:
            return None
        return int(round(value))

    @property
    def outside_temp_c(self) -> float | None:
        value = _number((self.hvac or {}).get("externalTemperature"))
        if value is None or not -50 <= value <= 60:
            return None
        return round(value, 1)

    @property
    def coordinates(self) -> tuple[float, float] | None:
        loc = self.location or {}
        lat = _number(loc.get("gpsLatitude"))
        lon = _number(loc.get("gpsLongitude"))
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        return (lat, lon)

    @property
    def captured_at(self) -> datetime:
        return parse_timestamp(self.battery.get("timestamp")) or self.fetched_at

    @property
    def raw(self) -> dict:
        return {
            "battery-status": self.battery,
            "cockpit": self.cockpit,
            "location": self.location,
            "hvac-status": self.hvac,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "charging_state": self.charging_state,
            "plug_connected": self.plug_connected,
            "soc_percent": self.soc_percent,
            "charge_power_kw": self.charge_power_kw,
            "charge_type": self.charge_type,
            "odometer_km": self.odometer_km,
            "outside_temp_c": self.outside_temp_c,
            "has_position": self.coordinates is not None,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@dataclass
class PendingTwoFactor:
    """Laufende Bestaetigung per E-Mail-Code (zwischen "Anmelden" und der
    Eingabe des Codes)."""

    reg_token: str
    gigya_assertion: str
    phv_token: str
    email_hint: str | None
    created: float = field(default_factory=time.monotonic)


class RenaultClient:
    """Synchroner Client fuer Gigya und Kamereon.

    `login_token` und `device_ids` werden von aussen gesetzt und wieder
    ausgelesen (der Poller speichert sie verschluesselt in der DB). Der JWT
    lebt nur im Prozess.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        locale: str = "de_DE",
        login_token: str | None = None,
        device_ids: dict[str, str] | None = None,
        account_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if locale not in LOCALES:
            raise RenaultError(f"Unbekanntes Land/Sprache: {locale}")
        self.locale = LOCALES[locale]
        self._email = email
        self._password = password
        self.login_token = login_token
        self.device_ids: dict[str, str] = dict(device_ids or {})
        self.account_id = account_id
        self.person_id: str | None = None
        self._jwt: str | None = None
        self._jwt_until = 0.0
        self._http = httpx.Client(
            transport=transport or TRANSPORT or httpx.HTTPTransport(retries=2),
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._http.close()

    # -- Gigya -----------------------------------------------------------------

    def _gigya(self, method: str, path: str, data: dict[str, Any]) -> dict:
        url = f"{self.locale.gigya_url}/{path}"
        headers = {}
        if self.device_ids:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.device_ids.items())
        try:
            if method == "POST":
                response = self._http.post(url, data=data, headers=headers)
            else:
                response = self._http.get(url, params=data, headers=headers)
        except httpx.HTTPError as exc:
            raise RenaultError(f"Renault-Anmeldedienst nicht erreichbar: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RenaultError(
                f"Renault-Anmeldedienst antwortete unerwartet (HTTP {response.status_code})."
            ) from exc
        code = int(payload.get("errorCode") or 0)
        if code:
            details = payload.get("errorDetails") or payload.get("errorMessage") or ""
            if code == GIGYA_PENDING_TFA and payload.get("regToken"):
                raise RenaultTwoFactorRequired(
                    "Renault verlangt eine Bestätigung per E-Mail-Code - in den Einstellungen "
                    "„Anmelden“ drücken und den Code eingeben.",
                    payload["regToken"],
                )
            if code == GIGYA_INVALID_CREDENTIALS:
                raise RenaultAuthError("E-Mail-Adresse oder Passwort falsch (Renault-Konto).")
            if code == GIGYA_INVALID_CODE and path.endswith("completeVerification"):
                raise RenaultInvalidCode("Der Bestätigungscode ist falsch.")
            if code in GIGYA_TOKEN_EXPIRED:
                raise RenaultAuthError(f"Anmeldung bei Renault abgelaufen ({details}).")
            raise RenaultError(f"Renault-Anmeldedienst meldet Fehler {code}: {details}")
        if response.status_code >= 400:
            raise RenaultError(f"Renault-Anmeldedienst antwortete mit HTTP {response.status_code}.")
        return payload

    def login(self) -> None:
        """Mit E-Mail und Passwort anmelden. Wirft `RenaultTwoFactorRequired`,
        wenn Gigya einen Code verlangt."""
        self.login_token = None
        self._jwt = None
        if not (self._email and self._password):
            raise RenaultAuthError("Ohne E-Mail-Adresse und Passwort keine Anmeldung bei Renault.")
        payload = self._gigya(
            "POST",
            "accounts.login",
            {"ApiKey": self.locale.gigya_key, "loginID": self._email, "password": self._password},
        )
        token = (payload.get("sessionInfo") or {}).get("cookieValue")
        if not token:
            raise RenaultError("Renault-Anmeldung ohne Login-Token in der Antwort.")
        self.login_token = token

    def _ensure_device_ids(self) -> None:
        """Geraetekennung (`gmid`/`ucid`) holen - die merkt sich Gigya nach der
        Bestaetigung, sonst fragt es bei jeder Anmeldung erneut nach einem Code."""
        if self.device_ids:
            return
        try:
            response = self._http.get(
                f"{self.locale.gigya_url}/accounts.webSdkBootstrap",
                params={"APIKey": self.locale.gigya_key},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RenaultError(f"Renault-Anmeldedienst nicht erreichbar: {exc}") from exc
        self.device_ids = {
            name: response.cookies[name] for name in ("gmid", "ucid") if name in response.cookies
        }

    def start_two_factor(self, reg_token: str) -> PendingTwoFactor:
        """Bestaetigung beginnen und den Code an die (erste) hinterlegte
        E-Mail-Adresse schicken lassen."""
        self._ensure_device_ids()
        key = self.locale.gigya_key
        init = self._gigya(
            "GET",
            "accounts.tfa.initTFA",
            {"APIKey": key, "regToken": reg_token, "provider": "gigyaEmail", "mode": "verify",
             **self.device_ids},
        )
        assertion = init.get("gigyaAssertion")
        if not assertion:
            raise RenaultError("Renault: Bestätigung konnte nicht gestartet werden.")
        emails = self._gigya(
            "GET", "accounts.tfa.email.getEmails", {"APIKey": key, "gigyaAssertion": assertion}
        ).get("emails") or []
        emails = [e for e in emails if isinstance(e, dict) and e.get("id")]
        if not emails:
            raise RenaultError("Im Renault-Konto ist keine E-Mail-Adresse für die Bestätigung hinterlegt.")
        sent = self._gigya(
            "GET",
            "accounts.tfa.email.sendVerificationCode",
            {"APIKey": key, "gigyaAssertion": assertion, "emailID": emails[0]["id"],
             "lang": self.locale.language},
        )
        phv = sent.get("phvToken")
        if not phv:
            raise RenaultError("Renault hat keinen Bestätigungscode verschickt.")
        return PendingTwoFactor(reg_token, assertion, phv, emails[0].get("obfuscated"))

    def complete_two_factor(self, pending: PendingTwoFactor, code: str) -> None:
        """Code einreichen, Geraet merken lassen, neu anmelden."""
        key = self.locale.gigya_key
        verified = self._gigya(
            "GET",
            "accounts.tfa.email.completeVerification",
            {"APIKey": key, "gigyaAssertion": pending.gigya_assertion,
             "phvToken": pending.phv_token, "code": code.strip()},
        )
        provider_assertion = verified.get("providerAssertion")
        if not provider_assertion:
            raise RenaultError("Renault hat den Code ohne Bestätigung beantwortet.")
        self._gigya(
            "GET",
            "accounts.tfa.finalizeTFA",
            {"APIKey": key, "gigyaAssertion": pending.gigya_assertion,
             "providerAssertion": provider_assertion, "regToken": pending.reg_token,
             # tempDevice=false: Geraet merken (rund 30 Tage keine neue Abfrage)
             "tempDevice": "false"},
        )
        self.login()

    def _get_jwt(self) -> str:
        if self._jwt and time.monotonic() < self._jwt_until:
            return self._jwt
        if not self.login_token:
            self.login()
        try:
            payload = self._jwt_request()
        except RenaultAuthError as exc:
            if isinstance(exc, RenaultTwoFactorRequired):
                raise
            # Login-Token abgelaufen: einmal mit dem Passwort neu anmelden.
            self.login()
            payload = self._jwt_request()
        token = payload.get("id_token")
        if not token:
            raise RenaultError("Renault: kein Zugriffstoken in der Antwort.")
        self._jwt = token
        self._jwt_until = time.monotonic() + JWT_LIFETIME_SECONDS - JWT_REFRESH_MARGIN_SECONDS
        return token

    def _jwt_request(self) -> dict:
        return self._gigya(
            "POST",
            "accounts.getJWT",
            {"ApiKey": self.locale.gigya_key, "login_token": self.login_token,
             "fields": "data.personId,data.gigyaDataCenter", "expiration": JWT_LIFETIME_SECONDS},
        )

    # -- Kamereon --------------------------------------------------------------

    def _kamereon(self, path: str, *, retry: bool = True) -> dict:
        url = f"{self.locale.kamereon_url}/commerce/v1{path}"
        headers = {
            "Content-type": "application/vnd.api+json",
            "apikey": KAMEREON_APIKEY,
            "x-gigya-id_token": self._get_jwt(),
        }
        try:
            response = self._http.get(url, params={"country": self.locale.country}, headers=headers)
        except httpx.HTTPError as exc:
            raise RenaultError(f"Renault-Server nicht erreichbar: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        for code, message in _kamereon_errors(payload):
            detail = _error_detail(message)
            if code == _KAMEREON_QUOTA:
                raise RenaultRateLimitError("Renault meldet: Kontingent erschöpft.")
            if code == _KAMEREON_UNAUTHORIZED:
                if retry:
                    self._jwt = None
                    return self._kamereon(path, retry=False)
                raise RenaultAuthError(f"Renault lehnt den Zugriff ab ({detail}).")
            if code in _KAMEREON_NOT_SUPPORTED:
                raise RenaultNotSupportedError(f"{code}: {detail}")
            raise RenaultError(f"Renault meldet Fehler {code}: {detail}")
        if response.status_code == 429:
            raise RenaultRateLimitError("Renault meldet: zu viele Anfragen.")
        if response.status_code in (401, 403) and retry:
            self._jwt = None
            return self._kamereon(path, retry=False)
        if response.status_code == 404:
            raise RenaultNotSupportedError("HTTP 404")
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise RenaultError(f"Renault-Server antwortete mit HTTP {response.status_code}.")
        return payload

    def get_account_id(self) -> str:
        """Das MYRENAULT-Konto der Person (eine Person hat daneben oft ein
        SFDC-Konto ohne Fahrzeuge)."""
        if self.account_id:
            return self.account_id
        if not self.person_id:
            # Meldet bei Bedarf an und erneuert ein abgelaufenes Login-Token.
            self._get_jwt()
            info = self._gigya(
                "POST",
                "accounts.getAccountInfo",
                {"ApiKey": self.locale.gigya_key, "login_token": self.login_token},
            )
            self.person_id = (info.get("data") or {}).get("personId")
            if not self.person_id:
                raise RenaultError("Renault: keine Personenkennung im Konto.")
        person = self._kamereon(f"/persons/{self.person_id}")
        accounts = [a for a in person.get("accounts") or [] if isinstance(a, dict) and a.get("accountId")]
        if not accounts:
            raise RenaultError("Zu diesem Renault-Konto gibt es kein Fahrzeugkonto.")
        preferred = [a for a in accounts if str(a.get("accountType")).upper() == "MYRENAULT"]
        self.account_id = str((preferred or accounts)[0]["accountId"])
        return self.account_id

    def list_vehicles(self) -> list[dict]:
        payload = self._kamereon(f"/accounts/{self.get_account_id()}/vehicles")
        result = []
        for link in payload.get("vehicleLinks") or []:
            if not isinstance(link, dict) or not link.get("vin"):
                continue
            details = link.get("vehicleDetails") or {}
            result.append({
                "vin": str(link["vin"]),
                "brand": ((details.get("brand") or {}).get("label") or "").strip() or None,
                "model": ((details.get("model") or {}).get("label") or "").strip() or None,
                "model_code": (details.get("model") or {}).get("code"),
            })
        return result

    def _car_data(self, vin: str, endpoint: str, version: int) -> dict:
        payload = self._kamereon(
            f"/accounts/{self.get_account_id()}/kamereon/kca/car-adapter/v{version}/cars/{vin}/{endpoint}"
        )
        attributes = (payload.get("data") or {}).get("attributes")
        if not isinstance(attributes, dict):
            raise RenaultError(f"Renault: Antwort auf {endpoint} ohne Daten.")
        return attributes

    def battery_status(self, vin: str) -> dict:
        return self._car_data(vin, "battery-status", 2)

    def cockpit(self, vin: str) -> dict:
        return self._car_data(vin, "cockpit", 1)

    def location(self, vin: str) -> dict:
        return self._car_data(vin, "location", 1)

    def hvac_status(self, vin: str) -> dict:
        return self._car_data(vin, "hvac-status", 1)
