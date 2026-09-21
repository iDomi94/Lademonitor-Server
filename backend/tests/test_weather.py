"""Aussentemperatur vom Wetterdienst (weather.py) und ihr Nachtrag.

Kein Test hier geht ins Netz: der HTTP-Client wird durchgereicht und durch
einen Transport ersetzt, der die Anfragen mitschreibt und eine feste Antwort
zurueckgibt. Geprueft wird damit genau das, was an diesem Modul gefaehrlich
ist - was den Server verlaesst (gerundete Koordinaten, keine feineren), welche
Stunde getroffen wird (Zeitzone!), und dass ein Probelauf wirklich nichts
schreibt.
"""

import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import models, weather
from conftest import create_vehicle, register


def _series(start: datetime, values: list[float]) -> dict:
    """Antwort im Format der Open-Meteo-API: stuendliche Werte ab `start`."""
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(values))]
    return {"hourly": {"time": times, "temperature_2m": values}}


class RecordingTransport(httpx.BaseTransport):
    """Antwortet auf jede Anfrage gleich und merkt sich die Parameter."""

    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.payload, request=request)


def make_client(payload: dict, status_code: int = 200):
    transport = RecordingTransport(payload, status_code)
    return httpx.Client(transport=transport), transport


# ---------------------------------------------------------------------------
# Was den Server verlaesst
# ---------------------------------------------------------------------------


def test_coordinates_are_rounded_before_leaving_the_server():
    """Der eigentliche Datenschutz-Punkt: die Hausnummer bleibt hier.

    Zwei Nachkommastellen sind rund 1,1 km - und kosten nichts, weil ERA5
    ohnehin in 9-25 km rastert (siehe weather.py).
    """
    client, transport = make_client(_series(datetime(2026, 1, 15, 0, 0), [1.0] * 48))
    query = weather.TempQuery(
        session_id="s1",
        when=datetime(2026, 1, 15, 12, 0),
        latitude=48.8012345,
        longitude=9.0198765,
    )

    weather.fetch_temperatures([query], client=client, today=datetime(2026, 1, 20).date())

    params = transport.requests[0].url.params
    assert params["latitude"] == "48.8"
    assert params["longitude"] == "9.02"
    # Gegenprobe: die vollen Nachkommastellen tauchen nirgends in der URL auf.
    assert "48.8012345" not in str(transport.requests[0].url)


def test_sessions_at_the_same_place_share_one_request():
    """Gebuendelt wird nach Ort, nicht je Ladevorgang - sonst waere ein
    Nachtrag ueber Jahre ein Sturm aus Einzelanfragen."""
    client, transport = make_client(_series(datetime(2026, 1, 10, 0, 0), [5.0] * 240))
    queries = [
        weather.TempQuery(
            session_id=f"s{i}",
            when=datetime(2026, 1, 10 + i, 12, 0),
            latitude=48.80,
            longitude=9.01,
        )
        for i in range(5)
    ]

    weather.fetch_temperatures(queries, client=client, today=datetime(2026, 2, 20).date())

    assert len(transport.requests) == 1


def test_a_normal_charging_rhythm_stays_one_request():
    """Der Fall, der im Live-Test durchgefallen ist: wer alle acht bis zehn
    Tage zuhause laedt, darf NICHT eine Anfrage je Ladevorgang ausloesen."""
    client, transport = make_client(_series(datetime(2026, 1, 1, 0, 0), [5.0] * (24 * 120)))
    queries = [
        weather.TempQuery(
            session_id=f"s{i}",
            when=datetime(2026, 1, 1, 18, 0) + timedelta(days=9 * i),
            latitude=48.80,
            longitude=9.01,
        )
        for i in range(10)
    ]

    weather.fetch_temperatures(queries, client=client, today=datetime(2026, 9, 21).date())

    assert len(transport.requests) == 1


