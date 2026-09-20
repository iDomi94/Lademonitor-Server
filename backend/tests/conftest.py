"""Gemeinsame Test-Vorrichtungen.

Die App laeuft im Betrieb gegen Postgres; die Tests laufen gegen ein
SQLite-in-memory, damit sie ohne Container und ohne Zustand zwischen zwei
Laeufen durchlaufen. Was dabei NICHT mitgetestet wird, ist Absicht und sollte
bewusst bleiben:

* `database.run_light_migrations()` ist bewusst rohes, Postgres-eigenes SQL
  (`information_schema`, `pg_constraint`, `ADD COLUMN IF NOT EXISTS`) und wird
  hier uebersprungen - auf SQLite wuerde es schlicht scheitern. Die Tabellen
  entstehen stattdessen direkt aus den Modellen (`Base.metadata.create_all`),
  also aus demselben Schema, das die Migration herstellen soll.
* Damit gilt: die Migrationen selbst bleiben ungetestet und muessen weiterhin
  gegen eine Kopie der echten Datenbank geprueft werden (siehe CLAUDE.md).
"""

import os
import sys
from pathlib import Path

import pytest

# Arbeitsverzeichnis wie im Container: main.py mountet "app/static" und
# Jinja2 liest aus "app/templates" - beides relativ zu backend/.
BACKEND_DIR = Path(__file__).resolve().parents[1]
os.chdir(BACKEND_DIR)
sys.path.insert(0, str(BACKEND_DIR))

# Muss VOR dem Import von app.database stehen: das Modul baut die Engine beim
# Import aus dieser Variable.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
# Feld-Verschluesselung ist opt-in (siehe crypto.py). Die Tests laufen ohne
# Schluessel, damit sie den Normalfall einer Heimnetz-Installation abbilden -
# ein gesetzter Schluessel aus der Umgebung wuerde sie sonst still veraendern.
os.environ.pop("FIELD_ENCRYPTION_KEY", None)

if "reverse_geocoder" not in sys.modules:
    try:  # pragma: no cover - haengt allein an der Umgebung
        import reverse_geocoder  # noqa: F401
    except Exception:
        # Das Paket zieht scipy/numpy nach und hat nicht fuer jede Architektur
        # ein fertiges Wheel (genau die Sorge, die auch im CLAUDE.md zum
        # Unraid-Build steht). Fuer die Tests hier ist es nebensaechlich: was
        # geprueft wird, ist die Ladeort-Zuordnung per Koordinatenvergleich,
        # nicht der Ortsname aus dem Offline-Datensatz.
        import types

        stub = types.ModuleType("reverse_geocoder")
        stub.search = lambda *args, **kwargs: [{}]
        sys.modules["reverse_geocoder"] = stub

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import database  # noqa: E402

# Eine Engine fuer den ganzen Testlauf, aber pro Test frische Tabellen (siehe
# `client`). StaticPool + check_same_thread=False, weil TestClient in einem
# anderen Thread laeuft als der Test und ":memory:" sonst pro Verbindung eine
# eigene, leere Datenbank waere.
_test_engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


@event.listens_for(_test_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record):
    """SQLite prueft Fremdschluessel standardmaessig NICHT - ohne dieses PRAGMA
    wuerden Tests gruen bleiben, die auf Postgres an einer Constraint-Verletzung
    scheitern (z.B. ein Nutzer, der geloescht wird, obwohl noch Zeilen auf ihn
    zeigen). Genau so ein Fehler steckte 2026-09-09 in der Admin-Loeschfunktion."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

database.engine = _test_engine
database.SessionLocal = _TestSession
# Postgres-eigenes SQL, siehe Modul-Docstring.
database.run_light_migrations = lambda: None

from app import models  # noqa: E402,F401  (registriert die Tabellen an Base)
from app.database import Base  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    """Importiert app.main genau einmal - der Import hat Seiteneffekte
    (create_all, Router-Registrierung) und ist nicht wiederholbar."""
    from app import main

    return main


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    Base.metadata.drop_all(bind=_test_engine)
    Base.metadata.create_all(bind=_test_engine)

    # Der Rate-Limiter zaehlt im Prozessspeicher, nicht in der Datenbank - ohne
    # das Zuruecksetzen waeren die fuenf erlaubten Registrierungen pro Stunde
    # nach wenigen Tests aufgebraucht und alle weiteren bekaemen 429.
    from app import rate_limit

    rate_limit.reset()

    # Bewusst OHNE `with`: der Kontextmanager wuerde den lifespan starten und
    # damit die drei Hintergrund-Scheduler (WebDAV-Backup, MyŠkoda-Polling,
    # Benachrichtigungen). Die haben in einem Test nichts zu suchen - sie
    # wuerden nebenher auf derselben Datenbank arbeiten und das Ergebnis vom
    # Zufall der Taktung abhaengig machen. `get_db` zieht seine Session aus
    # `database.SessionLocal`, das oben bereits auf die Test-Engine zeigt.
    test_client = TestClient(app_module.app)
    yield test_client
    test_client.close()


@pytest.fixture
def db_session():
    """Direkter DB-Zugriff fuer Tests, die nicht ueber die API gehen."""
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


def register(client, username="tester", password="geheim1234", email=None):
    """Legt ein Konto an und haengt den Bearer-Token an den Client.

    Der erste registrierte Nutzer wird automatisch Admin (siehe
    routers/auth.py) - fuer die Tests hier ohne Bedeutung, aber der Grund,
    warum ein zweiter Nutzer in denselben Tests kein Admin ist.
    """
    payload = {"username": username, "password": password}
    if email:
        payload["email"] = email
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def create_vehicle(client, external_id="enyaq", name="Enyaq", capacity=77.0):
    response = client.post(
        "/api/vehicles",
        json={"external_id": external_id, "name": name, "battery_capacity_kwh": capacity},
    )
    assert response.status_code == 201, response.text
    return response.json()
