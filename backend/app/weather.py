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
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

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

# Tagstunden, aus denen sich ein Wetterdienstwert zusammensetzt: 6-20 Uhr
# LOKALZEIT. Gemittelt wird NICHT ueber einen Tag, sondern ueber das ganze
# Intervall seit dem vorherigen Ladevorgang - also ueber die Tagstunden jedes
# Tages dazwischen (siehe `_interval_windows()`).
#
# Warum ueberhaupt gemittelt wird: der Verbrauch eines Vorgangs N stammt aus
# der Strecke zwischen N-1 und N, beschreibt also eine FAHRT bzw. alle Fahrten
# dieses Zeitraums (siehe Abschnitt "Verbrauch nach Aussentemperatur" in
# CLAUDE.md, Punkt 1). Hier liegen zwischen zwei Ladevorgaengen leicht zwei
# Wochen und zwanzig Fahrten - ein Messpunkt beim Einstecken ist dafuer nur
# eine Tendenz. Der Wetterdienst KANN ueber den Zeitraum mitteln; ein
# Fahrzeugsensor kann es nicht, deshalb bleiben dessen Werte (HA-Push,
# MyŠkoda-Poller) unberuehrt.
#
# Warum 6-20 und nicht 24 h: nachts wird kaum gefahren, gemessen werden soll
# aber die Fahrt. Das 24-h-Mittel laege rund 1,6 K darunter. Die Grenzen sind
# eine bewusste Vereinfachung - genauere Angaben ueber die Gewohnheiten des
# Fahrers haben wir nicht.
#
# Der alte Punktwert lag am weitesten daneben, wo gar keine Uhrzeit vorliegt:
# Spritmonitor-Importe stehen alle auf 00:00 (das ist das Fehlen einer Angabe,
# nicht die Angabe "kurz nach Mitternacht"), und beim Wort genommen traf man
# damit das TAGESMINIMUM - an echten Stundendaten (Raum Stuttgart, 60 Tage) im
# Mittel 3,9 K unter dem 6-20-Mittel, in der Spitze 8,8 K.
DAILY_WINDOW_START_HOUR = 6
DAILY_WINDOW_END_HOUR = 20

# Obergrenze fuer das gemittelte Intervall. Liegt der vorherige Ladevorgang
# laenger zurueck, zaehlen nur die letzten so vielen Tage davor. Ein groesserer
# Abstand ist in der Praxis keine Fahrt, sondern eine Luecke (Urlaub ohne Auto,
# unvollstaendiger Import) - und ueber Jahre zu mitteln ergaebe einen
# Jahresdurchschnitt, der ueber nichts mehr etwas aussagt.
MAX_INTERVAL_DAYS = 60

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
    # Beginn des VORHERIGEN Ladevorgangs desselben Fahrzeugs, naiv/lokal wie
    # `when`. Er spannt zusammen mit `when` den Zeitraum auf, ueber den
    # gemittelt wird. None (kein Vorgaenger bekannt) faellt auf die Tagstunden
    # des Ladetages selbst zurueck.
    previous: datetime | None = None


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


def _to_local_naive(when: datetime) -> datetime:
    """Zonenbehaftete Angabe (aus einem API-Payload) in lokale Wandzeit holen.

    Aus der Datenbank kommt `start_time` ohnehin naiv und ist als lokale Zeit
    gemeint; ohne diese Umrechnung waere der Ladetag einer zonenbehafteten
    Angabe der ihres Offsets, nicht der, an dem hier geladen wurde.
    """
    if when.tzinfo is not None:
        return when.astimezone().replace(tzinfo=None)
    return when


def _interval_windows(
    when: datetime, previous: datetime | None
) -> list[tuple[datetime, datetime]]:
    """Die Zeitfenster, ueber die ein Wert gemittelt wird - in naiver UTC.

    Fuer jeden Tag zwischen dem vorherigen Ladevorgang und diesem die
    Tagstunden (`DAILY_WINDOW_START_HOUR`..`DAILY_WINDOW_END_HOUR`), an den
    Raendern beschnitten auf den tatsaechlichen Zeitraum:

    * 10.07. 18:00 -> 20.07. 09:00 ergibt den Rest des 10., die vollen
      Tagstunden vom 11. bis 19. und den Anfang des 20.
    * Zwei Ladungen am selben Tag (08:00 und 12:00) ergeben genau ein Fenster
      von 08:00 bis 12:00 - nicht den ganzen Tag, denn dazwischen liegt auch
      nur diese eine Fahrt.
    * Ohne Vorgaenger (erster Vorgang eines Fahrzeugs) bleiben die Tagstunden
      des Ladetages selbst.

    Gerechnet wird durchweg in LOKALER Zeit und erst zum Schluss nach UTC
    geschoben: lokal 00:00 liegt in Mitteleuropa schon im UTC-Vortag, ein
    Umweg ueber die UTC-Datumsangabe haette also den falschen Tag erwischt.
    Liegt gar keine Tagstunde im Zeitraum (beide Ladungen nachts, am selben
    Tag), faellt das Ergebnis auf die Tagstunden des Ladetages zurueck - ein
    grober Wert ist hier besser als gar keiner.
    """
    when = _to_local_naive(when)
    day_window = [
        (
            datetime.combine(when.date(), dt_time(hour=DAILY_WINDOW_START_HOUR)),
            datetime.combine(when.date(), dt_time(hour=DAILY_WINDOW_END_HOUR)),
        )
    ]

    windows = day_window
    if previous is not None:
        previous = _to_local_naive(previous)
        earliest = when - timedelta(days=MAX_INTERVAL_DAYS)
        if previous < earliest:
            previous = earliest
        if previous < when:
            windows = []
            day = previous.date()
            while day <= when.date():
                start = max(
                    datetime.combine(day, dt_time(hour=DAILY_WINDOW_START_HOUR)),
                    previous,
                )
                end = min(
                    datetime.combine(day, dt_time(hour=DAILY_WINDOW_END_HOUR)), when
                )
                if start <= end:
                    windows.append((start, end))
                day += timedelta(days=1)
            if not windows:
                windows = day_window

    return [(_to_utc(start), _to_utc(end)) for start, end in windows]


