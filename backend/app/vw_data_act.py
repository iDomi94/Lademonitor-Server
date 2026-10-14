"""Client fuer das Datenportal des VW-Konzerns nach EU Data Act.

Portal: https://eu-data-act.drivesomethinggreater.com - dort kann jeder Halter
eines Fahrzeugs von Volkswagen, VW Nutzfahrzeuge, Audi, Škoda, SEAT, Cupra oder
Bentley eine fortlaufende Datenanfrage anlegen ("All Data", alle 15 Minuten).
Das Portal stellt die Daten danach als rollierende Liste von ZIP-Dateien bereit,
in jeder steckt eine JSON-Datei mit den Feldern des Fahrzeugs.

Vierte Quelle fuer automatisch erfasste Ladevorgaenge neben Home Assistant,
MyŠkoda und BMW CarData. Wie bei MyŠkoda gibt es nur ZUSTAENDE, keine fertigen
Ladevorgaenge - die Zustandsmaschine dafuer liegt in `charge_detection.py`,
dieser Client liefert ihr je ZIP einen Bericht (`DataActSnapshot`).

## Herkunft des Codes

Anmeldung und Abruf sind uebernommen aus
`carconnectivity-connector-vw-eu-data-act` (Client `client.py`, Auswertung
`dataset.py`, Marken `brands.py`), das wiederum `api.py` aus
`HA_VAG-EU-Data-Act` (https://github.com/TommiG1/HA_VAG-EU-Data-Act) portiert.
Beide stehen unter der MIT-Lizenz:

    Copyright (c) 2025-2026 TommiG1 and the CarConnectivity contributors
    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions: The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Umgebaut von `requests` auf das synchrone `httpx` wie der Rest der App, und
ohne die automatische Zustimmung zu geaenderten Nutzungsbedingungen (die
bleibt Sache des Nutzers im Browser).

## Was beim Nachbau des Logins zu wissen ist

Es gibt KEINE offizielle Entwickler-Schnittstelle. Der Login ist das
nachgebaute Verhalten eines Browsers gegen `identity.vwgroup.io` (OIDC,
E-Mail und Passwort, Formularfelder `hmac`/`_csrf`/`relayState` teils aus dem
HTML, teils aus einem JS-Objekt `templateModel`). Aendert VW die Seite, bricht
er - die Fehlermeldung sagt das dann ausdruecklich. VW hat die
Community-Clients im Oktober 2026 gebeten, sich mit einem Produkt-User-Agent
auszuweisen, keinen Abruf der Portal-Startseite vorzuschalten und nach dem
Callback (`/services/callbacklogin`) keine weitere Seite zu laden - daran
haelt sich dieser Client.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import httpx

from .changelog import VERSION

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Portal / OIDC
# --------------------------------------------------------------------------

BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
PORTAL_HOST = urlparse(BASE_URL).netloc
IDENTITY_HOST = urlparse(IDENTITY_BASE).netloc

CALLBACK_LOGIN_PATH = "/services/callbacklogin"
MARKETING_CALLBACK_PATHS = (
    "/oidc/v1/oauth/client/callback",
    "/oidc/v1/oauth/client/callback/success",
)
LOGIN_REDIRECT_STATUSES = frozenset({301, 302, 303})
MAX_LOGIN_REDIRECTS = 10

OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"

VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

#: ZIPs mit diesem Suffix enthalten nichts (Fahrzeug hat im Fenster nicht
#: gesendet) und werden gar nicht erst geladen.
NO_CONTENT_SUFFIX = "_no_content_found.zip"

USER_AGENT = f"lademonitor-server/{VERSION} (+https://github.com/iDomi94/Lademonitor-Server)"

REQUEST_TIMEOUT = 60.0

#: Wird in Tests durch einen nachgebauten Dienst ersetzt.
TRANSPORT: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class Brand:
    key: str
    client_id: str
    label: str


_VW_CLIENT = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
_SEAT_CLIENT = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com"

#: Je Marke eine eigene OIDC-client_id; der Schluessel steht ausserdem im
#: `state` (`land__sprache__MARKE`). Cupra laeuft ueber den SEAT-Client.
#: Porsche nutzt ein eigenes Portal und fehlt deshalb.
BRANDS: dict[str, Brand] = {
    "VOLKSWAGEN_PASSENGER_CARS": Brand("VOLKSWAGEN_PASSENGER_CARS", _VW_CLIENT, "Volkswagen"),
    "VOLKSWAGEN_COMMERCIAL_VEHICLES": Brand(
        "VOLKSWAGEN_COMMERCIAL_VEHICLES", _VW_CLIENT, "VW Nutzfahrzeuge"
    ),
    "AUDI": Brand("AUDI", "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com", "Audi"),
    "SKODA": Brand("SKODA", "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com", "Škoda"),
    "SEAT": Brand("SEAT", _SEAT_CLIENT, "SEAT"),
    "CUPRA": Brand("CUPRA", _SEAT_CLIENT, "Cupra"),
    "BENTLEY": Brand("BENTLEY", "d38aac0f-3d89-4a63-8538-b75b31322c7b@apps_vw-dilab_com", "Bentley"),
}


# --------------------------------------------------------------------------
# Fehler
# --------------------------------------------------------------------------

class DataActError(Exception):
    """Basisfehler fuer jede fehlgeschlagene Interaktion mit dem Portal."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DataActAuthError(DataActError):
    """Anmeldung abgelehnt (Passwort, offene Zustimmung, geaenderter Login)."""


