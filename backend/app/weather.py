"""Aussentemperatur zu einem Ort und Zeitpunkt von einem Wetterdienst holen.

Die Verbrauchsauswertung nach Aussentemperatur (`temperature.py`) lebt davon,
dass ueberhaupt Werte da sind. Bisher kamen sie nur aus dem Fahrzeug (Home
Assistant, MyŠkoda-Poller) oder von Hand - Bestandsdaten haben naturgemaess
keine, und wer weder HA noch einen Fahrzeugsensor hat, bekommt nie welche.
Dieses Modul schliesst die Luecke, indem es die Temperatur nachtraeglich am
Ladeort nachschlaegt.

**Das ist die dritte Ausnahme von der "kein Cloud-Dienst"-Linie des Projekts -
und die erste, die der SERVER von sich aus macht.** Die Adresssuche
(`geocode.py`) laeuft nur auf Nutzeraktion, die Kartenkacheln holt der
Browser. Hier geht eine Koordinate an einen fremden Server, und zwar
moeglicherweise die des eigenen Zuhauses. Deshalb:

* **Opt-in pro Nutzer, Standard aus** (`User.weather_autofill_enabled`) -
  nichts passiert, solange niemand den Schalter umlegt.
* **Koordinaten werden auf `COORD_PRECISION` Nachkommastellen gerundet**
  (2 -> rund 1,1 km). Das ist kein Kompromiss zulasten der Genauigkeit: ERA5
  rastert ohnehin in 9-25 km, feinere Angaben landen im selben Gitterpunkt.
  Uebrig bleibt am fremden Server ein Ortsteil, keine Hausnummer.
* **Kein Zeitstempel in der Anfrage, der ueber den Tag hinausgeht.** Abgefragt
  wird ein Datumsbereich; welche Stunde daraus gebraucht wird, entscheidet sich
  hier.

Standardanbieter ist Open-Meteo (kein API-Schluessel noetig, CC-BY-4.0-Daten).
`User.weather_api_url` kann auf eine eigene Instanz zeigen - Open-Meteo ist
quelloffen und laesst sich selbst hosten, womit die Linie oben wieder
unangetastet waere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal

logger = logging.getLogger(__name__)

# Nachkommastellen, auf die jede Koordinate vor dem Verlassen des Servers
# gerundet wird. 2 entspricht rund 1,1 km - siehe Modul-Docstring.
COORD_PRECISION = 2

# Zwei Endpunkte, weil kein einzelner beides kann:
#   - Der Vorhersage-Endpunkt liefert die juengste Vergangenheit ohne Verzug,
#     aber nur bis `FORECAST_MAX_PAST_DAYS` zurueck.
#   - Das Archiv (ERA5) reicht bis 1940 zurueck, hinkt dafuer rund fuenf Tage
#     hinterher.
# Die Grenze `ARCHIVE_SWITCH_DAYS` liegt bewusst innerhalb BEIDER Fenster, damit
# kein Vorgang zwischen die Endpunkte faellt.
DEFAULT_API_URL = "https://api.open-meteo.com"
PUBLIC_ARCHIVE_URL = "https://archive-api.open-meteo.com"
FORECAST_MAX_PAST_DAYS = 92
ARCHIVE_SWITCH_DAYS = 30

FORECAST_PATH = "/v1/forecast"
ARCHIVE_PATH = "/v1/archive"

REQUEST_TIMEOUT_SECONDS = 20.0

# Obergrenze an HTTP-Anfragen je Lauf. Das freie Kontingent von Open-Meteo
# liegt bei rund 600 Anfragen/Minute und 10.000/Tag; ein einzelner Nutzer
# kommt hier normalerweise mit einer Handvoll aus (eine je Ladeort). Der
# Deckel schuetzt den Fall, in dem jemand an hunderten verschiedenen Orten
# geladen hat: der Lauf hoert dann auf und meldet, wieviel offen blieb, statt
# das Kontingent leerzulaufen.
MAX_REQUESTS_PER_RUN = 50

# Liegen zwischen zwei Ladetagen am selben Ort mehr als so viele Tage, werden
# sie als getrennte Abfragen geholt statt als ein durchgehender Bereich. Ohne
# das wuerde ein einzelner Vorgang von vor drei Jahren dazu fuehren, dass drei
# Jahre Stundenwerte ueber die Leitung gehen.
#
# Der Wert war zuerst auf 7 Tage gesetzt und das war falsch herum gedacht: wer
# alle acht bis zehn Tage zuhause laedt - der Normalfall - bekam damit fuer
# JEDEN Vorgang eine eigene Anfrage, also genau den Anfragensturm, den die
# Buendelung verhindern soll (im Live-Test sichtbar geworden). Teuer ist hier
# die ANZAHL der Anfragen (Rate-Limit), nicht die Antwortgroesse: ein Jahr
# Stundenwerte sind rund 70 KB. Also grosszuegig buendeln und nur echte
# Ausreisser - ein einzelner Vorgang aus einem lange zurueckliegenden Urlaub -
# getrennt holen.
CLUSTER_GAP_DAYS = 60

# Nur Vorgaenge dieser juengsten Vergangenheit holt die Automatik nach. Aeltere
# sind Sache des Nachtragens per Knopfdruck. Ohne diese Grenze wuerde der
# Scheduler fuer einen Vorgang, zu dem es dauerhaft keinen Wert gibt (Ozean,
# Datenluecke), alle 15 Minuten bis in alle Ewigkeit erneut anfragen.
AUTOFILL_MAX_AGE_DAYS = 14
AUTOFILL_MAX_SESSIONS_PER_RUN = 100


@dataclass(frozen=True)
class TempQuery:
    """Was fuer einen Ladevorgang nachgeschlagen werden soll."""

    session_id: str
    # Naiv und als LOKALE Zeit gemeint - so liegt start_time in der DB und so
    # lesen es beide Apps (siehe APIClient.swift). Die Umrechnung nach UTC
    # passiert genau einmal, in `_to_utc()`.
    when: datetime
    latitude: float
    longitude: float


def round_coord(value: float) -> float:
    return round(value, COORD_PRECISION)


def archive_url_for(base_url: str) -> str:
    """Basis-URL des Archiv-Endpunkts zu einer gegebenen Basis-URL.

    Der oeffentliche Dienst trennt beides auf zwei Hosts
    (`api.` und `archive-api.`), eine selbst gehostete Instanz beantwortet
    beide Pfade unter derselben Adresse. Deshalb wird nur beim bekannten
    oeffentlichen Host umgeschrieben - alles andere bleibt, wie der Nutzer es
    eingetragen hat.
    """
    base = base_url.rstrip("/")
    if base == DEFAULT_API_URL:
        return PUBLIC_ARCHIVE_URL
    return base


def _to_utc(when: datetime) -> datetime:
    """Naive Lokalzeit -> naive UTC.

    Ein um eine Stunde verschobener Zeitstempel holt im Tagesgang schnell den
    falschen Wert (2-3 K) - also genau die Groessenordnung, die die Auswertung
    zu messen versucht. Deshalb wird hier einmal sauber umgerechnet und die
    API anschliessend ausschliesslich in UTC befragt (`timezone=UTC`).
    """
    if when.tzinfo is not None:
        return when.astimezone(timezone.utc).replace(tzinfo=None)
    # astimezone() deutet einen naiven Zeitstempel als lokale Zeit des
    # Systems - dieselbe Annahme, unter der die Werte geschrieben wurden.
    return when.astimezone(timezone.utc).replace(tzinfo=None)


def _cluster_dates(dates: list[date]) -> list[tuple[date, date]]:
    """Sortierte Tage zu zusammenhaengenden Bereichen zusammenfassen."""
    ranges: list[tuple[date, date]] = []
    start = prev = dates[0]
    for current in dates[1:]:
        if (current - prev).days > CLUSTER_GAP_DAYS:
            ranges.append((start, prev))
            start = current
        prev = current
    ranges.append((start, prev))
    return ranges


def _interpolate(series: dict[datetime, float], when: datetime) -> float | None:
    """Wert zu einem Zeitpunkt aus Stundenwerten.

    Zwischen den beiden umgebenden Stunden wird linear interpoliert - auf die
    volle Stunde zu runden traegt sonst eine Treppe in die Streuung, die gar
    nicht im Wetter steckt.
    """
    floor = when.replace(minute=0, second=0, microsecond=0)
    ceil = floor + timedelta(hours=1)
    low = series.get(floor)
    high = series.get(ceil)
    if low is None:
        return high
    if high is None:
        return low
    fraction = (when - floor).total_seconds() / 3600
    return low + (high - low) * fraction


def _parse_hourly(payload: dict) -> dict[datetime, float]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("temperature_2m") or []
    series: dict[datetime, float] = {}
    for raw_time, value in zip(times, values):
        if value is None:
            continue
        try:
            series[datetime.fromisoformat(raw_time)] = float(value)
        except (TypeError, ValueError):
            continue
    return series


def fetch_temperatures(
    queries: list[TempQuery],
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> dict[str, float]:
    """Temperaturen zu mehreren Ladevorgaengen holen.

    Rueckgabe: `session_id -> Grad Celsius`, nur fuer Vorgaenge, zu denen es
    tatsaechlich einen Wert gab. Fehler (Netz, Rate-Limit, unbekannter Ort)
    fuehren NIE zu einer Ausnahme: eine fehlende Temperatur ist ein fehlendes
    Detail, kein Grund, einen Ladevorgang oder einen ganzen Nachtrag scheitern
    zu lassen.

    Gebuendelt wird nach gerundeter Koordinate und zusammenhaengendem
    Zeitraum - typischerweise bleibt damit eine Anfrage je Ladeort uebrig,
    nicht eine je Ladevorgang.
    """
    if not queries:
        return {}

    base = (base_url or DEFAULT_API_URL).rstrip("/")
    archive_base = archive_url_for(base)
    today = today or datetime.utcnow().date()
    archive_before = today - timedelta(days=ARCHIVE_SWITCH_DAYS)

    # (Endpunkt, lat, lon) -> {Tag -> [Anfragen]}
    buckets: dict[tuple[str, float, float], dict[date, list[tuple[TempQuery, datetime]]]] = {}
    for query in queries:
        when_utc = _to_utc(query.when)
        day = when_utc.date()
        # Alles juenger als ARCHIVE_SWITCH_DAYS holt der Vorhersage-Endpunkt
        # (kein Verzug), alles aeltere das Archiv. Die Grenze liegt in beiden
        # Fenstern, es faellt also nichts dazwischen.
        endpoint = "archive" if day < archive_before else "forecast"
        key = (endpoint, round_coord(query.latitude), round_coord(query.longitude))
        buckets.setdefault(key, {}).setdefault(day, []).append((query, when_utc))

    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    result: dict[str, float] = {}
    requests_made = 0
    try:
        for (endpoint, latitude, longitude), by_day in buckets.items():
            url = (base if endpoint == "forecast" else archive_base) + (
                FORECAST_PATH if endpoint == "forecast" else ARCHIVE_PATH
            )
            for start, end in _cluster_dates(sorted(by_day)):
                if requests_made >= MAX_REQUESTS_PER_RUN:
                    logger.warning(
                        "Wetter-Abruf: Obergrenze von %s Anfragen je Lauf erreicht, "
                        "der Rest bleibt offen",
                        MAX_REQUESTS_PER_RUN,
                    )
                    return result
                requests_made += 1
                params = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "hourly": "temperature_2m",
                    "timezone": "UTC",
                    "start_date": start.isoformat(),
                    # Einen Tag Puffer: eine Stunde kurz vor Mitternacht UTC
                    # braucht zum Interpolieren noch den ersten Wert des
                    # Folgetags.
                    "end_date": (end + timedelta(days=1)).isoformat(),
                }
                try:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    series = _parse_hourly(response.json())
                except Exception as exc:  # noqa: BLE001 - siehe Docstring
                    logger.warning("Wetter-Abruf fehlgeschlagen (%s): %s", url, exc)
                    continue
                if not series:
                    continue
                for day in sorted(by_day):
                    if not (start <= day <= end):
                        continue
                    for query, when_utc in by_day[day]:
                        value = _interpolate(series, when_utc)
                        if value is not None:
                            result[query.session_id] = round(value, 1)
    finally:
        if owns_client:
            client.close()
    return result


# ---------------------------------------------------------------------------
# Anwendung auf Ladevorgaenge
# ---------------------------------------------------------------------------


def coordinates_for(session: models.ChargingSession) -> tuple[float, float] | None:
    """Koordinaten eines Ladevorgangs - eigene zuerst, sonst die des Ladeorts.

    Ein importierter Spritmonitor-Vorgang hat selbst nie GPS. Haengt er an
    einem Ladeort, ist dessen Position die beste verfuegbare Auskunft darueber,
    wo das Fahrzeug stand; ohne beides gibt es schlicht nichts nachzuschlagen.
    """
    if session.latitude is not None and session.longitude is not None:
        return (session.latitude, session.longitude)
    location = session.location
    if location is not None and location.latitude is not None and location.longitude is not None:
        return (location.latitude, location.longitude)
    return None


@dataclass
class BackfillReport:
    considered: int = 0
    already_set: int = 0
    without_coordinates: int = 0
    resolved: int = 0
    unresolved: int = 0
    written: int = 0
    preview: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.preview is None:
            self.preview = []


PREVIEW_LIMIT = 50


def backfill_sessions(
    db: Session,
    user: models.User,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> BackfillReport:
    """Aussentemperatur fuer bestehende Ladevorgaenge nachtragen.

    `dry_run=True` (Vorgabe) schreibt garantiert nichts: es wird genauso
    abgefragt, das Ergebnis aber nur zurueckgemeldet. Der Abruf gehoert
    bewusst zum Probelauf dazu - eine Vorschau, die nur zaehlt, wieviele
    Vorgaenge in Frage kaemen, saehe auch dann gut aus, wenn der Dienst gar
    keine Werte liefert.

    `overwrite=False` laesst vorhandene Werte in Ruhe. Ein Wert aus dem
    Fahrzeug oder von Hand ist naeher dran als einer vom Wetterdienst und
    darf nicht stillschweigend ersetzt werden.
    """
    report = BackfillReport()
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.user_id == user.id)
        .order_by(models.ChargingSession.start_time.desc())
        .all()
    )

    queries: list[TempQuery] = []
    by_id: dict[str, models.ChargingSession] = {}
    for session in sessions:
        report.considered += 1
        if session.outside_temp_c is not None and not overwrite:
            report.already_set += 1
            continue
        coords = coordinates_for(session)
        if coords is None:
            report.without_coordinates += 1
            continue
        queries.append(
            TempQuery(
                session_id=session.id,
                when=session.start_time,
                latitude=coords[0],
                longitude=coords[1],
            )
        )
        by_id[session.id] = session

    temperatures = fetch_temperatures(
        queries, base_url=base_url or user.weather_api_url, client=client
    )
    report.resolved = len(temperatures)
    report.unresolved = len(queries) - len(temperatures)

    for session_id, value in temperatures.items():
        session = by_id[session_id]
        if len(report.preview) < PREVIEW_LIMIT:
            report.preview.append(
                {
                    "session_id": session.id,
                    "start_time": session.start_time,
                    "previous_temp_c": session.outside_temp_c,
                    "temp_c": value,
                }
            )
        if dry_run:
            continue
        session.outside_temp_c = value
        session.outside_temp_source = models.TemperatureSource.WEATHER
        report.written += 1

    if not dry_run and report.written:
        db.commit()
    return report


def autofill_new_sessions(db: Session) -> int:
    """Automatik: frische Vorgaenge ohne Temperatur nachziehen.

    Laeuft im Scheduler und bewusst NICHT im Request-Pfad des HA-Pushes: sonst
    haenge die Antwortzeit von `POST /api/sessions/auto` an einem fremden
    Server, und ein langsamer Wetterdienst liesse die Automation des Nutzers
    ins Timeout laufen.
    """
    cutoff = datetime.utcnow() - timedelta(days=AUTOFILL_MAX_AGE_DAYS)
    users = (
        db.query(models.User)
        .filter(models.User.weather_autofill_enabled.is_(True))
        .all()
    )
    filled = 0
    for user in users:
        sessions = (
            db.query(models.ChargingSession)
            .filter(
                models.ChargingSession.user_id == user.id,
                models.ChargingSession.outside_temp_c.is_(None),
                models.ChargingSession.start_time >= cutoff,
            )
            .order_by(models.ChargingSession.start_time.desc())
            .limit(AUTOFILL_MAX_SESSIONS_PER_RUN)
            .all()
        )
        queries: list[TempQuery] = []
        by_id: dict[str, models.ChargingSession] = {}
        for session in sessions:
            coords = coordinates_for(session)
            if coords is None:
                continue
            queries.append(
                TempQuery(
                    session_id=session.id,
                    when=session.start_time,
                    latitude=coords[0],
                    longitude=coords[1],
                )
            )
            by_id[session.id] = session
        if not queries:
            continue
        for session_id, value in fetch_temperatures(
            queries, base_url=user.weather_api_url
        ).items():
            session = by_id[session_id]
            session.outside_temp_c = value
            session.outside_temp_source = models.TemperatureSource.WEATHER
            filled += 1
        db.commit()
    return filled


def run_due_autofill() -> None:
    """Einstiegspunkt fuer den Scheduler (siehe main.py)."""
    db = SessionLocal()
    try:
        autofill_new_sessions(db)
    finally:
        db.close()