def test_distant_dates_are_not_fetched_as_one_huge_range():
    """Ein einzelner Vorgang von vor Jahren darf nicht dazu fuehren, dass
    Jahre an Stundenwerten uebertragen werden."""
    client, transport = make_client(_series(datetime(2020, 1, 1, 0, 0), [5.0] * 48))
    queries = [
        weather.TempQuery("alt", datetime(2020, 1, 1, 12, 0), 48.80, 9.01),
        weather.TempQuery("neu", datetime(2026, 1, 1, 12, 0), 48.80, 9.01),
    ]

    weather.fetch_temperatures(queries, client=client, today=datetime(2026, 2, 20).date())

    assert len(transport.requests) == 2


def test_recent_and_old_sessions_use_different_endpoints():
    """Das Archiv hinkt rund fuenf Tage hinterher, der Vorhersage-Endpunkt
    reicht nur ein Vierteljahr zurueck - erst beide zusammen decken alles ab."""
    today = datetime(2026, 9, 21).date()
    client, transport = make_client(_series(datetime(2026, 9, 19, 0, 0), [5.0] * 48))
    queries = [
        weather.TempQuery("frisch", datetime(2026, 9, 20, 12, 0), 48.80, 9.01),
        weather.TempQuery("alt", datetime(2024, 3, 3, 12, 0), 48.80, 9.01),
    ]

    weather.fetch_temperatures(queries, client=client, today=today)

    paths = {request.url.path for request in transport.requests}
    hosts = {request.url.host for request in transport.requests}
    assert paths == {weather.FORECAST_PATH, weather.ARCHIVE_PATH}
    assert hosts == {"api.open-meteo.com", "archive-api.open-meteo.com"}


def test_self_hosted_url_keeps_one_host_for_both_endpoints():
    """Eine eigene Instanz beantwortet beide Pfade - nur beim oeffentlichen
    Dienst liegt das Archiv auf einem zweiten Host."""
    assert weather.archive_url_for("https://wetter.example.com") == "https://wetter.example.com"
    assert weather.archive_url_for(weather.DEFAULT_API_URL) == weather.PUBLIC_ARCHIVE_URL


# ---------------------------------------------------------------------------
# Welcher Wert herauskommt
# ---------------------------------------------------------------------------


