"""Vollstaendiger Datenbank-Export/-Import als ZIP mit CSVs.

Gedacht als Backup/Restore-Mechanismus (z.B. Neuaufsetzen des Servers), nicht
als flexibles Datenaustauschformat - Reimport erwartet exakt die vom Export
erzeugte ZIP-Struktur mit den vier festen Dateinamen. IDs bleiben beim
Reimport erhalten (Datensaetze mit bereits vorhandener ID werden
uebersprungen, kein Ueberschreiben) - dadurch ist der Import gefahrlos
mehrfach ausfuehrbar.
"""

import csv
import io
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/backup", tags=["backup"])

VEHICLE_FIELDS = [
    "id", "external_id", "name", "brand", "model",
    "battery_capacity_kwh", "is_active", "created_at",
]
PROVIDER_FIELDS = [
    "id", "name", "last_price_ac_per_kwh", "last_price_dc_per_kwh", "notes", "created_at",
]
LOCATION_FIELDS = [
    "id", "name", "latitude", "longitude", "radius_m",
    "default_provider_id", "default_provider_name", "created_at",
]
SESSION_FIELDS = [
    "id", "vehicle_id", "vehicle_name", "provider_id", "provider_name",
    "location_id", "location_name", "start_time", "end_time", "charging_type",
    "soc_start", "soc_end", "energy_kwh", "energy_is_estimated", "odometer_km",
    "price_total", "price_per_kwh", "latitude", "longitude", "geocoded_place",
    "source", "needs_review", "external_session_id", "notes", "created_at", "updated_at",
]

README_TEMPLATE = """Lademonitor Backup
===================

Erstellt am: {timestamp}

Inhalt dieser ZIP-Datei:
- vehicles.csv    - alle Fahrzeuge
- providers.csv   - alle Ladeanbieter
- locations.csv   - alle bekannten Ladeorte
- sessions.csv    - alle Ladevorgaenge

Format:
- Trennzeichen: Komma, UTF-8, Kopfzeile in der ersten Zeile
- Datumsfelder im ISO-8601-Format (z.B. 2026-08-11T14:30:00)
- Spalten wie "vehicle_name", "provider_name", "location_name" und
  "default_provider_name" sind NUR zur besseren Lesbarkeit dieser Datei -
  fuer den Reimport in Lademonitor zaehlen ausschliesslich die *_id-Spalten
  mit den Original-UUIDs. Diese Zusatzspalten koennen also veraltet sein
  (z.B. falls ein Fahrzeug seither umbenannt wurde) und werden beim Import
  ignoriert.

Reimport:
- Ueber die Web-UI (Einstellungen -> Daten-Backup -> "Backup importieren")
  diese ZIP-Datei unveraendert wieder hochladen.
- Datensaetze werden anhand ihrer Original-ID wiederhergestellt. Bereits
  vorhandene IDs werden uebersprungen (kein Ueberschreiben) - der Import ist
  daher gefahrlos mehrfach ausfuehrbar.
- Die Reihenfolge (Fahrzeuge vor Anbietern vor Ladeorten vor Ladevorgaengen,
  wegen Fremdschluesseln) uebernimmt der Importer automatisch.
"""


