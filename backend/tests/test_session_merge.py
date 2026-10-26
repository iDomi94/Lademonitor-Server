"""Mehrere automatische Quellen fuer ein Fahrzeug (`session_merge.py`).

Nachgestellt ist der Enyaq vom 18.10.2026: Home Assistant meldet die Ladung
auf die Minute genau (10 -> 80 %), das VW-Datenportal sieht sie erst ab 25 %
und rund zwei Stunden spaeter. Es darf nur EIN Vorgang entstehen, und jede
Quelle darf nur das beitragen, worin sie besser ist.
"""

from datetime import datetime, timedelta

from app import bmw_cardata_poller, charge_detection, models
from app.myskoda_poller import SOURCE as MYSKODA_SOURCE
from app.vw_data_act_poller import SOURCE as VW_SOURCE
from conftest import create_vehicle, register

# Naive UTC wie in der Ladeerkennung; HA bekommt dieselben Zeitpunkte in Ortszeit.
PLUG_UTC = datetime(2026, 10, 18, 15, 40)
END_UTC = datetime(2026, 10, 19, 5, 45)


def local(when_utc: datetime) -> datetime:
    return charge_detection.utc_to_local(when_utc)


def setup(client, db_session):
    register(client)
    vehicle = create_vehicle(client, capacity=77.0)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    config = models.VwDataActConfig(user_id=owner, vehicle_id=vehicle["id"], brand="SKODA")
    db_session.add(config)
    db_session.commit()
    return vehicle, config


