"""Reifensaetze und der temperaturbereinigte Vergleich (tires.py).

Der Kern dieser Auswertung ist nicht die Rechnung, sondern das, was sie NICHT
behaupten darf: Winterreifen werden im Winter gefahren, ein roher Vergleich
der beiden Saison-Durchschnitte misst also die Jahreszeit und nennt sie
Reifen. Die Faelle hier pruefen entsprechend vor allem, dass der
Temperatureffekt wirklich herausgerechnet wird und dass eine fehlende
Ueberlappung der Temperaturbereiche als solche gemeldet wird.
"""

from datetime import datetime, timedelta

import pytest

from app import models, tires
from app.temperature import TempPoint

from conftest import create_vehicle, register

BASE = datetime(2026, 1, 1, 8, 0, 0)


def tire_set(idx, kind, day, *, brand="Michelin", model="Pilot", size="235/45 R21",
             size_rear=None, vehicle_id="v1", odo=None):
    return models.TireSet(
        id=f"t{idx}",
        vehicle_id=vehicle_id,
        kind=kind,
        installed_on=BASE + timedelta(days=day),
        odometer_km=odo,
        brand=brand,
        model=model,
        size=size,
        size_rear=size_rear,
    )


def session(idx, day, vehicle_id="v1"):
    return models.ChargingSession(
        id=f"s{idx}", vehicle_id=vehicle_id, start_time=BASE + timedelta(days=day)
    )


def driven(idx, day, odo, *, kwh=40.0, vehicle_id="v1"):
    """Ladevorgang mit Kilometerstand - fuer die Laufleistung der Uebersicht."""
    row = session(idx, day, vehicle_id=vehicle_id)
    row.odometer_km = odo
    row.energy_kwh = kwh
    return row


def point(idx, *, temp, consumption, km=200.0):
    return TempPoint(
        session_id=f"s{idx}",
        start_time=BASE,
        temp_c=temp,
        consumption=consumption,
        km=km,
        method="soc_corrected",
        season="winter",
    )


def test_before_the_first_change_no_set_is_assumed():
    """Der aelteste Eintrag war vor seinem eigenen Montagedatum gerade NICHT
    montiert - was davor drauf war, weiss niemand."""
    sets = [tire_set(1, models.TireKind.SUMMER, 100)]

    assert tires.set_at(sets, BASE + timedelta(days=50)) is None
    assert tires.set_at(sets, BASE + timedelta(days=150)) is sets[0]


def test_a_drive_across_a_change_belongs_to_neither_set():
    """Der Verbrauch eines Vorgangs beschreibt die Strecke davor. Faellt der
    Wechsel in genau dieses Intervall, ist die Fahrt auf beiden Saetzen
    gelaufen - sie wird verworfen und gezaehlt, nicht einem Satz zugeschlagen."""
    sets = [
        tire_set(1, models.TireKind.SUMMER, 0),
        tire_set(2, models.TireKind.WINTER, 10),
    ]
    sessions = [session(0, 2), session(1, 8), session(2, 12), session(3, 20)]

    assigned, spanning = tires.sets_for_drives(sessions, sets)

    assert spanning == 1  # die Fahrt von Tag 8 bis Tag 12
    assert assigned["s1"].id == "t1"
    assert assigned["s3"].id == "t2"
    assert "s2" not in assigned
    # Der allererste Vorgang hat keinen Vorgaenger und damit keine Fahrt.
    assert "s0" not in assigned


def test_the_same_set_mounted_again_stays_one_group():
    """Eine Zeile ist eine Montage, kein physischer Satz - derselbe Reifen im
    naechsten Winter waere sonst eine zweite, halb so grosse Gruppe."""
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 60, brand="Michelin", model="Sport EV"),
        tire_set(3, models.TireKind.WINTER, 120, brand="Nokian", model="Snowproof"),
    ]
    sessions = [session(i, day) for i, day in enumerate([2, 20, 40, 70, 90, 110, 130, 150, 170])]
    assigned, _ = tires.sets_for_drives(sessions, sets)
    points = [point(i, temp=10.0, consumption=18.0) for i in range(9) if f"s{i}" in assigned]

    result = tires.compare(sessions, sets, points)

    winter = [g for g in result.by_set if g.kind == "winter"]
    assert len(winter) == 1
    assert winter[0].label == "Nokian Snowproof 235/45 R21"