def test_the_value_is_the_daytime_mean_not_the_moment_of_plugging_in(monkeypatch):
    """Der Wert soll die FAHRT beschreiben, nicht den Moment des Einsteckens.

    Zwischen zwei Ladevorgaengen liegen hier leicht zwei Wochen und zwanzig
    Fahrten - ein einzelner Messpunkt ist dafuer nur eine Tendenz. Der
    Wetterdienst kann mitteln, also tut er es (ein Fahrzeugsensor kann es
    nicht, dessen Werte bleiben deshalb unberuehrt).
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        client, _ = make_client(
            _series(datetime(2026, 1, 15, 0, 0), [float(i) for i in range(48)])
        )
        # Eingesteckt um 22:00 - der Wert dort waere 22.0. Gemittelt wird ueber
        # 6-20 Uhr desselben Tages, also die Werte 6..20 -> 13.0.
        query = weather.TempQuery("s1", datetime(2026, 1, 15, 22, 0), 48.80, 9.01)

        result = weather.fetch_temperatures(
            [query], client=client, today=datetime(2026, 1, 20).date()
        )

        assert result["s1"] == pytest.approx(13.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_local_wall_clock_is_converted_to_utc(monkeypatch):
    """`start_time` liegt naiv als LOKALE Zeit in der DB, die API rechnet in
    UTC. Ohne Umrechnung griffe man im Tagesgang um 1-2 Stunden daneben - also
    um 2-3 K, und damit um genau die Groessenordnung, die die Auswertung
    messen will.

    Deshalb wird hier die Zeitzone des Prozesses umgestellt und ein NAIVER
    Zeitstempel geprueft: genau so kommt der Wert aus der Datenbank. Mit einer
    zonenbehafteten Angabe waere der Test in einem UTC-Container blind.
    """
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        client, _ = make_client(
            _series(datetime(2026, 7, 1, 0, 0), [float(i) for i in range(48)])
        )
        # Sommerzeit (UTC+2): lokal 6-20 Uhr ist UTC 04:00..18:00, also die
        # Werte 4..18 -> Mittel 11.0. Ohne Umrechnung kaeme das UTC-Fenster
        # 6..20 heraus und damit 13.0.
        naive_local = datetime(2026, 7, 1, 14, 0)

        result = weather.fetch_temperatures(
            [weather.TempQuery("s1", naive_local, 48.80, 9.01)],
            client=client,
            today=datetime(2026, 7, 10).date(),
        )

        assert result["s1"] == pytest.approx(11.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_timezone_aware_timestamps_are_converted_too(monkeypatch):
    """Eine zonenbehaftete Angabe (aus einem API-Payload) muss denselben
    lokalen Ladetag treffen wie eine naive aus der Datenbank."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        client, _ = make_client(
            _series(datetime(2026, 7, 1, 0, 0), [float(i) for i in range(48)])
        )
        aware = datetime(2026, 7, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))

        result = weather.fetch_temperatures(
            [weather.TempQuery("s1", aware, 48.80, 9.01)],
            client=client,
            today=datetime(2026, 7, 10).date(),
        )

        assert result["s1"] == pytest.approx(11.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_a_failing_service_yields_no_value_and_no_exception():
    """Eine fehlende Temperatur ist ein fehlendes Detail - kein Grund, einen
    Ladevorgang oder einen ganzen Nachtrag scheitern zu lassen."""
    client, _ = make_client({"error": True, "reason": "limit"}, status_code=429)

    result = weather.fetch_temperatures(
        [weather.TempQuery("s1", datetime(2026, 1, 15, 12, 0), 48.80, 9.01)],
        client=client,
        today=datetime(2026, 1, 20).date(),
    )

    assert result == {}


def test_gaps_in_the_response_are_skipped():
    payload = _series(datetime(2026, 1, 15, 0, 0), [None] * 48)
    client, _ = make_client(payload)

    result = weather.fetch_temperatures(
        [weather.TempQuery("s1", datetime(2026, 1, 15, 12, 0), 48.80, 9.01)],
        client=client,
        today=datetime(2026, 1, 20).date(),
    )

    assert result == {}


# ---------------------------------------------------------------------------
# Nachtrag fuer Bestandsdaten
# ---------------------------------------------------------------------------


def _session_row(db, user_id, vehicle_id, **kwargs):
    row = models.ChargingSession(
        user_id=user_id,
        vehicle_id=vehicle_id,
        start_time=kwargs.pop("start_time", datetime(2026, 1, 15, 12, 0)),
        **kwargs,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_dry_run_writes_nothing_but_still_asks_the_service(client, db_session):
    """Der Probelauf fragt wirklich ab. Eine Vorschau, die nur zaehlt, wieviele
    Vorgaenge in Frage kaemen, saehe auch dann gut aus, wenn der Dienst gar
    keine Werte liefert."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(
        db_session, user.id, vehicle["id"], latitude=48.80, longitude=9.01
    )
    http, transport = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 48))

    report = weather.backfill_sessions(
        db_session, user, dry_run=True, client=http
    )

    assert transport.requests, "der Probelauf soll tatsaechlich abfragen"
    assert report.resolved == 1
    assert report.written == 0
    assert report.preview[0]["temp_c"] == pytest.approx(7.5)
    db_session.refresh(row)
    assert row.outside_temp_c is None


def test_applying_the_backfill_marks_the_source(client, db_session):
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(db_session, user.id, vehicle["id"], latitude=48.80, longitude=9.01)
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 48))

    report = weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    assert report.written == 1
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(7.5)
    assert row.outside_temp_source == models.TemperatureSource.WEATHER_DAILY


def test_existing_values_are_never_overwritten(client, db_session):
    """Ein Wert aus dem Fahrzeug ist naeher dran als einer vom Wetterdienst."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=3.0,
        outside_temp_source=models.TemperatureSource.VEHICLE,
    )
    http, transport = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 48))

    report = weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    assert report.already_set == 1
    assert not transport.requests, "fuer schon gefuellte Vorgaenge gibt es nichts zu holen"
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(3.0)
    assert row.outside_temp_source == models.TemperatureSource.VEHICLE