def ha_push(client, external_id="ha-1", start=PLUG_UTC, end=END_UTC, **extra):
    body = {
        "vehicle_external_id": "enyaq",
        "external_session_id": external_id,
        "start_time": local(start).isoformat(),
        "end_time": local(end).isoformat(),
        "soc_start": 10,
        "soc_end": 80,
        "latitude": 48.8,
        "longitude": 9.1,
        "outside_temp_c": 11.5,
    }
    body.update(extra)
    res = client.post("/api/sessions/auto", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def vw_session(db_session, config, *, start=PLUG_UTC + timedelta(hours=1, minutes=52),
               end=END_UTC + timedelta(minutes=10), soc=(25, 80), energy=49.0, km=59472):
    session = charge_detection.create_session(
        db_session, config, VW_SOURCE,
        start_time=start, end_time=end, soc_start=soc[0], soc_end=soc[1],
        charging_type=models.ChargingType.AC, odometer_km=km, outside_temp_c=None,
        latitude=None, longitude=None, notes="Erkannt ueber das VW-Datenportal.",
        energy_kwh=energy,
    )
    db_session.commit()
    return session


def all_sessions(db_session):
    db_session.expire_all()
    return db_session.query(models.ChargingSession).all()


def test_poller_after_ha_push_only_fills_gaps(client, db_session):
    _, config = setup(client, db_session)
    ha_push(client)

    assert vw_session(db_session, config) is None  # nicht neu angelegt

    [session] = all_sessions(db_session)
    assert session.external_session_id == "ha-1"
    # HA ist bei Zeiten und SoC genauer - bleibt stehen.
    assert (session.soc_start, session.soc_end) == (10, 80)
    assert session.start_time == local(PLUG_UTC)
    # Was HA nicht hatte, kommt vom Portal.
    assert session.odometer_km == 59472
    assert session.charging_type == models.ChargingType.AC
    assert "Ergänzt durch das VW-Datenportal" in session.notes


def test_ha_push_after_poller_corrects_times_and_soc(client, db_session):
    _, config = setup(client, db_session)
    vw_session(db_session, config)

    pushed = ha_push(client)

    [session] = all_sessions(db_session)
    assert pushed["id"] == session.id
    assert session.external_session_id.startswith("vwda-")
    assert session.start_time == local(PLUG_UTC)
    assert session.soc_start == 10
    assert (session.latitude, session.longitude) == (48.8, 9.1)
    assert session.outside_temp_c == 11.5
    # Geschaetzte Energie folgt dem korrigierten SoC: 70 % von 77 kWh.
    assert session.energy_is_estimated is True
    assert session.energy_kwh == 53.9
    assert "Ergänzt durch Home Assistant" in session.notes


def test_repeated_push_changes_nothing_the_second_time(client, db_session):
    _, config = setup(client, db_session)
    vw_session(db_session, config)
    ha_push(client)
    notes = all_sessions(db_session)[0].notes

    ha_push(client)  # z.B. aus der Warteschlange von Lademonitor-HA

    [session] = all_sessions(db_session)
    assert session.notes == notes


def test_confirmed_session_is_only_filled_never_overwritten(client, db_session):
    _, config = setup(client, db_session)
    vw_session(db_session, config)
    session = all_sessions(db_session)[0]
    session.needs_review = False
    db_session.commit()

    ha_push(client)

    [session] = all_sessions(db_session)
    assert session.soc_start == 25  # bestaetigt, bleibt
    assert session.latitude == 48.8  # war leer, kommt dazu


def test_two_separate_charges_stay_two(client, db_session):
    _, config = setup(client, db_session)
    # DC unterwegs, danach zwei Stunden Fahrt und der naechste Halt.
    ha_push(client, "ha-1", start=PLUG_UTC, end=PLUG_UTC + timedelta(minutes=25))
    vw_session(db_session, config, start=PLUG_UTC + timedelta(hours=2, minutes=30),
               end=PLUG_UTC + timedelta(hours=3), soc=(30, 70))

    assert len(all_sessions(db_session)) == 2


def test_other_vehicle_is_never_matched(client, db_session):
    _, config = setup(client, db_session)
    create_vehicle(client, external_id="zweitwagen", name="Zweitwagen")
    ha_push(client, vehicle_external_id="zweitwagen")

    vw_session(db_session, config)

    assert len(all_sessions(db_session)) == 2


def test_measured_energy_replaces_estimate(client, db_session):
    vehicle, _ = setup(client, db_session)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    ha_push(client)  # ohne Wallbox-Zaehler: Schaetzung 53,9 kWh
    bmw = models.BmwCarDataConfig(user_id=owner, vehicle_id=vehicle["id"])
    db_session.add(bmw)
    db_session.commit()
    start = local(PLUG_UTC)
    item = {
        "startTime": int(start.timestamp()),
        "endTime": int(local(END_UTC).timestamp()),
        "displayedStartSoc": 10,
        "displayedSoc": 80,
        "energyConsumedFromPowerGridKwh": 61.2,
        "chargingBlocks": [{"averagePowerGridKw": 7.0}],
    }

    created = bmw_cardata_poller.import_history_item(
        db_session, bmw, db_session.get(models.Vehicle, vehicle["id"]), item
    )
    db_session.commit()

    assert created is None
    [session] = all_sessions(db_session)
    assert session.energy_kwh == 61.2
    assert session.energy_is_estimated is False


def test_vw_counter_is_an_estimate_and_only_used_without_soc(client, db_session):
    _, config = setup(client, db_session)

    with_soc = vw_session(db_session, config)
    without_soc = vw_session(db_session, config, start=PLUG_UTC + timedelta(days=3),
                             end=PLUG_UTC + timedelta(days=3, hours=2), soc=(None, None),
                             energy=12.0)

    # Mit SoC gilt die eigene Schaetzung (55 % von 77 kWh), nicht der Zaehler.
    assert (with_soc.energy_kwh, with_soc.energy_is_estimated) == (42.35, True)
    assert with_soc.energy_meter is None
    # Ohne SoC ist der Zaehler besser als nichts - aber ebenfalls geschaetzt.
    assert (without_soc.energy_kwh, without_soc.energy_is_estimated) == (12.0, True)


def test_myskoda_and_vw_portal_together(client, db_session):
    vehicle, vw = setup(client, db_session)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    myskoda = models.MySkodaConfig(user_id=owner, vehicle_id=vehicle["id"])
    db_session.add(myskoda)
    db_session.commit()

    vw_session(db_session, vw)
    charge_detection.create_session(
        db_session, myskoda, MYSKODA_SOURCE,
        start_time=PLUG_UTC + timedelta(minutes=8), end_time=END_UTC,
        soc_start=10, soc_end=80, charging_type=models.ChargingType.AC,
        odometer_km=59472, outside_temp_c=None, latitude=48.8, longitude=9.1,
        notes="Erkannt ueber MySkoda.",
    )
    db_session.commit()

    [session] = all_sessions(db_session)
    # MyŠkoda pollt im Minutentakt und schlaegt das Datenportal.
    assert session.start_time == local(PLUG_UTC + timedelta(minutes=8))
    assert session.soc_start == 10
    assert session.latitude == 48.8


def test_existing_vw_portal_sessions_become_estimates(client, db_session):
    from app.database import _mark_vw_portal_energy_estimated

    vehicle, config = setup(client, db_session)
    owner = config.user_id
    db_session.add_all([
        models.ChargingSession(
            user_id=owner, vehicle_id=vehicle["id"], start_time=datetime(2026, 10, 18, 17, 30),
            energy_kwh=49.0, energy_is_estimated=False, energy_meter="vehicle",
            external_session_id=f"vwda-{vehicle['id']}-20261018T173250",
            source=models.SessionSource.AUTOMATIC,
        ),
        models.ChargingSession(
            user_id=owner, vehicle_id=vehicle["id"], start_time=datetime(2026, 10, 10, 17, 30),
            energy_kwh=40.0, energy_is_estimated=False, energy_meter="charger",
            external_session_id="ha-wallbox", source=models.SessionSource.AUTOMATIC,
        ),
    ])
    db_session.commit()

    _mark_vw_portal_energy_estimated(db_session.connection())
    db_session.commit()

    by_id = {s.external_session_id: s for s in all_sessions(db_session)}
    vw = next(s for k, s in by_id.items() if k.startswith("vwda-"))
    assert (vw.energy_kwh, vw.energy_is_estimated, vw.energy_meter) == (49.0, True, None)
    assert by_id["ha-wallbox"].energy_is_estimated is False
