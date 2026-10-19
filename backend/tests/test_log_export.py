"""ZIP-Export der Debug-Protokolle und Umschalter auf der Seite API & Debug."""

import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest

from app import models
from conftest import create_vehicle, register

SOURCES = [
    ("myskoda", models.MySkodaLogEntry),
    ("bmw-cardata", models.BmwCarDataLogEntry),
    ("vw-data-act", models.VwDataActLogEntry),
    ("renault", models.RenaultLogEntry),
]


@pytest.mark.parametrize("api, model", SOURCES)
def test_zip_contains_every_entry_and_each_payload(client, db_session, api, model):
    register(client)
    vehicle = create_vehicle(client)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    t0 = datetime(2026, 10, 19, 5, 0)
    db_session.add_all([
        model(user_id=owner, vehicle_id=vehicle["id"], created_at=t0 + timedelta(minutes=15),
              event="session_finished", message="Ladevorgang angelegt", payload='{"soc": 80}'),
        model(user_id=owner, vehicle_id=vehicle["id"], created_at=t0,
              event="poll", message="ohne Rohdaten"),
    ])
    db_session.commit()

    response = client.get(f"/api/{api}/logs/export.zip", params={"vehicle_id": vehicle["id"]})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    overview = json.loads(archive.read("protokoll.json"))
    # Aelteste zuerst, auch die Zeile ohne Rohdaten ist dabei.
    assert [row["event"] for row in overview] == ["poll", "session_finished"]
    assert "payload_file" not in overview[0]
    name = overview[1]["payload_file"]
    assert name == "rohdaten/0002_2026-10-19T05-15-00_session_finished.json"
    assert json.loads(archive.read(name)) == {"soc": 80}


def test_zip_only_contains_own_entries(client, db_session):
    register(client)
    vehicle = create_vehicle(client)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    db_session.add(models.VwDataActLogEntry(user_id=owner, vehicle_id=vehicle["id"],
                                            event="poll", message="fremd", payload="{}"))
    db_session.commit()
    register(client, username="zweiter", password="geheim1234")

    response = client.get("/api/vw-data-act/logs/export.zip")

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert json.loads(archive.read("protokoll.json")) == []


def test_api_page_has_source_switch_and_zip_buttons(client):
    register(client)

    response = client.get("/api-debug")

    assert response.status_code == 200
    assert response.text.count('class="api-source"') == 4
    for api in ("myskoda", "bmw-cardata", "vw-data-act", "renault"):
        assert f"api/{api}/logs/export.zip" in response.text
    assert "api.source" not in response.text and "api.log_zip" not in response.text