def test_location_coordinates_stand_in_for_a_session_without_gps(client, db_session):
    """Spritmonitor-Importe haben nie eigenes GPS. Haengt so ein Vorgang an
    einem Ladeort, ist dessen Position die beste verfuegbare Auskunft."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    location = models.ChargingLocation(
        user_id=user.id, name="Zuhause", latitude=48.80, longitude=9.01, radius_m=100
    )
    db_session.add(location)
    db_session.commit()
    row = _session_row(db_session, user.id, vehicle["id"], location_id=location.id)
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [4.0] * 48))

    report = weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    assert report.without_coordinates == 0
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(4.0)


def test_sessions_without_any_coordinates_are_counted_not_guessed(client, db_session):
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(db_session, user.id, vehicle["id"])
    http, transport = make_client(_series(datetime(2026, 1, 15, 0, 0), [4.0] * 48))

    report = weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    assert report.without_coordinates == 1
    assert not transport.requests
    db_session.refresh(row)
    assert row.outside_temp_c is None


# ---------------------------------------------------------------------------
# Einstellungen und Endpunkte
# ---------------------------------------------------------------------------


def test_autofill_is_off_by_default(client):
    register(client)

    body = client.get("/api/weather/settings").json()

    assert body["enabled"] is False
    assert body["api_url"] is None
    assert body["effective_api_url"] == weather.DEFAULT_API_URL
    assert body["coordinate_precision"] == weather.COORD_PRECISION


def test_settings_roundtrip_and_url_validation(client):
    register(client)

    ok = client.put(
        "/api/weather/settings",
        json={"enabled": True, "api_url": "https://wetter.example.com/"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["enabled"] is True
    # Trailing Slash wird abgeschnitten, damit die Pfade sauber anhaengen.
    assert ok.json()["api_url"] == "https://wetter.example.com"
    assert ok.json()["effective_api_url"] == "https://wetter.example.com"

    bad = client.put("/api/weather/settings", json={"enabled": True, "api_url": "wetter.local"})
    assert bad.status_code == 422


def test_autofill_skips_users_who_did_not_opt_in(client, db_session, monkeypatch):
    """Der Scheduler laeuft fuer alle - abfragen darf er nur fuer die, die den
    Schalter selbst umgelegt haben. Ohne Schalter darf nicht einmal eine
    Anfrage entstehen."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime.utcnow() - timedelta(hours=2),
        latitude=48.80,
        longitude=9.01,
    )

    calls: list[list] = []

    def fake_fetch(queries, **kwargs):
        calls.append(queries)
        return {q.session_id: 11.5 for q in queries}

    monkeypatch.setattr(weather, "fetch_temperatures", fake_fetch)

    assert weather.autofill_new_sessions(db_session) == 0
    assert calls == [], "ohne Opt-in darf gar nichts abgefragt werden"

    user.weather_autofill_enabled = True
    db_session.commit()

    assert weather.autofill_new_sessions(db_session) == 1
    assert len(calls) == 1
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(11.5)
    assert row.outside_temp_source == models.TemperatureSource.WEATHER_DAILY