def _seasonal_dataset(winter_penalty_pct):
    """Ein Jahr mit Winter- und Sommerreifen, beide im eigenen Temperaturband
    plus einer Ueberlappung in den Uebergangsmonaten.

    Der Verbrauch folgt exakt einer Geraden ueber der Temperatur (20 kWh/100 km
    bei 0 Grad, 0,2 weniger je Grad) - der Winter-Satz liegt zusaetzlich um
    den uebergebenen Prozentsatz darueber. Findet die Auswertung genau diesen
    Prozentsatz wieder, ist der Temperatureffekt sauber herausgerechnet.
    """
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 200, brand="Michelin", model="Sport EV"),
    ]
    sessions, points = [], []
    idx = 0

    def add(day, temp, penalty):
        nonlocal idx
        sessions.append(session(idx, day))
        if idx:  # der erste Vorgang hat keine Fahrt davor
            points.append(
                point(idx, temp=temp, consumption=(20.0 - 0.2 * temp) * (1 + penalty / 100))
            )
        idx += 1

    for day, temp in zip(range(2, 200, 20), [-5, 0, 2, 5, 8, 10, 12, 14, 15, 16]):
        add(day, temp, winter_penalty_pct)
    for day, temp in zip(range(210, 400, 20), [8, 10, 12, 15, 18, 20, 24, 28, 30, 32]):
        add(day, temp, 0.0)
    return sessions, sets, points


def test_the_temperature_effect_is_removed_from_the_comparison():
    sessions, sets, points = _seasonal_dataset(winter_penalty_pct=10.0)

    result = tires.compare(sessions, sets, points)

    assert result.winter_vs_summer_pct == pytest.approx(10.0, abs=0.6)
    assert result.overlap_ok is True
    # Die rohen Durchschnitte wuerden das Gegenteil behaupten: der Winter-Satz
    # faehrt in der Kaelte, also bei hoeherem Verbrauch - aber der Sommer-Satz
    # liegt im gleichen Vergleich nur wegen der Temperatur darunter.
    winter = next(g for g in result.by_kind if g.kind == "winter")
    summer = next(g for g in result.by_kind if g.kind == "summer")
    assert winter.avg_consumption > summer.avg_consumption
    assert winter.delta_pct_vs_model > summer.delta_pct_vs_model


def test_identical_tires_show_no_difference():
    """Gegenprobe: ohne Aufschlag darf trotz voellig verschiedener
    Temperaturbaender kein Unterschied herauskommen."""
    sessions, sets, points = _seasonal_dataset(winter_penalty_pct=0.0)

    result = tires.compare(sessions, sets, points)

    assert result.winter_vs_summer_pct == pytest.approx(0.0, abs=0.6)


def test_without_a_shared_temperature_range_the_result_is_flagged():
    """Ohne Ueberlappung rechnet das Modell jeden Satz in Temperaturen hoch, in
    denen er nie gefahren ist. Die Zahl kommt trotzdem - aber markiert."""
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 200, brand="Michelin", model="Sport EV"),
    ]
    sessions, points = [], []
    idx = 0
    for day, temp in list(zip(range(2, 120, 20), [-8, -5, -2, 0, 2, 4])) + list(
        zip(range(210, 330, 20), [22, 24, 26, 28, 30, 32])
    ):
        sessions.append(session(idx, day))
        if idx:
            points.append(point(idx, temp=temp, consumption=20.0 - 0.2 * temp))
        idx += 1

    result = tires.compare(sessions, sets, points)

    assert result.overlap_ok is False
    assert result.overlap_span_c < 0  # echte Luecke zwischen beiden Baendern