def _c(value):
    """Wandelt einen Modellwert in einen CSV-tauglichen String um."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # Enum
        return value.value
    return value


def _write_csv(fieldnames: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def build_backup_zip(db: Session, user: models.User) -> bytes:
    """Baut dieselbe Backup-ZIP wie der manuelle Export - eigene Funktion,
    damit sowohl GET /export als auch der WebDAV-Auto-Backup-Scheduler
    (siehe ../webdav_backup.py) dieselbe Logik nutzen statt sie zu duplizieren."""
    vehicles = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.user_id == user.id)
        .order_by(models.Vehicle.created_at)
        .all()
    )
    providers = (
        db.query(models.Provider)
        .filter(models.Provider.user_id == user.id)
        .order_by(models.Provider.created_at)
        .all()
    )
    locations = (
        db.query(models.ChargingLocation)
        .filter(models.ChargingLocation.user_id == user.id)
        .order_by(models.ChargingLocation.created_at)
        .all()
    )
    sessions = (
        db.query(models.ChargingSession)
        .filter(models.ChargingSession.user_id == user.id)
        .order_by(models.ChargingSession.start_time)
        .all()
    )

    vehicles_by_id = {v.id: v for v in vehicles}
    providers_by_id = {p.id: p for p in providers}
    locations_by_id = {l.id: l for l in locations}

    vehicle_rows = [
        {
            "id": _c(v.id), "external_id": _c(v.external_id), "name": _c(v.name),
            "brand": _c(v.brand), "model": _c(v.model),
            "battery_capacity_kwh": _c(v.battery_capacity_kwh),
            "is_active": _c(v.is_active), "created_at": _c(v.created_at),
        }
        for v in vehicles
    ]
    provider_rows = [
        {
            "id": _c(p.id), "name": _c(p.name),
            "last_price_ac_per_kwh": _c(p.last_price_ac_per_kwh),
            "last_price_dc_per_kwh": _c(p.last_price_dc_per_kwh),
            "notes": _c(p.notes), "created_at": _c(p.created_at),
        }
        for p in providers
    ]
    location_rows = [
        {
            "id": _c(l.id), "name": _c(l.name), "latitude": _c(l.latitude),
            "longitude": _c(l.longitude), "radius_m": _c(l.radius_m),
            "default_provider_id": _c(l.default_provider_id),
            "default_provider_name": _c(
                providers_by_id[l.default_provider_id].name
            ) if l.default_provider_id in providers_by_id else "",
            "created_at": _c(l.created_at),
        }
        for l in locations
    ]
    session_rows = [
        {
            "id": _c(s.id),
            "vehicle_id": _c(s.vehicle_id),
            "vehicle_name": _c(vehicles_by_id[s.vehicle_id].name) if s.vehicle_id in vehicles_by_id else "",
            "provider_id": _c(s.provider_id),
            "provider_name": _c(providers_by_id[s.provider_id].name) if s.provider_id in providers_by_id else "",
            "location_id": _c(s.location_id),
            "location_name": _c(locations_by_id[s.location_id].name) if s.location_id in locations_by_id else "",
            "start_time": _c(s.start_time), "end_time": _c(s.end_time),
            "charging_type": _c(s.charging_type),
            "soc_start": _c(s.soc_start), "soc_end": _c(s.soc_end),
            "energy_kwh": _c(s.energy_kwh), "energy_is_estimated": _c(s.energy_is_estimated),
            "odometer_km": _c(s.odometer_km), "price_total": _c(s.price_total),
            "price_per_kwh": _c(s.price_per_kwh), "latitude": _c(s.latitude),
            "longitude": _c(s.longitude), "geocoded_place": _c(s.geocoded_place),
            "source": _c(s.source), "needs_review": _c(s.needs_review),
            "external_session_id": _c(s.external_session_id), "notes": _c(s.notes),
            "created_at": _c(s.created_at), "updated_at": _c(s.updated_at),
        }
        for s in sessions
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README_TEMPLATE.format(timestamp=datetime.utcnow().isoformat()))
        zf.writestr("vehicles.csv", _write_csv(VEHICLE_FIELDS, vehicle_rows))
        zf.writestr("providers.csv", _write_csv(PROVIDER_FIELDS, provider_rows))
        zf.writestr("locations.csv", _write_csv(LOCATION_FIELDS, location_rows))
        zf.writestr("sessions.csv", _write_csv(SESSION_FIELDS, session_rows))
    return buf.getvalue()


@router.get("/export")
def export_backup(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    content = build_backup_zip(db, user)
    filename = f"lademonitor-backup-{datetime.utcnow().strftime('%Y-%m-%d')}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class BackupImportResult(BaseModel):
    vehicles_imported: int
    vehicles_skipped: int
    providers_imported: int
    providers_skipped: int
    locations_imported: int
    locations_skipped: int
    sessions_imported: int
    sessions_skipped: int


def _parse_bool(v: str | None) -> bool:
    return (v or "").strip().lower() == "true"


def _parse_float(v: str | None) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def _parse_int(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _parse_str(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def _parse_dt(v: str | None) -> datetime | None:
    v = (v or "").strip()
    return datetime.fromisoformat(v) if v else None


def _read_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


@router.post("/import", response_model=BackupImportResult)
async def import_backup(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(422, "Bitte die vom Export erzeugte ZIP-Datei hochladen")

    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(422, "Datei ist keine gueltige ZIP-Datei")

    names = set(zf.namelist())
    for required in ("vehicles.csv", "providers.csv", "locations.csv", "sessions.csv"):
        if required not in names:
            raise HTTPException(
                422, f"{required} fehlt in der ZIP-Datei - bitte die vollstaendige Export-ZIP hochladen"
            )

    # id -> owner user_id, damit sessions.csv spaeter nicht versehentlich auf
    # Fahrzeuge/Anbieter/Orte EINES ANDEREN Nutzers verweisen kann (z.B. falls
    # eine fremde Backup-ZIP hochgeladen wird, die zufaellig dieselben UUIDs
    # enthaelt wie bereits vorhandene Daten)
    existing_vehicle_owners = {row[0]: row[1] for row in db.query(models.Vehicle.id, models.Vehicle.user_id).all()}
    existing_provider_owners = {row[0]: row[1] for row in db.query(models.Provider.id, models.Provider.user_id).all()}
    existing_location_owners = {
        row[0]: row[1] for row in db.query(models.ChargingLocation.id, models.ChargingLocation.user_id).all()
    }
    existing_session_ids = {row[0] for row in db.query(models.ChargingSession.id).all()}

    vehicles_imported = vehicles_skipped = 0
    for row in _read_csv(zf.read("vehicles.csv")):
        if row["id"] in existing_vehicle_owners:
            vehicles_skipped += 1
            continue
        db.add(
            models.Vehicle(
                id=row["id"],
                user_id=user.id,
                external_id=row["external_id"],
                name=row["name"],
                brand=_parse_str(row.get("brand")),
                model=_parse_str(row.get("model")),
                battery_capacity_kwh=_parse_float(row.get("battery_capacity_kwh")),
                is_active=_parse_bool(row.get("is_active")),
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        existing_vehicle_owners[row["id"]] = user.id
        vehicles_imported += 1
    db.commit()

    providers_imported = providers_skipped = 0
    for row in _read_csv(zf.read("providers.csv")):
        if row["id"] in existing_provider_owners:
            providers_skipped += 1
            continue
        db.add(
            models.Provider(
                id=row["id"],
                user_id=user.id,
                name=row["name"],
                last_price_ac_per_kwh=_parse_float(row.get("last_price_ac_per_kwh")),
                last_price_dc_per_kwh=_parse_float(row.get("last_price_dc_per_kwh")),
                notes=_parse_str(row.get("notes")),
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        existing_provider_owners[row["id"]] = user.id
        providers_imported += 1
    db.commit()

    locations_imported = locations_skipped = 0
    for row in _read_csv(zf.read("locations.csv")):
        if row["id"] in existing_location_owners:
            locations_skipped += 1
            continue
        default_provider_id = _parse_str(row.get("default_provider_id"))
        if default_provider_id and existing_provider_owners.get(default_provider_id) != user.id:
            default_provider_id = None
        db.add(
            models.ChargingLocation(
                id=row["id"],
                user_id=user.id,
                name=row["name"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                radius_m=_parse_int(row.get("radius_m")) or 100,
                default_provider_id=default_provider_id,
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        existing_location_owners[row["id"]] = user.id
        locations_imported += 1
    db.commit()

    sessions_imported = sessions_skipped = 0
    for row in _read_csv(zf.read("sessions.csv")):
        if row["id"] in existing_session_ids:
            sessions_skipped += 1
            continue
        vehicle_id = _parse_str(row.get("vehicle_id"))
        if not vehicle_id or existing_vehicle_owners.get(vehicle_id) != user.id:
            sessions_skipped += 1
            continue

        provider_id = _parse_str(row.get("provider_id"))
        if provider_id and existing_provider_owners.get(provider_id) != user.id:
            provider_id = None
        location_id = _parse_str(row.get("location_id"))
        if location_id and existing_location_owners.get(location_id) != user.id:
            location_id = None

        charging_type_raw = _parse_str(row.get("charging_type"))
        charging_type = models.ChargingType(charging_type_raw) if charging_type_raw else None
        source = models.SessionSource(_parse_str(row.get("source")) or "import")

        db.add(
            models.ChargingSession(
                id=row["id"],
                user_id=user.id,
                vehicle_id=vehicle_id,
                provider_id=provider_id,
                location_id=location_id,
                start_time=_parse_dt(row.get("start_time")),
                end_time=_parse_dt(row.get("end_time")),
                charging_type=charging_type,
                soc_start=_parse_int(row.get("soc_start")),
                soc_end=_parse_int(row.get("soc_end")),
                energy_kwh=_parse_float(row.get("energy_kwh")),
                energy_is_estimated=_parse_bool(row.get("energy_is_estimated")),
                odometer_km=_parse_int(row.get("odometer_km")),
                price_total=_parse_float(row.get("price_total")),
                price_per_kwh=_parse_float(row.get("price_per_kwh")),
                latitude=_parse_float(row.get("latitude")),
                longitude=_parse_float(row.get("longitude")),
                geocoded_place=_parse_str(row.get("geocoded_place")),
                source=source,
                needs_review=_parse_bool(row.get("needs_review")),
                external_session_id=_parse_str(row.get("external_session_id")),
                notes=_parse_str(row.get("notes")),
                created_at=_parse_dt(row.get("created_at")),
                updated_at=_parse_dt(row.get("updated_at")),
            )
        )
        existing_session_ids.add(row["id"])
        sessions_imported += 1
    db.commit()

    return BackupImportResult(
        vehicles_imported=vehicles_imported,
        vehicles_skipped=vehicles_skipped,
        providers_imported=providers_imported,
        providers_skipped=providers_skipped,
        locations_imported=locations_imported,
        locations_skipped=locations_skipped,
        sessions_imported=sessions_imported,
        sessions_skipped=sessions_skipped,
    )