def test_autofill_leaves_old_sessions_to_the_backfill_button(client, db_session, monkeypatch):
    """Ohne Altersgrenze wuerde der Scheduler fuer einen Vorgang, zu dem es
    dauerhaft keinen Wert gibt, alle 15 Minuten bis in alle Ewigkeit erneut
    anfragen."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    user.weather_autofill_enabled = True
    _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime.utcnow() - timedelta(days=weather.AUTOFILL_MAX_AGE_DAYS + 1),
        latitude=48.80,
        longitude=9.01,
    )
    db_session.commit()

    calls: list[list] = []
    monkeypatch.setattr(weather, "fetch_temperatures", lambda queries, **kw: calls.append(queries) or {})

    assert weather.autofill_new_sessions(db_session) == 0
    assert calls == []


def test_manual_entry_marks_the_source_as_manual(client):
    """Kein Client muss das Feld kennen: wer eine Temperatur schickt, ohne
    ihre Herkunft zu nennen, hat sie von Hand eingetragen."""
    register(client)
    vehicle = create_vehicle(client)

    created = client.post(
        "/api/sessions",
        json={
            "vehicle_id": vehicle["id"],
            "start_time": "2026-01-15T12:00:00",
            "outside_temp_c": 6.5,
        },
    ).json()

    assert created["outside_temp_source"] == "manual"


def test_resaving_an_unchanged_value_keeps_the_weather_source(client, db_session):
    """Web-UI und Apps schicken outside_temp_c bei JEDEM Speichern mit. Ein
    blosses Oeffnen und Speichern darf einen geholten Wert nicht zu "von Hand"
    umetikettieren - dieselbe Falle wie beim energy_is_estimated-Flag."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        outside_temp_c=7.5,
        outside_temp_source=models.TemperatureSource.WEATHER,
    )

    unchanged = client.patch(f"/api/sessions/{row.id}", json={"outside_temp_c": 7.5}).json()
    assert unchanged["outside_temp_source"] == "weather"

    changed = client.patch(f"/api/sessions/{row.id}", json={"outside_temp_c": 9.0}).json()
    assert changed["outside_temp_source"] == "manual"


def test_home_assistant_push_counts_as_a_vehicle_value(client):
    register(client)
    create_vehicle(client)

    pushed = client.post(
        "/api/sessions/auto",
        json={
            "vehicle_external_id": "enyaq",
            "external_session_id": "ha-1",
            "start_time": "2026-01-15T12:00:00",
            "soc_start": 20,
            "soc_end": 80,
            "outside_temp_c": 2.5,
        },
    ).json()

    assert pushed["outside_temp_source"] == "vehicle"


def test_backfill_endpoint_defaults_to_a_dry_run(client, db_session, monkeypatch):
    """Der Endpunkt darf ohne ausdrueckliches Zutun nichts veraendern."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(db_session, user.id, vehicle["id"], latitude=48.80, longitude=9.01)
    monkeypatch.setattr(
        weather, "fetch_temperatures", lambda queries, **kw: {q.session_id: 8.0 for q in queries}
    )

    body = client.post("/api/weather/backfill").json()

    assert body["dry_run"] is True
    assert body["written"] == 0
    assert body["considered"] == 1
    assert body["preview"][0]["temp_c"] == pytest.approx(8.0)
    db_session.refresh(row)
    assert row.outside_temp_c is None

    applied = client.post("/api/weather/backfill?dry_run=false").json()

    assert applied["written"] == 1
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Zeilen ohne Uhrzeit (Spritmonitor-Import) - der schaerfste Fall des Fensters
# ---------------------------------------------------------------------------


def test_a_row_without_a_time_gets_the_daytime_mean_not_the_midnight_value(monkeypatch):
    """Ein Spritmonitor-Import steht auf 00:00 - das ist das FEHLEN einer
    Angabe, nicht die Angabe "kurz nach Mitternacht". Wer sie beim Wort nimmt,
    trifft systematisch das Tagesminimum (an echten Stundendaten im Mittel
    3,9 K unter dem 6-20-Mittel).

    Die Zeitzone wird bewusst umgestellt: das Fenster ist LOKAL gemeint, und
    lokal 00:00 liegt in Mitteleuropa schon im UTC-Vortag - ein Umweg ueber
    das UTC-Datum haette also den falschen Tag erwischt.
    """
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        # Stundenwerte 0..47 ab 15.01. 00:00 UTC. Lokal 6-20 Uhr (UTC+1) ist
        # UTC 05:00..19:00 einschliesslich, also die Werte 5..19 -> Mittel 12.
        client, _ = make_client(
            _series(datetime(2026, 1, 15, 0, 0), [float(i) for i in range(48)])
        )
        query = weather.TempQuery("s1", datetime(2026, 1, 15, 0, 0), 48.80, 9.01)

        result = weather.fetch_temperatures(
            [query], client=client, today=datetime(2026, 1, 20).date()
        )

        assert result["s1"] == pytest.approx(12.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_every_fetched_row_is_marked_as_a_daily_mean(client, db_session):
    """Die Herkunft muss die Arten unterscheiden: ein Tagesmittel ist nicht
    dasselbe wie ein Fahrzeugsensor, der zwangslaeufig punktuell misst.
    Unabhaengig davon, ob die Zeile eine Uhrzeit traegt."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    imported = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 15, 0, 0),
        source=models.SessionSource.IMPORT,
        latitude=48.80,
        longitude=9.01,
    )
    with_time = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 16, 17, 30),
        source=models.SessionSource.MANUAL,
        latitude=48.80,
        longitude=9.01,
    )
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 72))

    weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    db_session.refresh(imported)
    db_session.refresh(with_time)
    assert imported.outside_temp_source == models.TemperatureSource.WEATHER_DAILY
    assert with_time.outside_temp_source == models.TemperatureSource.WEATHER_DAILY


