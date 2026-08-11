import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/import", tags=["import"])


def _get_owned_vehicle(db: Session, user: models.User, vehicle_id: str) -> models.Vehicle:
    vehicle = (
        db.query(models.Vehicle)
        .filter(models.Vehicle.id == vehicle_id, models.Vehicle.user_id == user.id)
        .first()
    )
    if not vehicle:
        raise HTTPException(404, "Vehicle nicht gefunden")
    return vehicle


# Spritmonitor-Exportspalten variieren leicht je nach Sprache/Fahrzeugtyp-Einstellung.
# Diese Liste deckt die gaengigen deutschen Exportnamen ab; bei Abweichungen bitte
# im Vorschau-Schritt pruefen und ggf. hier ergaenzen.
COLUMN_ALIASES = {
    "date": ["Datum", "date"],
    "odometer": ["Km-Stand", "Kilometerstand", "Tacho", "odometer"],
    "quantity": ["Spritmenge", "Menge", "kWh", "quantity"],
    "price_total": ["Kosten", "Preis gesamt", "price"],
    "price_per_unit": ["Preis/Einheit", "Preis pro kWh", "price/unit"],
    "location": ["Tankstelle", "Ort", "location"],
    "notes": ["Bemerkung", "Kommentar", "notes"],
    "soc_end": ["Ladezustand", "SoC"],
}


def find_column(header: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in header:
            return c
    return None


class ImportPreviewRow(BaseModel):
    date: str | None
    odometer: str | None
    quantity: str | None
    price_total: str | None
    price_per_unit: str | None
    location: str | None
    will_import: bool
    reason: str | None = None


class ImportPreviewResponse(BaseModel):
    detected_columns: dict[str, str | None]
    rows: list[ImportPreviewRow]


class ImportCommitResponse(BaseModel):
    imported: int
    skipped_duplicates: int


def _parse_csv(content: bytes) -> tuple[list[str], list[dict]]:
    text = content.decode("utf-8-sig")
    # Spritmonitor nutzt oft Semikolon als Trenner
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = list(reader)
    header = reader.fieldnames or []
    return header, rows


@router.post("/spritmonitor/preview", response_model=ImportPreviewResponse)
async def preview_import(
    vehicle_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _get_owned_vehicle(db, user, vehicle_id)

    content = await file.read()
    header, raw_rows = _parse_csv(content)

    detected = {key: find_column(header, aliases) for key, aliases in COLUMN_ALIASES.items()}
    if not detected["date"] or not detected["quantity"]:
        raise HTTPException(
            422,
            f"Konnte Datum/Menge-Spalten nicht erkennen. Gefundene Spalten: {header}. "
            "Bitte COLUMN_ALIASES im Backend anpassen.",
        )

    existing_keys = {
        (s.start_time.date().isoformat(), s.odometer_km)
        for s in db.query(models.ChargingSession)
        .filter(models.ChargingSession.vehicle_id == vehicle_id)
        .all()
    }

    preview_rows = []
    for row in raw_rows:
        date_raw = row.get(detected["date"], "")
        odo_raw = row.get(detected["odometer"], "") if detected["odometer"] else ""
        will_import = True
        reason = None
        try:
            parsed_date = _parse_date(date_raw)
            odo_value = _to_int(odo_raw)
            key = (parsed_date.date().isoformat(), odo_value)
            if key in existing_keys:
                will_import = False
                reason = "Duplikat (Datum+Kilometerstand bereits vorhanden)"
        except Exception:
            will_import = False
            reason = "Datum konnte nicht geparst werden"

        preview_rows.append(
            ImportPreviewRow(
                date=date_raw,
                odometer=odo_raw or None,
                quantity=row.get(detected["quantity"], "") if detected["quantity"] else None,
                price_total=row.get(detected["price_total"], "") if detected["price_total"] else None,
                price_per_unit=row.get(detected["price_per_unit"], "")
                if detected["price_per_unit"]
                else None,
                location=row.get(detected["location"], "") if detected["location"] else None,
                will_import=will_import,
                reason=reason,
            )
        )

    return ImportPreviewResponse(detected_columns=detected, rows=preview_rows)


@router.post("/spritmonitor/commit", response_model=ImportCommitResponse)
async def commit_import(
    vehicle_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _get_owned_vehicle(db, user, vehicle_id)

    content = await file.read()
    header, raw_rows = _parse_csv(content)
    detected = {key: find_column(header, aliases) for key, aliases in COLUMN_ALIASES.items()}

    existing_keys = {
        (s.start_time.date().isoformat(), s.odometer_km)
        for s in db.query(models.ChargingSession)
        .filter(models.ChargingSession.vehicle_id == vehicle_id)
        .all()
    }

    imported, skipped = 0, 0
    for row in raw_rows:
        date_raw = row.get(detected["date"], "")
        odo_raw = row.get(detected["odometer"], "") if detected["odometer"] else ""
        try:
            parsed_date = _parse_date(date_raw)
        except Exception:
            skipped += 1
            continue

        odometer = _to_int(odo_raw)
        key = (parsed_date.date().isoformat(), odometer)
        if key in existing_keys:
            skipped += 1
            continue

        quantity_raw = row.get(detected["quantity"], "") if detected["quantity"] else ""
        price_total_raw = row.get(detected["price_total"], "") if detected["price_total"] else ""
        price_unit_raw = (
            row.get(detected["price_per_unit"], "") if detected["price_per_unit"] else ""
        )
        location_raw = row.get(detected["location"], "") if detected["location"] else ""
        soc_end_raw = row.get(detected["soc_end"], "") if detected.get("soc_end") else ""

        energy_kwh = _to_float(quantity_raw)
        price_total = _to_float(price_total_raw)
        price_per_kwh = _to_float(price_unit_raw)
        # Spritmonitor liefert i.d.R. nur den Gesamtpreis - Preis/kWh daraus ableiten
        if price_per_kwh is None and price_total and energy_kwh:
            price_per_kwh = round(price_total / energy_kwh, 4)

        soc_end = _to_int(soc_end_raw)

        session = models.ChargingSession(
            vehicle_id=vehicle_id,
            user_id=user.id,
            start_time=parsed_date,
            odometer_km=odometer,
            energy_kwh=energy_kwh,
            price_total=price_total,
            price_per_kwh=price_per_kwh,
            soc_end=soc_end,
            notes=f"Importiert aus Spritmonitor. Ort: {location_raw}" if location_raw else "Importiert aus Spritmonitor",
            source=models.SessionSource.IMPORT,
        )
        db.add(session)
        existing_keys.add(key)
        imported += 1

    db.commit()
    return ImportCommitResponse(imported=imported, skipped_duplicates=skipped)


def _parse_date(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unbekanntes Datumsformat: {raw}")


def _to_int(raw: str | None) -> int | None:
    value = _to_float(raw)
    return int(round(value)) if value is not None else None


def _to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace(".", "").replace(",", ".") if "," in raw else raw
    try:
        return float(cleaned)
    except ValueError:
        return None