def test_groups_below_the_minimum_are_not_reported():
    sets = [tire_set(1, models.TireKind.SUMMER, 0)]
    sessions = [session(0, 2), session(1, 10), session(2, 20)]
    points = [point(1, temp=10.0, consumption=18.0), point(2, temp=12.0, consumption=18.5)]

    result = tires.compare(sessions, sets, points)

    assert result.by_kind == []
    assert result.by_set == []


def test_drives_without_a_set_are_counted():
    """Fahrten vor dem ersten Wechsel gehen nicht still verloren - die Anzeige
    nennt sie, sonst sieht man der Auswertung nicht an, wie duenn sie steht."""
    sets = [tire_set(1, models.TireKind.SUMMER, 100)]
    sessions = [session(i, day) for i, day in enumerate([2, 20, 40, 110, 130, 150])]
    points = [point(i, temp=10.0, consumption=18.0) for i in range(1, 6)]

    result = tires.compare(sessions, sets, points)

    # Tag 20, Tag 40 und Tag 110: die ersten beiden liegen ganz vor dem
    # Wechsel, die dritte reicht ueber ihn hinweg - aber was davor montiert
    # war, ist nicht eingetragen, also ist sie keine Fahrt "ueber einen
    # Wechsel", sondern eine ohne bekannten Satz.
    assert result.drives_without_set == 3
    assert result.drives_spanning_change == 0


def test_a_staggered_fitment_shows_both_axles():
    """Mischbereifung: beide Groessen in einer Zeile, vorne zuerst."""
    mixed = tire_set(1, models.TireKind.SUMMER, 0, brand="Michelin", model="Sport EV",
                     size="235/45 R21", size_rear="255/40 R21")
    same = tire_set(2, models.TireKind.WINTER, 100, brand="Nokian", model="Snowproof",
                    size="235/45 R21")

    assert tires.set_label(mixed) == "Michelin Sport EV 235/45 R21 / 255/40 R21"
    # Gleiche Groesse rundum bleibt eine Angabe - kein "X / X".
    assert tires.set_label(same) == "Nokian Snowproof 235/45 R21"


def test_the_rear_size_separates_two_otherwise_identical_sets():
    """Derselbe Reifen einmal rundum und einmal als Mischbereifung sind zwei
    verschiedene Saetze - sie duerfen nicht zu einer Gruppe verschmelzen."""
    sets = [
        tire_set(1, models.TireKind.SUMMER, 0, brand="Michelin", model="Sport EV",
                 size="235/45 R21"),
        tire_set(2, models.TireKind.WINTER, 100, brand="Nokian", model="Snowproof"),
        tire_set(3, models.TireKind.SUMMER, 200, brand="Michelin", model="Sport EV",
                 size="235/45 R21", size_rear="255/40 R21"),
    ]
    sessions = [driven(i, day, 10_000 + i * 1000) for i, day in enumerate([2, 30, 130, 230, 260])]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=300))

    summer = [s for s in result.sets if s.kind == "summer"]
    assert len(summer) == 2
    assert {s.mountings for s in summer} == {1}


# ---------- Uebersicht ----------


def test_overview_counts_days_and_kilometres_per_mounting():
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 100, brand="Michelin", model="Sport EV"),
    ]
    sessions = [
        driven(0, 2, 10_000),
        driven(1, 30, 11_000),
        driven(2, 60, 12_000),
        driven(3, 120, 13_000),   # Fahrt ueber den Wechsel hinweg
        driven(4, 150, 14_500),
    ]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=200))

    winter = next(m for m in result.mountings if m.kind == "winter")
    summer = next(m for m in result.mountings if m.kind == "summer")
    assert winter.days == 100 and winter.removed_on == sets[1].installed_on
    assert winter.is_current is False
    assert winter.drives == 2 and winter.km == 2000.0  # Tag 30 und Tag 60
    assert summer.is_current is True and summer.removed_on is None
    assert summer.days == 100  # Montage bis "jetzt"
    # Die Fahrt von Tag 60 bis Tag 120 lief auf beiden Saetzen und zaehlt fuer
    # keinen - sie wird ausgewiesen statt verteilt.
    assert summer.drives == 1 and summer.km == 1500.0
    assert result.drives_spanning_change == 1