def test_refresh_replaces_weather_values_but_not_vehicle_or_manual_ones(client, db_session):
    """Der Korrekturlauf fuer alle, die den Nachtrag schon vor dieser
    Aenderung haben laufen lassen: dort stehen Mitternachtswerte. Er darf
    genau die anfassen - nicht die Werte aus dem Fahrzeug oder von Hand."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    from_weather = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 15, 0, 0),
        source=models.SessionSource.IMPORT,
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=-2.0,
        outside_temp_source=models.TemperatureSource.WEATHER,
    )
    from_vehicle = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 16, 12, 0),
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=3.0,
        outside_temp_source=models.TemperatureSource.VEHICLE,
    )
    by_hand = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 17, 12, 0),
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=4.0,
        outside_temp_source=models.TemperatureSource.MANUAL,
    )
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 96))

    report = weather.backfill_sessions(
        db_session, user, dry_run=False, refresh=True, client=http
    )

    assert report.written == 1
    db_session.refresh(from_weather)
    db_session.refresh(from_vehicle)
    db_session.refresh(by_hand)
    assert from_weather.outside_temp_c == pytest.approx(7.5)
    assert from_weather.outside_temp_source == models.TemperatureSource.WEATHER_DAILY
    assert from_vehicle.outside_temp_c == pytest.approx(3.0)
    assert by_hand.outside_temp_c == pytest.approx(4.0)


def test_without_refresh_an_existing_weather_value_stays(client, db_session):
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    row = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 15, 0, 0),
        source=models.SessionSource.IMPORT,
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=-2.0,
        outside_temp_source=models.TemperatureSource.WEATHER,
    )
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 48))

    report = weather.backfill_sessions(db_session, user, dry_run=False, client=http)

    assert report.written == 0
    assert report.already_set == 1
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(-2.0)


def test_the_refresh_preview_shows_the_previous_value(client, db_session):
    """Sonst sieht man dem Probelauf nicht an, ob sich ueberhaupt etwas
    aendert - und genau das ist die Frage vor einem Korrekturlauf."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime(2026, 1, 15, 0, 0),
        source=models.SessionSource.IMPORT,
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=-2.0,
        outside_temp_source=models.TemperatureSource.WEATHER,
    )
    http, _ = make_client(_series(datetime(2026, 1, 15, 0, 0), [7.5] * 48))

    report = weather.backfill_sessions(
        db_session, user, dry_run=True, refresh=True, client=http
    )

    assert report.preview[0]["previous_temp_c"] == pytest.approx(-2.0)
    assert report.preview[0]["temp_c"] == pytest.approx(7.5)


