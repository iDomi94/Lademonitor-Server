import csv
import io
from datetime import datetime

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, myskoda_import
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


# --- MySkoda-App (Abgleich, siehe myskoda_import.py) ------------------------


class MySkodaChangeOut(BaseModel):
    field: str
    old: object | None
    new: object | None


class MySkodaChargeOut(BaseModel):
    start: datetime
    end: datetime
    soc_start: int
    soc_end: int
    energy_kwh: float | None


class MySkodaUpdateOut(BaseModel):
    key: str
    session_id: str
    start_time: datetime
    provider_name: str | None
    source: str
    charges: list[MySkodaChargeOut]
    changes: list[MySkodaChangeOut]


class MySkodaNewOut(BaseModel):
    key: str
    charge: MySkodaChargeOut
    charging_type: str | None


class MySkodaHintOut(BaseModel):
    kind: str
    when: datetime
    data: dict


class MySkodaPreviewResponse(BaseModel):
    charges_total: int
    charges_ignored: int
    unchanged: int
    already_imported: int
    updates: list[MySkodaUpdateOut]
    new_sessions: list[MySkodaNewOut]
    hints: list[MySkodaHintOut]


class MySkodaCommitResponse(BaseModel):
    updated: int
    created: int


def _myskoda_plan(db: Session, user: models.User, vehicle: models.Vehicle, content: bytes) -> myskoda_import.Plan:
    try:
        charges = myskoda_import.parse_export(content)
    except UnicodeDecodeError:
        raise HTTPException(422, "Die Datei ist keine UTF-8-CSV.")
    except myskoda_import.ExportFormatError as exc:
        raise HTTPException(
            422,
            f"Kein MySkoda-Export erkannt, es fehlen Spalten ({', '.join(exc.missing)}). "
            f"Gefundene Spalten: {exc.header}",
        )
    return myskoda_import.build_plan(db, user, vehicle, charges)


def _charge_out(charge: myskoda_import.Charge) -> MySkodaChargeOut:
    return MySkodaChargeOut(
        start=charge.start,
        end=charge.end,
        soc_start=charge.soc_start,
        soc_end=charge.soc_end,
        energy_kwh=charge.energy_kwh,
    )


def _json_value(value: object) -> object:
    if isinstance(value, models.ChargingType):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@router.post("/myskoda/preview", response_model=MySkodaPreviewResponse)
async def preview_myskoda(
    vehicle_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Schreibt nichts - rechnet nur den Abgleich aus."""
    vehicle = _get_owned_vehicle(db, user, vehicle_id)
    plan = _myskoda_plan(db, user, vehicle, await file.read())
    return MySkodaPreviewResponse(
        charges_total=plan.charges_total,
        charges_ignored=plan.charges_ignored,
        unchanged=plan.unchanged,
        already_imported=plan.already_imported,
        updates=[
            MySkodaUpdateOut(
                key=u.key,
                session_id=u.session.id,
                start_time=u.session.start_time,
                provider_name=u.session.provider.name if u.session.provider else None,
                source=u.session.source.value,
                charges=[_charge_out(c) for c in u.charges],
                changes=[
                    MySkodaChangeOut(field=c.field, old=_json_value(c.old), new=_json_value(c.new))
                    for c in u.changes
                ],
            )
            for u in plan.updates
        ],
        new_sessions=[
            MySkodaNewOut(
                key=n.key,
                charge=_charge_out(n.charge),
                charging_type=n.charging_type.value if n.charging_type else None,
            )
            for n in plan.new_sessions
        ],
        hints=[MySkodaHintOut(kind=h.kind, when=h.when, data=h.data) for h in plan.hints],
    )


@router.post("/myskoda/commit", response_model=MySkodaCommitResponse)
async def commit_myskoda(
    vehicle_id: str,
    file: UploadFile = File(...),
    accept: str = Form("[]"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """`accept` ist die JSON-Liste der angehakten Schluessel aus der Vorschau.
    Der Plan wird aus derselben Datei neu gerechnet; ein Schluessel, den es
    darin (nicht mehr) gibt, bleibt folgenlos."""
    vehicle = _get_owned_vehicle(db, user, vehicle_id)
    try:
        accepted = json.loads(accept)
    except json.JSONDecodeError:
        raise HTTPException(422, "accept muss eine JSON-Liste sein")
    if not isinstance(accepted, list) or not all(isinstance(k, str) for k in accepted):
        raise HTTPException(422, "accept muss eine JSON-Liste sein")
    plan = _myskoda_plan(db, user, vehicle, await file.read())
    updated, created = myskoda_import.apply_plan(db, user, vehicle, plan, set(accepted))
    return MySkodaCommitResponse(updated=updated, created=created)


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