class DataActRateLimitError(DataActError):
    """HTTP 429 - das Portal will eine Pause."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


class DataActNotFoundError(DataActError):
    """HTTP 404 - z.B. noch keine Daten oder Datenanfrage abgelaufen."""


# --------------------------------------------------------------------------
# Formular- und Seitenauswertung des Logins
# --------------------------------------------------------------------------

class _FormParser(HTMLParser):
    """Erstes <form>: action und alle input-Felder."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._done = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._done:
            return
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


def _extract_template_model(html: str) -> dict:
    """Das JS-Objekt `templateModel` der Anmeldeseiten (hmac, relayState, ...)."""
    idx = html.find("templateModel")
    if idx == -1:
        return {}
    brace = html.find("{", idx)
    if brace == -1:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except ValueError:
                    return {}
    return {}


def _extract_csrf(html: str) -> str | None:
    m = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else None


def _login_fields(html: str) -> tuple[dict[str, str], str | None]:
    """Felder fuer einen Login-Schritt: HTML-Inputs plus templateModel/csrf."""
    form = _FormParser()
    form.feed(html)
    fields = dict(form.fields)
    model = _extract_template_model(html)
    for key in ("hmac", "relayState"):
        if model.get(key):
            fields[key] = model[key]
    email = (model.get("emailPasswordForm") or {}).get("email")
    if email:
        fields.setdefault("email", email)
    csrf = _extract_csrf(html)
    if csrf:
        fields.setdefault("_csrf", csrf)
    return fields, form.action


def _login_error(html: str) -> str | None:
    model = _extract_template_model(html)
    err = model.get("error") or model.get("errorCode")
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or str(err)
    return str(err) if err else None