def test_autofill_never_touches_a_vehicle_value(client, db_session, monkeypatch):
    """Die Grenze zwischen beiden Welten: der Wetterdienst mittelt ueber den
    Tag, ein Fahrzeugsensor misst beim Einstecken. Ein vorhandener
    Fahrzeugwert darf deshalb nie still durch ein Tagesmittel ersetzt
    werden - sonst haette dieselbe Spalte zwei Bedeutungen."""
    register(client)
    vehicle = create_vehicle(client)
    user = db_session.query(models.User).first()
    user.weather_autofill_enabled = True
    row = _session_row(
        db_session,
        user.id,
        vehicle["id"],
        start_time=datetime.utcnow() - timedelta(days=1),
        latitude=48.80,
        longitude=9.01,
        outside_temp_c=3.0,
        outside_temp_source=models.TemperatureSource.VEHICLE,
    )
    db_session.commit()

    calls: list[list] = []

    def fake_fetch(queries, **kwargs):
        calls.append(queries)
        return {q.session_id: 11.5 for q in queries}

    monkeypatch.setattr(weather, "fetch_temperatures", fake_fetch)

    assert weather.autofill_new_sessions(db_session) == 0
    assert calls == [], "fuer eine Zeile mit Fahrzeugwert darf nichts abgefragt werden"
    db_session.refresh(row)
    assert row.outside_temp_c == pytest.approx(3.0)
    assert row.outside_temp_source == models.TemperatureSource.VEHICLE


# ---------------------------------------------------------------------------
# Gemittelt wird ueber das ganze Intervall seit dem vorherigen Ladevorgang
# ---------------------------------------------------------------------------