def _windows_mean(
    series: dict[datetime, float], windows: list[tuple[datetime, datetime]]
) -> float | None:
    """Mittel aller Stundenwerte in den Fenstern, Grenzen einschliesslich.

    Die Fenster sind nach Konstruktion ueberschneidungsfrei (je Tag hoechstens
    eines), es wird also nichts doppelt gezaehlt. Fehlen einzelne Stunden, wird
    ueber die vorhandenen gemittelt - ein unvollstaendiges Fenster ist immer
    noch naeher dran als ein einzelner Punktwert. Gar keine Stunde ergibt None.
    """
    values = [
        value
        for hour, value in series.items()
        if any(start <= hour <= end for start, end in windows)
    ]
    if not values:
        return None
    return sum(values) / len(values)


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

    Jeder Wert ist das Mittel der Tagstunden ueber den Zeitraum seit dem
    vorherigen Ladevorgang (`_interval_windows()`). Gebuendelt wird nach
    gerundeter Koordinate und zusammenhaengendem Zeitraum - typischerweise
    bleibt damit eine Anfrage je Ladeort uebrig, nicht eine je Ladevorgang.
    Die Stundenwerte einer Koordinate werden dabei ueber alle Anfragen hinweg
    zu EINER Reihe zusammengelegt und erst danach ausgewertet: ein Intervall
    kann laenger sein als ein Abfragebereich und sogar die Grenze zwischen
    Vorhersage- und Archiv-Endpunkt ueberschreiten.

    `latitude`/`longitude` sind die des Vorgangs N, auch fuer die Tage davor -
    wo das Fahrzeug zwischendurch unterwegs war, weiss niemand. Bei einem
    Ladeintervall im Alltag ist das die Gegend, in der auch gefahren wurde.
    """
    if not queries:
        return {}

    base = (base_url or DEFAULT_API_URL).rstrip("/")
    archive_base = archive_url_for(base)
    today = today or datetime.utcnow().date()
    archive_before = today - timedelta(days=ARCHIVE_SWITCH_DAYS)

    Plan = tuple[TempQuery, tuple[float, float], list[tuple[datetime, datetime]]]
    plans: list[Plan] = []
    days_by_coord: dict[tuple[float, float], set[date]] = {}
    for query in queries:
        windows = _interval_windows(query.when, query.previous)
        coord = (round_coord(query.latitude), round_coord(query.longitude))
        days = days_by_coord.setdefault(coord, set())
        for window_start, window_end in windows:
            day = window_start.date()
            while day <= window_end.date():
                days.add(day)
                day += timedelta(days=1)
        plans.append((query, coord, windows))

    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    series_by_coord: dict[tuple[float, float], dict[datetime, float]] = {}
    requests_made = 0
    try:
        for coord, days in days_by_coord.items():
            merged: dict[datetime, float] = {}
            series_by_coord[coord] = merged
            # Alles juenger als ARCHIVE_SWITCH_DAYS holt der Vorhersage-
            # Endpunkt (kein Verzug), alles aeltere das Archiv. Die Grenze
            # liegt in beiden Fenstern, es faellt also nichts dazwischen.
            by_endpoint: dict[str, list[date]] = {"archive": [], "forecast": []}
            for day in sorted(days):
                by_endpoint["archive" if day < archive_before else "forecast"].append(day)
            for endpoint, endpoint_days in by_endpoint.items():
                if not endpoint_days:
                    continue
                url = (base if endpoint == "forecast" else archive_base) + (
                    FORECAST_PATH if endpoint == "forecast" else ARCHIVE_PATH
                )
                for start, end in _cluster_dates(endpoint_days):
                    if requests_made >= MAX_REQUESTS_PER_RUN:
                        logger.warning(
                            "Wetter-Abruf: Obergrenze von %s Anfragen je Lauf "
                            "erreicht, der Rest bleibt offen",
                            MAX_REQUESTS_PER_RUN,
                        )
                        return _evaluate(plans, series_by_coord)
                    requests_made += 1
                    params = {
                        "latitude": coord[0],
                        "longitude": coord[1],
                        "hourly": "temperature_2m",
                        "timezone": "UTC",
                        "start_date": start.isoformat(),
                        # Einen Tag Puffer: das lokale Fenster eines Tages
                        # reicht je nach Zeitzone in den UTC-Folgetag hinein.
                        "end_date": (end + timedelta(days=1)).isoformat(),
                    }
                    try:
                        response = client.get(url, params=params)
                        response.raise_for_status()
                        merged.update(_parse_hourly(response.json()))
                    except Exception as exc:  # noqa: BLE001 - siehe Docstring
                        logger.warning(
                            "Wetter-Abruf fehlgeschlagen (%s): %s", url, exc
                        )
                        continue
    finally:
        if owns_client:
            client.close()
    return _evaluate(plans, series_by_coord)


def _evaluate(plans, series_by_coord) -> dict[str, float]:
    """Aus den geholten Stundenreihen je Anfrage einen Wert bilden."""
    result: dict[str, float] = {}
    for query, coord, windows in plans:
        series = series_by_coord.get(coord)
        if not series:
            continue
        value = _windows_mean(series, windows)
        if value is not None:
            result[query.session_id] = round(value, 1)
    return result


# ---------------------------------------------------------------------------
# Anwendung auf Ladevorgaenge
# ---------------------------------------------------------------------------


def previous_start_times(
    db: Session, sessions: list[models.ChargingSession]
) -> dict[str, datetime | None]:
    """Zu jedem Vorgang den Beginn des vorherigen desselben FAHRZEUGS.

    Er spannt den Zeitraum auf, ueber den gemittelt wird - dieselbe Kette, aus
    der `consumption.py` den Verbrauch rechnet, damit Temperatur und Verbrauch
    denselben Abschnitt beschreiben. Geladen wird pro Fahrzeug einmal, nicht
    pro Vorgang: bei der Automatik liegt der Vorgaenger sonst regelmaessig
    ausserhalb der betrachteten Menge.
    """
    vehicle_ids = {s.vehicle_id for s in sessions if s.vehicle_id}
    starts: dict[str, list[datetime]] = {}
    if vehicle_ids:
        rows = (
            db.query(
                models.ChargingSession.vehicle_id,
                models.ChargingSession.start_time,
            )
            .filter(models.ChargingSession.vehicle_id.in_(vehicle_ids))
            .all()
        )
        for vehicle_id, start_time in rows:
            starts.setdefault(vehicle_id, []).append(start_time)
        for values in starts.values():
            values.sort()

    previous: dict[str, datetime | None] = {}
    for session in sessions:
        known = starts.get(session.vehicle_id) or []
        # bisect_left liefert den ersten Eintrag >= start_time - der davor ist
        # damit echt frueher, auch wenn zwei Vorgaenge dieselbe Zeit tragen.
        index = bisect_left(known, session.start_time)
        previous[session.id] = known[index - 1] if index > 0 else None
    return previous


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


# Jeder hier geholte Wert ist ein Tagesmittel (siehe DAILY_WINDOW_START_HOUR),
# nie eine Messung zu einem Zeitpunkt.
WRITTEN_SOURCE = models.TemperatureSource.WEATHER_DAILY

# Beide gelten als "vom Wetterdienst geholt" und sind damit durch einen
# Korrekturlauf ersetzbar. WEATHER steht nur noch fuer Bestandszeilen aus der
# Zeit vor v0.24.1, als der Wert zum Ladebeginn geholt wurde.
WEATHER_SOURCES = (
    models.TemperatureSource.WEATHER,
    models.TemperatureSource.WEATHER_DAILY,
)


def backfill_sessions(
    db: Session,
    user: models.User,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    refresh: bool = False,
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

    `refresh=True` ist der Mittelweg und der Grund, warum es ihn ueberhaupt
    gibt: wer den Nachtrag schon einmal hat laufen lassen, bevor die Zeilen
    ohne Uhrzeit erkannt wurden, hat dort Mitternachtswerte stehen. Dann
    werden Werte neu geholt, die VOM WETTERDIENST stammen
    (`weather`/`weather_daily`) - Fahrzeug- und Handeintraege bleiben
    unangetastet, anders als bei `overwrite`.
    """
    report = BackfillReport()
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.user_id == user.id)
        .order_by(models.ChargingSession.start_time.desc())
        .all()
    )

    previous = previous_start_times(db, sessions)
    queries: list[TempQuery] = []
    by_id: dict[str, models.ChargingSession] = {}
    for session in sessions:
        report.considered += 1
        if session.outside_temp_c is not None and not overwrite:
            refreshable = refresh and session.outside_temp_source in WEATHER_SOURCES
            if not refreshable:
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
                previous=previous.get(session.id),
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
        session.outside_temp_source = WRITTEN_SOURCE
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
        previous = previous_start_times(db, sessions)
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
                    previous=previous.get(session.id),
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
            session.outside_temp_source = WRITTEN_SOURCE
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
