"""Der automatische Abruf geht an, sobald Zugangsdaten gespeichert werden.

BMW CarData (Verbinden statt Zugangsdaten) steht in test_bmw_cardata.py.
"""

import pytest

from app import models
from conftest import create_vehicle, register

VIN = "TMBJC9NY0RF012345"

# api, Zugangsdaten, Feldname fuer die Abfrage, ob Zugangsdaten gespeichert sind
SOURCES = [
    ("myskoda", {"api_key": "key-123"}, "has_api_key"),
    ("vw-data-act", {"brand": "SKODA", "email": "a@b.de", "password": "geheim"}, "has_password"),
    ("renault", {"email": "a@b.de", "password": "geheim"}, "has_password"),
]


@pytest.mark.parametrize("api, credentials, _flag", SOURCES)
def test_saving_credentials_with_vin_turns_polling_on(client, api, credentials, _flag):
    register(client)
    vehicle = create_vehicle(client)

    res = client.put(f"/api/{api}/configs/{vehicle['id']}",
                     json={"enabled": False, "vin": VIN, **credentials})

    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is True


@pytest.mark.parametrize("api, credentials, flag", SOURCES)
def test_credentials_first_vin_later(client, api, credentials, flag):
    register(client)
    vehicle = create_vehicle(client)
    url = f"/api/{api}/configs/{vehicle['id']}"

    first = client.put(url, json=credentials).json()
    assert first[flag] and first["enabled"] is False  # ohne FIN geht es nicht

    assert client.put(url, json={**credentials, "vin": VIN}).json()["enabled"] is True


@pytest.mark.parametrize("api, credentials, _flag", SOURCES)
def test_switching_off_sticks_until_new_credentials(client, api, credentials, _flag):
    register(client)
    vehicle = create_vehicle(client)
    url = f"/api/{api}/configs/{vehicle['id']}"
    client.put(url, json={"vin": VIN, **credentials})

    base = {k: v for k, v in credentials.items() if k in ("brand",)}
    assert client.put(url, json={**base, "enabled": False}).json()["enabled"] is False
    # Anderes Feld speichern laesst ihn aus ...
    assert client.put(url, json={**base, "vin": VIN}).json()["enabled"] is False
    # ... neue Zugangsdaten schalten ihn wieder ein.
    fresh = {**credentials}
    fresh["api_key" if "api_key" in fresh else "password"] = "neu-456"
    assert client.put(url, json=fresh).json()["enabled"] is True


def test_not_switched_on_while_another_source_runs(client, db_session):
    register(client)
    vehicle = create_vehicle(client)
    owner = db_session.get(models.Vehicle, vehicle["id"]).user_id
    db_session.add(models.MySkodaConfig(user_id=owner, vehicle_id=vehicle["id"], enabled=True,
                                        api_key="key", vin=VIN))
    db_session.commit()

    res = client.put(f"/api/vw-data-act/configs/{vehicle['id']}",
                     json={"brand": "SKODA", "email": "a@b.de", "password": "geheim", "vin": VIN})

    # Kein 422 beim Speichern, der Abruf bleibt einfach aus.
    assert res.status_code == 200
    assert res.json()["enabled"] is False