def _daily_constants(start: datetime, days: int) -> dict:
    """Stundenreihe, in der jeder Tag seine eigene konstante Temperatur hat.

    Tag 0 ist 0 Grad, Tag 1 ist 1 Grad und so weiter - damit laesst sich am
    Ergebnis ablesen, WELCHE Tage in den Mittelwert eingegangen sind.
    """
    return _series(start, [float(i // 24) for i in range(days * 24)])


def test_the_mean_covers_every_day_since_the_previous_session(monkeypatch):
    """10.07. 18:00 -> 20.07. 09:00: der Rest des 10., die vollen Tagstunden
    vom 11. bis 19. und der Anfang des 20. - nicht nur der Ladetag."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        client, _ = make_client(_daily_constants(datetime(2026, 7, 10, 0, 0), 12))
        query = weather.TempQuery(
            "s1",
            datetime(2026, 7, 20, 9, 0),
            48.80,
            9.01,
            previous=datetime(2026, 7, 10, 18, 0),
        )

        result = weather.fetch_temperatures(
            [query], client=client, today=datetime(2026, 7, 25).date()
        )

        # Tag 10.07. (=0 Grad) liefert 18-20 Uhr -> 3 Stunden, die Tage 11.-19.
        # (=1..9 Grad) je 15 Stunden, der 20.07. (=10 Grad) 6-9 Uhr -> 4.
        expected = (3 * 0 + 15 * sum(range(1, 10)) + 4 * 10) / (3 + 15 * 9 + 4)
        assert result["s1"] == pytest.approx(round(expected, 1))
        # Gegenprobe: nur der Ladetag waere 10.0 gewesen.
        assert result["s1"] != pytest.approx(10.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_two_sessions_on_the_same_day_only_average_the_hours_between_them(monkeypatch):
    """Zwischen 08:00 und 12:00 liegt auch nur diese eine Fahrt - den ganzen
    Tag zu mitteln waere hier schlechter, nicht besser."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        # Temperatur = Stunde des Tages, damit der Ausschnitt ablesbar ist.
        client, _ = make_client(
            _series(datetime(2026, 1, 15, 0, 0), [float(i % 24) for i in range(48)])
        )
        query = weather.TempQuery(
            "s1",
            datetime(2026, 1, 15, 12, 0),
            48.80,
            9.01,
            previous=datetime(2026, 1, 15, 8, 0),
        )

        result = weather.fetch_temperatures(
            [query], client=client, today=datetime(2026, 1, 20).date()
        )

        # Stunden 8..12 -> Mittel 10.0; die vollen Tagstunden waeren 13.0.
        assert result["s1"] == pytest.approx(10.0)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_a_very_long_gap_is_capped(monkeypatch):
    """Ein Abstand von Monaten ist keine Fahrt mehr, sondern eine Luecke -
    daraus einen Jahresdurchschnitt zu bilden sagt ueber nichts mehr etwas."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        client, transport = make_client(
            _daily_constants(datetime(2025, 1, 1, 0, 0), 400)
        )
        when = datetime(2026, 1, 15, 12, 0)
        query = weather.TempQuery(
            "s1", when, 48.80, 9.01, previous=datetime(2025, 3, 1, 12, 0)
        )

        weather.fetch_temperatures(
            [query], client=client, today=datetime(2026, 1, 20).date()
        )

        requested_start = date.fromisoformat(
            transport.requests[0].url.params["start_date"]
        )
        earliest = (when - timedelta(days=weather.MAX_INTERVAL_DAYS)).date()
        assert requested_start >= earliest
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_an_interval_spanning_both_endpoints_still_yields_one_value(monkeypatch):
    """Ein Intervall kann die Grenze zwischen Vorhersage und Archiv
    ueberschreiten. Die Stundenwerte beider Anfragen gehoeren dann in
    denselben Mittelwert - deshalb werden sie je Koordinate zusammengelegt und
    erst danach ausgewertet."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        today = date(2026, 5, 20)
        client, transport = make_client(
            _daily_constants(datetime(2026, 4, 1, 0, 0), 60)
        )
        query = weather.TempQuery(
            "s1",
            datetime(2026, 4, 25, 12, 0),
            48.80,
            9.01,
            previous=datetime(2026, 4, 10, 12, 0),
        )

        result = weather.fetch_temperatures([query], client=client, today=today)

        assert len(transport.requests) == 2, "je Endpunkt eine Anfrage"
        paths = {r.url.path for r in transport.requests}
        assert paths == {weather.FORECAST_PATH, weather.ARCHIVE_PATH}
        assert "s1" in result
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_the_backfill_uses_the_previous_session_of_the_same_vehicle(
    client, db_session, monkeypatch
):
    """End-to-End: der Vorgaenger kommt aus der Datenbank, nicht aus dem
    Aufrufer - und es ist der desselben Fahrzeugs."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        register(client)
        vehicle = create_vehicle(client)
        user = db_session.query(models.User).first()
        first = _session_row(
            db_session,
            user.id,
            vehicle["id"],
            start_time=datetime(2026, 7, 10, 12, 0),
            latitude=48.80,
            longitude=9.01,
        )
        second = _session_row(
            db_session,
            user.id,
            vehicle["id"],
            start_time=datetime(2026, 7, 13, 12, 0),
            latitude=48.80,
            longitude=9.01,
        )
        http, _ = make_client(_daily_constants(datetime(2026, 7, 10, 0, 0), 6))

        weather.backfill_sessions(db_session, user, dry_run=False, client=http)

        db_session.refresh(first)
        db_session.refresh(second)
        # Der erste Vorgang hat keinen Vorgaenger -> nur sein Ladetag (0 Grad).
        assert first.outside_temp_c == pytest.approx(0.0)
        # Der zweite mittelt ueber 10.07. 12-20 Uhr (0), 11. und 12. je 6-20
        # Uhr (1 bzw. 2) und 13.07. 6-12 Uhr (3).
        expected = (9 * 0 + 15 * 1 + 15 * 2 + 7 * 3) / (9 + 15 + 15 + 7)
        assert second.outside_temp_c == pytest.approx(round(expected, 1))
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