def _is_portal_callback(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == PORTAL_HOST and parsed.path.startswith(CALLBACK_LOGIN_PATH)


def _marketing_consent_callback(url: str) -> str | None:
    """Callback, der eine optionale Marketing-Zustimmung UEBERSPRINGT.

    Es wird nichts zugestimmt - der `callback`-Parameter der Seite setzt den
    Login ohne Antwort fort (so macht es auch VWs eigener Login fuer evcc).
    Nur ein https-Callback auf dem Identity-Host mit bekanntem Pfad zaehlt.
    """
    parsed = urlparse(url)
    if parsed.netloc.lower() != IDENTITY_HOST or "/consent/marketing/" not in parsed.path:
        return None
    callback = (parse_qs(parsed.query).get("callback") or [""])[0]
    target = urlparse(callback)
    if (
        target.scheme != "https"
        or target.netloc.lower() != IDENTITY_HOST
        or target.path not in MARKETING_CALLBACK_PATHS
    ):
        return None
    return target._replace(query=urlencode(parse_qsl(target.query, keep_blank_values=True))).geturl()


def _accept_language(country: str, language: str) -> str:
    """Wie ein Browser - ohne den Header leitet der Identity-Dienst gern auf
    Zustimmungsseiten fuer Dokumentvarianten, die nie jemand angeboten bekam."""
    lang = language.lower()
    parts = [f"{lang}-{country.upper()}", f"{lang};q=0.9"]
    if lang != "en":
        parts.append("en;q=0.8")
    return ",".join(parts)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return int(raw)
    if raw:
        try:
            delta = parsedate_to_datetime(raw) - datetime.now(tz=timezone.utc)
        except (TypeError, ValueError):
            return None
        return max(int(delta.total_seconds()), 0)
    return None


def extract_vins(payload: Any) -> list[dict]:
    """FINs aus der (undokumentierten) Fahrzeugliste - sucht jede 17-stellige
    `vin`, unabhaengig von der Verpackung."""
    vins: dict[str, dict] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            vin = node.get("vin") or node.get("vehicleIdentificationNumber")
            if isinstance(vin, str) and len(vin) == 17:
                vins.setdefault(vin, {"vin": vin})
                nick = node.get("vehicleNickname") or node.get("nickname") or node.get("modelName")
                if nick:
                    vins[vin]["nickname"] = nick
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(vins.values())


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class DataActClient:
    """Angemeldeter Zugriff aufs Portal. Haelt das Session-Cookie im Speicher -
    der Poller hebt die Instanz zwischen zwei Abrufen auf, damit nicht jede
    Viertelstunde ein neuer Login noetig ist."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        brand: str,
        country: str = "de",
        language: str = "de",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if brand not in BRANDS:
            raise DataActError(f"Unbekannte Marke: {brand}")
        resolved = BRANDS[brand]
        self._email = email
        self._password = password
        self._client_id = resolved.client_id
        self._state = f"{country.lower()}__{language.lower()}__{resolved.key}"
        self._http = httpx.Client(
            transport=transport or TRANSPORT or httpx.HTTPTransport(retries=2),
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": _accept_language(country, language),
            },
        )
        self.logged_in = False

    def close(self) -> None:
        self._http.close()

    # -- Anmeldung -------------------------------------------------------------

    def login(self) -> None:
        """Kompletter OIDC-Login; danach liegt `access_token` im Cookie-Jar."""
        self.logged_in = False
        self._http.cookies.clear()
        try:
            self._do_login()
        except httpx.HTTPError as exc:
            raise DataActError(f"Netzwerkfehler bei der Anmeldung: {exc}") from exc
        self.logged_in = True

    def _authorize_url(self) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": self._state,
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    def _do_login(self) -> None:
        # 1. Direkt beim Identity-Dienst beginnen (das Portal-Servlet dafuer
        #    antwortet Nicht-Browsern mit 500).
        resp = self._http.get(self._authorize_url(), follow_redirects=True)
        signin_url = str(resp.url)
        fields, action = _login_fields(resp.text)
        if "hmac" not in fields or "_csrf" not in fields:
            raise DataActAuthError(
                "Die Anmeldeseite von VW sah anders aus als erwartet (Formular nicht gefunden) - "
                "vermutlich hat VW den Login geaendert."
            )

        # 2. E-Mail-Schritt.
        fields["email"] = self._email
        resp = self._http.post(
            urljoin(signin_url, action or ""),
            data=fields,
            headers={"Referer": signin_url},
            follow_redirects=True,
        )
        authenticate_url = str(resp.url)
        fields2, action2 = _login_fields(resp.text)
        if "hmac" not in fields2 or "_csrf" not in fields2:
            raise DataActAuthError(
                _login_error(resp.text)
                or "Nach der E-Mail-Adresse kam kein Passwortfeld - E-Mail-Adresse pruefen "
                "(oder VW hat den Login geaendert)."
            )

        # 3. Passwort. An die URL OHNE Query posten - sonst steht relayState
        #    doppelt drin und der Dienst antwortet 400.
        fields2["email"] = self._email
        fields2["password"] = self._password
        target = urljoin(authenticate_url, action2) if action2 else authenticate_url.split("?", 1)[0]
        resp = self._http.post(
            target,
            data=fields2,
            headers={"Referer": authenticate_url},
            follow_redirects=False,
        )
        self._finish_login(self._follow_login_redirects(resp))

    def _follow_login_redirects(self, resp: httpx.Response) -> httpx.Response:
        """Weiterleitungen bis zum Portal-Callback, nach VWs eigener Vorgabe:
        hoechstens 10 Spruenge, nur https auf Identity- oder Portal-Host, Halt
        beim Callback (der setzt das Cookie), Marketing-Frage ueberspringen."""
        for _ in range(MAX_LOGIN_REDIRECTS):
            if _is_portal_callback(str(resp.url)) or resp.status_code not in LOGIN_REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("Location")
            if not location:
                return resp
            target = urljoin(str(resp.url), location)
            parsed = urlparse(target)
            if parsed.scheme != "https" or parsed.netloc.lower() not in (IDENTITY_HOST, PORTAL_HOST):
                raise DataActAuthError(
                    f"Die Anmeldung leitete an eine unerwartete Adresse weiter "
                    f"({parsed.scheme}://{parsed.netloc})."
                )
            callback = _marketing_consent_callback(target)
            if callback:
                logger.info("VW-Login: optionale Marketing-Frage uebersprungen (ohne Zustimmung)")
                target = callback
            resp = self._http.get(target, headers={"Referer": str(resp.url)}, follow_redirects=False)
        raise DataActAuthError(f"Die Anmeldung brauchte mehr als {MAX_LOGIN_REDIRECTS} Weiterleitungen.")

    def _finish_login(self, resp: httpx.Response) -> None:
        landing = str(resp.url)
        if _is_portal_callback(landing):
            if resp.status_code >= 400:
                raise DataActAuthError(f"Das Portal hat die Anmeldung abgelehnt (HTTP {resp.status_code}).")
            if "access_token" not in resp.cookies and "access_token" not in self._http.cookies:
                raise DataActAuthError(
                    "Das Portal hat nach der Anmeldung keine Sitzung eroeffnet "
                    "(Cookie access_token fehlt)."
                )
            return

        if resp.status_code >= 400:
            raise DataActAuthError(
                _login_error(resp.text) or f"Anmeldung abgelehnt (HTTP {resp.status_code})."
            )

        if "terms-and-conditions" in landing:
            # Bewusst KEINE automatische Zustimmung: das ist die Entscheidung
            # des Kontoinhabers.
            raise DataActAuthError(
                "VW verlangt die Zustimmung zu geaenderten Nutzungsbedingungen. Einmal im Browser "
                f"unter {BASE_URL} anmelden und zustimmen, danach klappt es hier wieder."
            )

        segments = urlparse(landing).path.split("/")
        if "signin-service" in segments and "consent" in segments:
            i = segments.index("consent") + 1
            kind = segments[i] if i < len(segments) else ""
            raise DataActAuthError(
                "VW stellt diesem Konto eine Zustimmungsfrage"
                + (f" ({kind})" if kind else "")
                + f". Einmal im Browser unter {BASE_URL} anmelden und sie beantworten."
            )

        if "signin-service" in landing or "/error" in landing:
            raise DataActAuthError("Anmeldung fehlgeschlagen - E-Mail-Adresse und Passwort pruefen.")
        ended = urlparse(landing)
        raise DataActAuthError(
            f"Die Anmeldung endete an unerwarteter Stelle ({ended.scheme}://{ended.netloc}{ended.path}) "
            "- vermutlich hat VW den Login geaendert."
        )

    def ensure_login(self) -> None:
        if not self.logged_in:
            self.login()

    # -- Abrufe ------------------------------------------------------------------

    def _get(self, path: str, *, headers: dict | None = None, retry: bool = True) -> httpx.Response:
        self.ensure_login()
        url = f"{BASE_URL}{path}"
        try:
            resp = self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise DataActError(f"Portal nicht erreichbar: {exc}") from exc
        if resp.status_code in (401, 403) and retry:
            # Sitzung abgelaufen - genau einmal neu anmelden.
            self.logged_in = False
            return self._get(path, headers=headers, retry=False)
        if resp.status_code in (401, 403):
            raise DataActAuthError(
                f"Das Portal verweigert den Zugriff (HTTP {resp.status_code}) trotz frischer Anmeldung.",
                status=resp.status_code,
            )
        if resp.status_code == 429:
            raise DataActRateLimitError(
                "Das Portal meldet zu viele Anfragen (HTTP 429).",
                retry_after=_retry_after_seconds(resp),
            )
        if resp.status_code == 404:
            raise DataActNotFoundError(f"{path.split('?')[0]} -> HTTP 404", status=404)
        if resp.status_code >= 400:
            raise DataActError(f"Portal antwortet mit HTTP {resp.status_code}.", status=resp.status_code)
        return resp

    def _get_json(self, path: str, *, headers: dict | None = None) -> Any:
        resp = self._get(path, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise DataActError(f"Portal lieferte kein JSON: {exc}") from exc

    def list_vehicles(self) -> list[dict]:
        """[{vin, nickname?}] aller im Portal freigegebenen Fahrzeuge."""
        return extract_vins(self._get_json(f"{VEHICLES_PATH}?viewPosition=FRONT_LEFT"))

    def get_identifier(self, vin: str) -> str:
        """Kennung der fortlaufenden Datenanfrage dieses Fahrzeugs."""
        try:
            data = self._get_json(METADATA_PATH.format(vin=vin))
        except DataActNotFoundError as exc:
            raise DataActNotFoundError(
                "Fuer dieses Fahrzeug gibt es im Portal keine fortlaufende Datenanfrage. "
                f"Unter {BASE_URL} eine Anfrage \"All Data\" anlegen.",
                status=404,
            ) from exc
        if isinstance(data, list) and data:
            data = data[0]
        identifier = (data or {}).get("Identifier") or (data or {}).get("identifier")
        if not identifier:
            raise DataActError("Das Portal lieferte keine Kennung der Datenanfrage.")
        return str(identifier)

    def list_datasets(self, vin: str, identifier: str) -> list[dict]:
        """Rollierende Liste [{name, createdOn, size}]; leer, solange es keine gibt."""
        try:
            data = self._get_json(
                LIST_PATH.format(vin=vin, identifier=identifier), headers={"type": "partial"}
            )
        except DataActNotFoundError:
            return []
        files = data if isinstance(data, list) else (data or {}).get("files", [])
        return [f for f in files if isinstance(f, dict) and f.get("name")]

    def download_dataset(self, vin: str, identifier: str, name: str) -> dict:
        """Laedt ein ZIP und gibt das JSON darin zurueck."""
        resp = self._get(
            DOWNLOAD_PATH.format(vin=vin, identifier=identifier),
            headers={"filename": name, "type": "partial"},
        )
        return unzip_json(resp.content, name)


def unzip_json(raw: bytes, name: str) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not members:
                raise DataActError(f"Keine JSON-Datei in {name}.")
            with zf.open(members[0]) as fh:
                return json.loads(fh.read().decode("utf-8"))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise DataActError(f"{name} liess sich nicht lesen: {exc}") from exc


# --------------------------------------------------------------------------
# Auswertung eines Datensatzes
# --------------------------------------------------------------------------

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")

#: Werte, mit denen das Portal "kein Messwert" meint (uint16/int32/uint32-Max).
SENTINELS = frozenset({2**16 - 1, 2**31 - 1, 2**32 - 1})


def parse_value(raw: Any) -> Any:
    """Rohtext -> bool/int/float/str. Enums und Zeitstempel bleiben Text."""
    if raw is None:
        return None
    if isinstance(raw, (bool, int, float)):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


#: Reihenfolge der Enum-Werte (Protobuf-Index) - das Portal liefert ab und zu
#: den Index statt des Namens.
ENUM_MEMBERS: dict[str, tuple[str, ...]] = {
    "charging_state_report.current_charge_state": (
        "CHARGE_STATE_NOT_READY_FOR_CHARGING",
        "CHARGE_STATE_READY_FOR_CHARGING",
        "CHARGE_STATE_CHARGING_HV_BATTERY",
        "CHARGE_STATE_DISCHARGING",
        "CHARGE_STATE_CHARGE_PURPOSE_REACHED_AND_NOT_CONSERVATION_CHARGING",
        "CHARGE_STATE_CHARGE_PURPOSE_REACHED_AND_CONSERVATION",
        "CHARGE_STATE_CONSERVATION_CHARGING",
        "CHARGE_STATE_CHARGING_ERROR",
    ),
    "charging_state_report.charge_type": (
        "CHARGE_TYPE_INVALID",
        "CHARGE_TYPE_OFF",
        "CHARGE_TYPE_AC",
        "CHARGE_TYPE_DC",
    ),
    "plug_states.[*].plug_connection_state": (
        "PLUG_CONNECTION_STATE_INVALID",
        "PLUG_CONNECTION_STATE_CONNECTED",
        "PLUG_CONNECTION_STATE_DISCONNECTED",
    ),
}

_PLUG_FIELD_RE = re.compile(r"^plug_states\.(\[\*\]|\d+)\.plug_connection_state$")


def _enum_key(field_name: str) -> str:
    return "plug_states.[*].plug_connection_state" if _PLUG_FIELD_RE.match(field_name) else field_name


def resolve_enum_index(field_name: str, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    members = ENUM_MEMBERS.get(_enum_key(field_name))
    if members is not None and 0 <= value < len(members):
        return members[value]
    return value


def parse_timestamp(raw: Any) -> datetime | None:
    """Zeitstempel des Portals -> naives UTC (wie ueberall in der DB)."""
    s = str(raw or "").strip()
    if not s:
        return None
    parsed: datetime | None
    if _INT_RE.match(s) and len(s) >= 12:  # Epoch-Millisekunden
        try:
            parsed = datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    else:
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


#: Kopien von Berichtsfeldern, die zu einem Ladeauftrag gehoeren (laut
#: Datenkatalog v1.0.0). Sie koennen dem allgemeinen Wert widersprechen und
#: springen deshalb nur ein, wenn es keinen anderen gibt - beim SoC nie, dort
#: bedeuten sie "SoC bei Ladebeginn".
REPORT_COPY_KEYS: dict[str, frozenset[str]] = {
    "battery_state_report.soc": frozenset({
        "93b55324-6628-36df-8f76-8eba797fc59c",
        "7bddd5e7-43a4-3878-bd63-9502782f77a5",
        "bd4b6d50-b574-31e6-8141-8787ca5fec8c",
    }),
    "battery_state_report.charge_power": frozenset({
        "44ed0d61-98c4-36df-b860-b077929a5797",
        "8087d3b7-03bd-350a-95ef-bd8cb3ba20fa",
        "885d1abd-799d-344a-bf02-0d07c6d6eebf",
    }),
    "charging_state_report.current_charge_state": frozenset({
        "96c211b4-f8fb-3f40-b7cf-6a1cd12cce6d",
        "3581f1e0-580b-3f18-aa73-6fbf037b841e",
        "0811aa96-26d8-3d5f-8bd7-77be06612d85",
    }),
    "charging_state_report.charge_type": frozenset({
        "f994c025-a69f-39ae-8860-9cb9a2ea9ed8",
        "b942e990-6a4e-351b-81ce-84e27d13d97a",
        "33bb1490-adbb-3a14-9abf-e68417d46f0b",
    }),
}
REPORT_COPIES_DIFFER = frozenset({"battery_state_report.soc"})


@dataclass
class DataPoint:
    key: str
    field_name: str
    raw_value: Any
    timestamp: datetime | None = None

    @property
    def value(self) -> Any:
        return resolve_enum_index(self.field_name, parse_value(self.raw_value))

    @property
    def is_report_copy(self) -> bool:
        return self.key in REPORT_COPY_KEYS.get(self.field_name, ())


def _freshness(dp: DataPoint) -> tuple:
    """Frischester zuerst; bei Gleichstand der kleinste Schluessel - eine
    willkuerliche, aber STABILE Wahl, damit ein Wert nicht hin- und
    herspringt, wenn das Portal die Liste neu mischt."""
    if dp.timestamp is not None:
        return (0, -dp.timestamp.timestamp(), dp.key)
    return (1, 0.0, dp.key)


@dataclass
class Dataset:
    """Ein JSON aus einem ZIP: `{"vin", "Data": [{key, dataFieldName, value,
    timestampUtc?}]}`. Das Portal legt mehrere Berichte ohne feste Reihenfolge
    in eine Liste; ein Feld kann also mehrfach mit verschiedenen Werten
    vorkommen."""

    points: list[DataPoint] = field(default_factory=list)
    captured_at: datetime | None = None

    @classmethod
    def from_json(cls, payload: dict) -> "Dataset":
        points: list[DataPoint] = []
        captured: list[datetime] = []
        for item in (payload or {}).get("Data", []) or []:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not key:
                continue
            name = item.get("dataFieldName") or key
            dp = DataPoint(
                key=str(key),
                field_name=str(name),
                raw_value=item.get("value"),
                timestamp=parse_timestamp(item.get("timestampUtc")),
            )
            points.append(dp)
            if dp.field_name.endswith("car_captured_time"):
                ts = parse_timestamp(dp.raw_value)
                if ts:
                    captured.append(ts)
        if not captured:
            # Flaches Format (aeltere Plattformen) kennt kein car_captured_time,
            # dafuer timestampUtc je Feld.
            captured = [dp.timestamp for dp in points if dp.timestamp is not None]
        return cls(points=points, captured_at=max(captured) if captured else None)

    def matching(self, predicate) -> list[DataPoint]:
        return [dp for dp in self.points if predicate(dp.field_name)]

    def by_field(self, field_name: str) -> DataPoint | None:
        matches = [dp for dp in self.points if dp.field_name == field_name]
        if field_name in REPORT_COPY_KEYS:
            live = [dp for dp in matches if not dp.is_report_copy]
            if live or field_name in REPORT_COPIES_DIFFER:
                matches = live
        return min(matches, key=_freshness) if matches else None

    def value_of(self, field_name: str) -> Any:
        dp = self.by_field(field_name)
        return dp.value if dp is not None else None

    def freshest_max(self, field_name: str) -> Any:
        """Wie `value_of`, bei gleich frischen Werten aber der groesste - fuer
        den Kilometerstand, der sonst durch einen nachhinkenden Bericht kurz
        zu niedrig stehen kann."""
        matches = [dp for dp in self.points if dp.field_name == field_name]
        if not matches:
            return None
        rank = lambda dp: (0, -dp.timestamp.timestamp()) if dp.timestamp else (1, 0.0)  # noqa: E731
        best = min(rank(dp) for dp in matches)
        freshest = [dp for dp in matches if rank(dp) == best]
        numeric = [dp.value for dp in freshest if _is_number(dp.value)]
        if numeric:
            return max(numeric)
        return min(freshest, key=_freshness).value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: Any) -> float | None:
    if not _is_number(value) or value in SENTINELS:
        return None
    return float(value)


#: Ladezustaende (ohne Praefix), bei denen das Kabel sicher steckt - nur
#: gebraucht, wenn der Bericht keinen Steckerzustand enthaelt.
PLUGGED_STATES = frozenset({
    "READY_FOR_CHARGING",
    "CHARGING_HV_BATTERY",
    "CHARGE_PURPOSE_REACHED_AND_NOT_CONSERVATION_CHARGING",
    "CHARGE_PURPOSE_REACHED_AND_CONSERVATION",
    "CONSERVATION_CHARGING",
    # flaches Format
    "CHARGING",
    "CONSERVATIONCHARGING",
})
UNPLUGGED_STATES = frozenset({"NOT_READY_FOR_CHARGING", "OFF"})
DISCHARGING_STATES = frozenset({"DISCHARGING"})

#: Plausibler Bereich der Aussentemperatur - ausserhalb ist es ein
#: Platzhalter oder eine falsch verstandene Einheit.
PLAUSIBLE_TEMP_C = (-50.0, 60.0)


@dataclass
class DataActSnapshot:
    """Ein Datensatz, uebersetzt in die Eigenschaften, die
    `charge_detection.apply_snapshot` erwartet (dieselben wie
    `myskoda.VehicleSnapshot`)."""

    dataset: Dataset
    raw: dict = field(default_factory=dict)
    name: str | None = None
    created_on: datetime | None = None
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_json(
        cls, payload: dict, *, name: str | None = None, created_on: datetime | None = None
    ) -> "DataActSnapshot":
        return cls(Dataset.from_json(payload), raw=payload, name=name, created_on=created_on)

    # Das Portal liefert keine Position.
    coordinates = None
    is_in_saved_location = None

    @property
    def charge_state(self) -> str | None:
        """Ladezustand ohne Praefix, z.B. CHARGING_HV_BATTERY (bzw. CHARGING im
        flachen Format)."""
        value = self.dataset.value_of("charging_state_report.current_charge_state")
        if isinstance(value, str) and value:
            return value.upper().removeprefix("CHARGE_STATE_")
        flat = self.dataset.value_of("charging_state")
        if isinstance(flat, str) and flat.strip().lower() not in ("", "invalid", "unsupported"):
            return flat.strip().upper()
        return None

    @property
    def plug_connected(self) -> bool | None:
        """Steckerzustand aus irgendeinem der Steckerfelder; bei mehreren
        Steckern genuegt einer."""
        states: list[bool] = []
        for dp in self.dataset.matching(
            lambda n: bool(_PLUG_FIELD_RE.match(n))
            or n in ("plug_connection_state", "charging_plug1_connectionstate")
        ):
            text = str(dp.value or "").strip().upper()
            if text.endswith("DISCONNECTED"):
                states.append(False)
            elif text.endswith("CONNECTED"):
                states.append(True)
        if not states:
            return None
        return any(states)

    @property
    def is_plugged_in(self) -> bool | None:
        plug = self.plug_connected
        if plug is not None:
            return plug
        state = self.charge_state
        if state in PLUGGED_STATES:
            return True
        if state in UNPLUGGED_STATES or state in DISCHARGING_STATES:
            return False
        return None

    @property
    def charging_state(self) -> str | None:
        """Was die Zustandsmaschine als Zustand sieht. None, wenn sich nicht
        sagen laesst, ob das Kabel steckt - dann wird nichts entschieden."""
        if self.is_plugged_in is None:
            return None
        state = self.charge_state
        if state:
            return state
        return "PLUG_CONNECTED" if self.is_plugged_in else "PLUG_DISCONNECTED"

    @property
    def soc_percent(self) -> int | None:
        for name in ("battery_state_report.soc", "state_of_charge"):
            value = _number(self.dataset.value_of(name))
            if value is not None and 0 <= value <= 100:
                return int(round(value))
        return None

    @property
    def charge_power_kw(self) -> float | None:
        value = _number(self.dataset.value_of("battery_state_report.charge_power"))
        if value is None:
            raw = _number(self.dataset.value_of("charging_power"))
            value = raw / 10 if raw is not None else None  # flach: 0,1 kW
        if value is None or value < 0 or value > 500:
            return None
        return round(value, 1)

    @property
    def charge_type(self) -> str | None:
        value = self.dataset.value_of("charging_state_report.charge_type")
        if isinstance(value, str) and value:
            return value.upper().removeprefix("CHARGE_TYPE_")
        return None

    @property
    def odometer_km(self) -> int | None:
        value = _number(self.dataset.freshest_max("mileage.value"))
        if value is not None:
            unit = self.dataset.value_of("mileage.unit")
            if unit in (1, "1") or (isinstance(unit, str) and unit.upper().startswith("MILE")):
                value *= 1.609344
        else:
            value = _number(self.dataset.freshest_max("mileage"))
        if value is None or value < 0:
            return None
        return int(round(value))

    @property
    def outside_temp_c(self) -> float | None:
        candidates: list[float | None] = []
        dk = _number(self.dataset.value_of("outside_temperature"))
        candidates.append(dk / 10 - 273.15 if dk is not None else None)
        for name in (
            "outdoor_temperature.outdoor_temperature",
            "outdoor_temperature",
            "outsideTemperatureIndication",
        ):
            candidates.append(_number(self.dataset.value_of(name)))
        # 0 = -46 °C in 0,1-K-Schritten bis 1460 = 100 °C (laut Datenkatalog).
        raw = _number(self.dataset.value_of("outdoor_temperature.temperature"))
        candidates.append(raw / 10 - 46 if raw is not None and 0 <= raw <= 1460 else None)
        for value in candidates:
            if value is not None and PLAUSIBLE_TEMP_C[0] <= value <= PLAUSIBLE_TEMP_C[1]:
                return round(value, 1)
        return None

    @property
    def charged_energy_kwh(self) -> float | None:
        """Energiezaehler des Fahrzeugs ("0 bis 1000 kWh"). Ob er je Vorgang
        oder insgesamt zaehlt, ist nicht dokumentiert - siehe
        charge_detection.py, "Gemessene Energie"."""
        value = _number(self.dataset.value_of("battery_state_report.charge_energy"))
        if value is None:
            raw = _number(self.dataset.value_of("charged_energy"))
            value = raw / 10 if raw is not None else None  # flach: 0,1 kWh
        if value is None or value < 0 or value > 100000:
            return None
        return round(value, 3)

    @property
    def captured_at(self) -> datetime:
        return self.dataset.captured_at or self.created_on or self.fetched_at

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.name,
            "charging_state": self.charging_state,
            "plug_connected": self.plug_connected,
            "soc_percent": self.soc_percent,
            "charge_power_kw": self.charge_power_kw,
            "charge_type": self.charge_type,
            "charged_energy_kwh": self.charged_energy_kwh,
            "odometer_km": self.odometer_km,
            "outside_temp_c": self.outside_temp_c,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "fields": len(self.dataset.points),
        }