def test_overview_adds_up_the_mountings_of_one_set():
    """Derselbe Satz im naechsten Winter ist dieselbe Gummimischung - seine
    Laufleistung steht auf EINER Zeile, sonst ist "wieviel km sind drauf"
    nicht zu beantworten."""
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 100, brand="Michelin", model="Sport EV"),
        tire_set(3, models.TireKind.WINTER, 200, brand="Nokian", model="Snowproof"),
    ]
    sessions = [driven(i, day, 10_000 + i * 1000) for i, day in enumerate([2, 30, 60, 130, 160, 230, 260])]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=300))

    winter = next(s for s in result.sets if s.kind == "winter")
    assert winter.mountings == 2
    assert winter.is_current is True
    assert winter.days_mounted == 200  # 100 im ersten, 100 seit der zweiten
    assert winter.age_days == 300      # Gummi altert auch zwischen den Saisons
    assert winter.km == sum(
        m.km for m in result.mountings if m.kind == "winter"
    )


def test_kilometres_come_from_the_odometer_when_both_changes_have_one():
    """Der Kilometerstand beim Wechsel ist die genauere Quelle: seine Differenz
    enthaelt auch die Fahrt ueber den Wechsel hinweg, die keinem Satz
    zugeordnet werden kann."""
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, odo=10_000, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 100, odo=12_500, brand="Michelin", model="Sport EV"),
    ]
    sessions = [
        driven(0, 2, 10_100),
        driven(1, 30, 11_000),
        driven(2, 60, 12_000),
        driven(3, 120, 13_000),
        driven(4, 150, 14_500),
    ]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=200))

    winter = next(m for m in result.mountings if m.kind == "winter")
    summer = next(m for m in result.mountings if m.kind == "summer")
    assert winter.km == 2500.0 and winter.km_source == "odometer"
    # Ueber die Fahrten waeren es nur 1000 km gewesen (Tag 30 und Tag 60) -
    # der Rest steckt im Anfahren des ersten Vorgangs und in der Fahrt ueber
    # den Wechsel.
    assert winter.drives == 2
    # Der noch montierte Satz rechnet gegen den letzten bekannten Stand.
    assert summer.km == 2000.0 and summer.km_source == "odometer"


def test_a_missing_or_wrong_odometer_falls_back_to_the_drives():
    """Ein fehlender Stand am zweiten Wechsel - oder ein Zahlendreher, der
    rueckwaerts laeuft - darf keine negative Laufleistung ergeben."""
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, odo=10_000, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 100, odo=1_000, brand="Michelin", model="Sport EV"),
    ]
    sessions = [driven(0, 2, 10_100), driven(1, 30, 11_000), driven(2, 60, 12_000)]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=200))

    winter = next(m for m in result.mountings if m.kind == "winter")
    assert winter.km_source == "drives"
    assert winter.km == 1900.0  # die beiden Fahrten, mehr weiss die App nicht


def test_a_set_is_only_exact_when_all_its_mountings_are():
    sets = [
        tire_set(1, models.TireKind.WINTER, 0, odo=10_000, brand="Nokian", model="Snowproof"),
        tire_set(2, models.TireKind.SUMMER, 100, brand="Michelin", model="Sport EV"),
        tire_set(3, models.TireKind.WINTER, 200, brand="Nokian", model="Snowproof"),
    ]
    sessions = [driven(i, day, 10_000 + i * 1000) for i, day in enumerate([2, 30, 130, 230, 260])]

    result = tires.overview(sessions, sets, now=BASE + timedelta(days=300))

    winter = next(s for s in result.sets if s.kind == "winter")
    # Die erste Montage hat einen Stand, die zweite nicht - die Summe ist
    # damit nicht durchgaengig gemessen.
    assert winter.km_source == "drives"


def test_overview_is_empty_without_tire_sets():
    result = tires.overview([driven(0, 2, 10_000), driven(1, 20, 10_500)], [])

    assert result.mountings == [] and result.sets == []


def test_overview_over_the_api(client):
    register(client)
    vehicle = create_vehicle(client)
    created = client.post(
        "/api/tires",
        json={
            "vehicle_id": vehicle["id"],
            "kind": "winter",
            "installed_on": "2026-01-15T00:00:00",
            "odometer_km": 41000,
            "brand": "Nokian",
        },
    )
    assert created.status_code == 201
    assert created.json()["odometer_km"] == 41000

    body = client.get("/api/tires/overview").json()

    assert len(body["mountings"]) == 1
    assert body["mountings"][0]["is_current"] is True
    assert body["mountings"][0]["removed_on"] is None
    assert body["sets"][0]["mountings"] == 1


# ---------- API ----------


def test_crud_over_the_api(client):
    register(client)
    vehicle = create_vehicle(client)

    created = client.post(
        "/api/tires",
        json={
            "vehicle_id": vehicle["id"],
            "kind": "winter",
            "installed_on": "2026-10-15T00:00:00",
            "size": "235/45 R21",
            "size_rear": "255/40 R21",
            "brand": "Nokian",
            "model": "Snowproof",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["size_rear"] == "255/40 R21"
    tire_id = created.json()["id"]

    listed = client.get("/api/tires")
    assert [t["id"] for t in listed.json()] == [tire_id]

    patched = client.patch(f"/api/tires/{tire_id}", json={"brand": "Continental"})
    assert patched.status_code == 200
    assert patched.json()["brand"] == "Continental"
    assert patched.json()["model"] == "Snowproof"  # unveraendert

    assert client.delete(f"/api/tires/{tire_id}").status_code == 204
    assert client.get("/api/tires").json() == []


def test_a_tire_set_for_a_foreign_vehicle_is_rejected(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)

    register(client, username="zweite")
    response = client.post(
        "/api/tires",
        json={
            "vehicle_id": vehicle["id"],
            "kind": "summer",
            "installed_on": "2026-04-01T00:00:00",
        },
    )

    # 404 statt 403 - eine fremde ID soll nicht einmal als existierend
    # erkennbar sein (siehe Pro-Nutzer-Datentrennung).
    assert response.status_code == 404


def test_foreign_tire_sets_are_invisible(client):
    register(client, username="erste")
    vehicle = create_vehicle(client)
    created = client.post(
        "/api/tires",
        json={
            "vehicle_id": vehicle["id"],
            "kind": "summer",
            "installed_on": "2026-04-01T00:00:00",
        },
    )
    tire_id = created.json()["id"]

    register(client, username="zweite")
    assert client.get("/api/tires").json() == []
    assert client.patch(f"/api/tires/{tire_id}", json={"brand": "X"}).status_code == 404
    assert client.delete(f"/api/tires/{tire_id}").status_code == 404


def test_deleting_a_user_takes_the_tire_sets_with_it(client):
    """Reifensaetze haengen per Fremdschluessel am Nutzer UND am Fahrzeug -
    bleibt beim Loeschen eine Zeile stehen, scheitert das Loeschen des Kontos
    an der Datenbank. Genau dieser Fehlertyp steckte 2026-09-09 schon einmal
    in der Admin-Loeschfunktion."""
    register(client, username="admin")  # der erste Nutzer ist Admin
    admin_token = client.headers["Authorization"]

    register(client, username="zweite")
    vehicle = create_vehicle(client)
    assert (
        client.post(
            "/api/tires",
            json={
                "vehicle_id": vehicle["id"],
                "kind": "winter",
                "installed_on": "2026-10-15T00:00:00",
            },
        ).status_code
        == 201
    )

    client.headers["Authorization"] = admin_token
    users = client.get("/api/auth/users").json()
    victim = next(u for u in users if u["username"] == "zweite")

    assert client.delete(f"/api/auth/users/{victim['id']}").status_code == 204
