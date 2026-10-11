"""Vollstaendiger Datenbank-Export/-Import als ZIP mit CSVs.

Gedacht als Backup/Restore-Mechanismus (z.B. Neuaufsetzen des Servers), nicht
als flexibles Datenaustauschformat - Reimport erwartet exakt die vom Export
erzeugte ZIP-Struktur mit den vier festen Dateinamen (plus optional
`fees.csv` seit v0.27.0, `costs.csv` seit v0.32.0, `reminders.csv` seit
v0.33.0 und `reimbursement_rates.csv` seit v0.35.0 - aeltere ZIPs haben
sie nicht und bleiben importierbar).

Zwei Faelle, die der Import auseinanderhalten muss (siehe `_target_id`):

- **Restore auf einen frischen Server**: die Original-IDs bleiben erhalten.
- **Import in ein ANDERES Konto derselben Instanz**: die Primaerschluessel
  sind global, die Daten aber pro Nutzer getrennt - hier bekommt der
  importierende Nutzer eigene Kopien mit neu vergebenen IDs, und die
  Fremdschluessel werden ueber `*_id_map` mitgezogen.

In beiden Faellen ist der Import idempotent: schon vorhandene Datensaetze
werden uebersprungen statt ueberschrieben, erkannt an der ID UND an den
Fachdaten (`own_*`-Schluessel) - letzteres, damit auch eine ZIP von einem
anderen Server mit voellig anderen UUIDs nichts doppelt anlegt.
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
from ..reminders import KINDS as REMINDER_KINDS

router = APIRouter(prefix="/api/backup", tags=["backup"])

VEHICLE_FIELDS = [
    "id", "external_id", "name", "brand", "model",
    "battery_capacity_kwh", "is_active", "created_at",
    # Seit v0.32.0 (Kauf/Leasing), beim Import optional (fehlt -> keine Angabe).
    "ownership", "purchase_price", "purchase_date", "first_registration",
    "purchase_odometer_km", "depreciation_monthly_override",
    "leasing_rate_monthly", "leasing_start", "leasing_end", "leasing_down_payment",
    # Seit v0.35.0 (Dienstwagen), beim Import optional (fehlt -> kein Dienstwagen).
    "is_company_car", "reimbursement_mode", "reimbursement_include_fees",
    "employee_name", "employee_number", "license_plate", "reimbursement_monthly_mail",
]
COMPANY_CAR_BOOL_FIELDS = (
    "is_company_car", "reimbursement_include_fees", "reimbursement_monthly_mail",
)
COMPANY_CAR_STR_FIELDS = ("employee_name", "employee_number", "license_plate")
OWNERSHIP_FLOAT_FIELDS = (
    "purchase_price", "depreciation_monthly_override",
    "leasing_rate_monthly", "leasing_down_payment",
)
OWNERSHIP_DATE_FIELDS = ("purchase_date", "first_registration", "leasing_start", "leasing_end")
PROVIDER_FIELDS = [
    "id", "name", "last_price_ac_per_kwh", "last_price_dc_per_kwh", "notes", "created_at",
    # Seit v0.29.0, beim Import optional (fehlt -> Ladesaeule, wie vorher).
    "energy_meter",
    # Seit v0.31.0, beim Import optional (fehlt -> unterwegs, wie vorher).
    "charging_place",
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
    # Ans Ende gehaengt statt neben odometer_km einsortiert: der Import liest
    # die Spalten ueber ihren Namen (DictReader), die Reihenfolge ist ihm egal -
    # aber eine aeltere Backup-ZIP OHNE diese Spalte soll sich weiterhin
    # einlesen lassen, und ein Diff zweier Exporte bleibt so lesbar.
    "outside_temp_c",
    # Ebenfalls hinten angehaengt (siehe oben): eine ZIP ohne diese Spalte -
    # jedes Backup vor v0.24.0 - bleibt importierbar, die Herkunft ist dann
    # eben unbekannt.
    "outside_temp_source",
    # Seit v0.29.0 (Messort-Ausnahme, leer = wie der Anbieter).
    "energy_meter",
]

# Seit v0.27.0 (Grundgebuehren, siehe models.ProviderFee). Beim Import
# OPTIONAL - jede aeltere ZIP hat die Datei nicht.
FEE_FIELDS = [
    "id", "provider_id", "provider_name", "amount", "interval",
    "start_date", "end_date", "label", "notes", "created_at",
]

# Seit v0.32.0 (Fahrzeugkosten, siehe models.VehicleCost), beim Import optional.
COST_FIELDS = [
    "id", "vehicle_id", "vehicle_name", "amount", "category", "interval",
    "start_date", "end_date", "odometer_km", "label", "notes", "created_at",
]

# Seit v0.35.0 (Erstattungssaetze, siehe models.ReimbursementRate), optional.
RATE_FIELDS = ["id", "vehicle_id", "vehicle_name", "valid_from", "rate_per_kwh", "created_at"]

# Seit v0.33.0 (Erinnerungen, siehe models.Reminder), beim Import optional.
REMINDER_FIELDS = [
    "id", "vehicle_id", "vehicle_name", "kind", "title", "due_date", "due_km",
    "repeat_months", "repeat_km", "lead_days", "lead_km", "notes",
    "last_done_date", "last_done_km", "created_at",
]

README_TEMPLATE = """Lademonitor Backup
===================

Erstellt am: {timestamp}

Inhalt dieser ZIP-Datei:
- vehicles.csv    - alle Fahrzeuge
- providers.csv   - alle Ladeanbieter
- locations.csv   - alle bekannten Ladeorte
- sessions.csv    - alle Ladevorgaenge
- fees.csv        - Grundgebuehren/Abos der Anbieter (seit v0.27.0)
- costs.csv       - sonstige Fahrzeugkosten (seit v0.32.0)
- reminders.csv   - Erinnerungen (HU, Inspektion, ...; seit v0.33.0)
- reimbursement_rates.csv - Erstattungssaetze Dienstwagen (seit v0.35.0)

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
- Auf einem frischen Server werden die Datensaetze mit ihren Original-IDs
  wiederhergestellt.
- Importiert ein ANDERER Nutzer diese ZIP (auch auf derselben Instanz),
  bekommt er eigene Kopien mit neuen IDs - die Daten der beiden Konten
  bleiben getrennt.
- Bereits vorhandene Datensaetze werden uebersprungen statt ueberschrieben.
  Erkannt werden sie an ihrer ID und zusaetzlich an den Fachdaten
  (Fahrzeug: External ID, Anbieter: Name, Ladeort: Name + Koordinaten,
  Ladevorgang: Fahrzeug + Startzeit, Grundgebuehr: Anbieter + Beginn +
  Rhythmus + Betrag) - der Import ist daher gefahrlos
  mehrfach ausfuehrbar und legt auch dann nichts doppelt an, wenn die ZIP
  von einem anderen Server stammt und alle IDs abweichen.
- Die Reihenfolge (Fahrzeuge vor Anbietern vor Grundgebuehren, Ladeorten und
  Ladevorgaengen, wegen Fremdschluesseln) uebernimmt der Importer automatisch.
- fees.csv, costs.csv, reminders.csv und reimbursement_rates.csv sind
  optional: ZIPs aus Versionen vor 0.27.0, 0.32.0, 0.33.0 bzw. 0.35.0
  enthalten sie nicht und lassen sich trotzdem einlesen.

Nicht enthalten:
- Zugangsdaten. Insbesondere der MyŠkoda-API-Key und das WebDAV-Passwort sind
  bewusst NICHT Teil dieser ZIP - sie muessen nach einer Neuinstallation in
  den Einstellungen neu eingetragen werden. Grund: diese Datei liegt
  typischerweise auf fremdem Speicher (WebDAV/Nextcloud).
- Das MyŠkoda-Debug-Protokoll (separat unter Einstellungen herunterladbar).
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
    fees = (
        db.query(models.ProviderFee)
        .filter(models.ProviderFee.user_id == user.id)
        .order_by(models.ProviderFee.start_date)
        .all()
    )
    costs = (
        db.query(models.VehicleCost)
        .filter(models.VehicleCost.user_id == user.id)
        .order_by(models.VehicleCost.start_date)
        .all()
    )
    reminders = (
        db.query(models.Reminder)
        .filter(models.Reminder.user_id == user.id)
        .order_by(models.Reminder.created_at)
        .all()
    )
    rates = (
        db.query(models.ReimbursementRate)
        .filter(models.ReimbursementRate.user_id == user.id)
        .order_by(models.ReimbursementRate.valid_from)
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
            "ownership": _c(v.ownership),
            "purchase_odometer_km": _c(v.purchase_odometer_km),
            **{f: _c(getattr(v, f)) for f in OWNERSHIP_FLOAT_FIELDS + OWNERSHIP_DATE_FIELDS},
            "reimbursement_mode": _c(v.reimbursement_mode),
            **{f: _c(getattr(v, f)) for f in COMPANY_CAR_BOOL_FIELDS + COMPANY_CAR_STR_FIELDS},
        }
        for v in vehicles
    ]
    provider_rows = [
        {
            "id": _c(p.id), "name": _c(p.name),
            "last_price_ac_per_kwh": _c(p.last_price_ac_per_kwh),
            "last_price_dc_per_kwh": _c(p.last_price_dc_per_kwh),
            "notes": _c(p.notes), "created_at": _c(p.created_at),
            "energy_meter": _c(p.energy_meter),
            "charging_place": _c(p.charging_place),
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
            "outside_temp_c": _c(s.outside_temp_c),
            "outside_temp_source": _c(s.outside_temp_source),
            "energy_meter": _c(s.energy_meter),
        }
        for s in sessions
    ]
    fee_rows = [
        {
            "id": _c(f.id),
            "provider_id": _c(f.provider_id),
            "provider_name": _c(providers_by_id[f.provider_id].name) if f.provider_id in providers_by_id else "",
            "amount": _c(f.amount), "interval": _c(f.interval),
            "start_date": _c(f.start_date), "end_date": _c(f.end_date),
            "label": _c(f.label), "notes": _c(f.notes), "created_at": _c(f.created_at),
        }
        for f in fees
    ]
    cost_rows = [
        {
            "id": _c(c.id),
            "vehicle_id": _c(c.vehicle_id),
            "vehicle_name": _c(vehicles_by_id[c.vehicle_id].name) if c.vehicle_id in vehicles_by_id else "",
            "amount": _c(c.amount), "category": _c(c.category), "interval": _c(c.interval),
            "start_date": _c(c.start_date), "end_date": _c(c.end_date),
            "odometer_km": _c(c.odometer_km),
            "label": _c(c.label), "notes": _c(c.notes), "created_at": _c(c.created_at),
        }
        for c in costs
    ]
    reminder_rows = [
        {
            "id": _c(r.id),
            "vehicle_id": _c(r.vehicle_id),
            "vehicle_name": _c(vehicles_by_id[r.vehicle_id].name) if r.vehicle_id in vehicles_by_id else "",
            "kind": _c(r.kind), "title": _c(r.title),
            "due_date": _c(r.due_date), "due_km": _c(r.due_km),
            "repeat_months": _c(r.repeat_months), "repeat_km": _c(r.repeat_km),
            "lead_days": _c(r.lead_days), "lead_km": _c(r.lead_km), "notes": _c(r.notes),
            "last_done_date": _c(r.last_done_date), "last_done_km": _c(r.last_done_km),
            "created_at": _c(r.created_at),
        }
        for r in reminders
    ]

    rate_rows = [
        {
            "id": _c(r.id),
            "vehicle_id": _c(r.vehicle_id),
            "vehicle_name": _c(vehicles_by_id[r.vehicle_id].name) if r.vehicle_id in vehicles_by_id else "",
            "valid_from": _c(r.valid_from), "rate_per_kwh": _c(r.rate_per_kwh),
            "created_at": _c(r.created_at),
        }
        for r in rates
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README_TEMPLATE.format(timestamp=datetime.utcnow().isoformat()))
        zf.writestr("vehicles.csv", _write_csv(VEHICLE_FIELDS, vehicle_rows))
        zf.writestr("providers.csv", _write_csv(PROVIDER_FIELDS, provider_rows))
        zf.writestr("locations.csv", _write_csv(LOCATION_FIELDS, location_rows))
        zf.writestr("sessions.csv", _write_csv(SESSION_FIELDS, session_rows))
        zf.writestr("fees.csv", _write_csv(FEE_FIELDS, fee_rows))
        zf.writestr("costs.csv", _write_csv(COST_FIELDS, cost_rows))
        zf.writestr("reminders.csv", _write_csv(REMINDER_FIELDS, reminder_rows))
        zf.writestr("reimbursement_rates.csv", _write_csv(RATE_FIELDS, rate_rows))
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
    fees_imported: int = 0
    fees_skipped: int = 0
    costs_imported: int = 0
    costs_skipped: int = 0
    reminders_imported: int = 0
    reminders_skipped: int = 0
    rates_imported: int = 0
    rates_skipped: int = 0


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


def _parse_place(v: str | None) -> str:
    v = (v or "").strip().lower()
    return v if v in ("home", "work", "public") else "public"


def _parse_meter(v: str | None) -> str | None:
    """Messort der kWh; unbekannte Werte werden verworfen statt gespeichert."""
    v = _parse_str(v)
    return v if v in ("charger", "vehicle") else None


def _parse_dt(v: str | None) -> datetime | None:
    v = (v or "").strip()
    return datetime.fromisoformat(v) if v else None


def _read_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _location_key(name: str, latitude: float, longitude: float) -> tuple:
    """Fachlicher Schluessel eines Ladeorts. Der Name allein reicht nicht
    (mehrere "Ionity"), die Koordinaten allein auch nicht - gerundet, weil
    der Umweg ueber CSV die letzten Nachkommastellen veraendern kann."""
    return (name, round(latitude, 5), round(longitude, 5))


def _fee_key(provider_id: str, start_date: datetime, interval, amount: float) -> tuple:
    """Fachlicher Schluessel einer Grundgebuehr: derselbe Anbieter, derselbe
    Beginn, derselbe Rhythmus und Betrag ist dieselbe Gebuehr."""
    return (provider_id, start_date, models.FeeInterval(interval), round(amount, 2))


def _cost_key(vehicle_id: str, start_date: datetime, category: str, interval: str, amount: float) -> tuple:
    """Fachlicher Schluessel eines Kosteneintrags, wie _fee_key()."""
    return (vehicle_id, start_date, category, interval, round(amount, 2))


def _reminder_key(vehicle_id: str, kind: str, title: str | None, due_date, due_km) -> tuple:
    """Fachlicher Schluessel einer Erinnerung: dieselbe Art mit derselben
    Bezeichnung und Faelligkeit am selben Fahrzeug."""
    return (vehicle_id, kind, title or "", due_date, due_km)


def _parse_ownership(v: str | None) -> str:
    v = (v or "").strip()
    return v if v in ("none", "purchase", "leasing") else "none"


def _target_id(old_id: str, owner: str | None, user_id: str) -> str | None:
    """Unter welcher ID ein Datensatz angelegt werden soll - oder None, wenn er
    zu ueberspringen ist.

    Drei Faelle, die frueher alle gleich behandelt wurden (immer ueberspringen)
    und den Import in ein ZWEITES Konto derselben Instanz komplett leer
    ausgehen liessen:

    - ID noch gar nicht vergeben -> Original-ID behalten. Das ist der
      urspruengliche Zweck (Restore auf einen frischen Server) und haelt den
      Import idempotent.
    - ID gehoert bereits DIESEM Nutzer -> echte Dublette, ueberspringen.
    - ID gehoert einem ANDEREN Nutzer -> derselbe Datensatz soll trotzdem
      importiert werden, aber unter einer neuen ID. Die Primaerschluessel sind
      global, die Daten dagegen pro Nutzer getrennt - ohne Neuvergabe waere
      ein Import in ein zweites Konto unmoeglich.
    """
    if owner is None:
        return old_id
    if owner == user_id:
        return None
    return models.gen_uuid()


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
    existing_session_owners = {
        row[0]: row[1] for row in db.query(models.ChargingSession.id, models.ChargingSession.user_id).all()
    }

    # Fachliche Schluessel der EIGENEN Datensaetze. Noetig, weil `external_id`
    # bzw. `name` pro Nutzer eindeutig sein muessen (siehe models.py
    # __table_args__): eine ZIP von einem ANDEREN Server kann dasselbe
    # Fahrzeug mit anderer UUID enthalten - ohne diese Pruefung liefe der
    # Insert in einen IntegrityError (HTTP 500) statt sauber zu ueberspringen.
    own_vehicle_ids_by_external_id = {
        row[0]: row[1]
        for row in db.query(models.Vehicle.external_id, models.Vehicle.id)
        .filter(models.Vehicle.user_id == user.id)
        .all()
    }
    own_provider_ids_by_name = {
        row[0]: row[1]
        for row in db.query(models.Provider.name, models.Provider.id)
        .filter(models.Provider.user_id == user.id)
        .all()
    }
    # Ladeorte und Ladevorgaenge haben keinen DB-seitigen Eindeutigkeits-
    # schluessel, brauchen aber trotzdem einen: sonst legt ein zweiter Import
    # derselben ZIP sie erneut an, sobald die Original-IDs einem anderen Nutzer
    # gehoeren und deshalb neu vergeben werden mussten.
    own_location_ids_by_key = {
        _location_key(row[0], row[1], row[2]): row[3]
        for row in db.query(
            models.ChargingLocation.name,
            models.ChargingLocation.latitude,
            models.ChargingLocation.longitude,
            models.ChargingLocation.id,
        )
        .filter(models.ChargingLocation.user_id == user.id)
        .all()
    }
    own_session_ids_by_key = {
        (row[0], row[1]): row[2]
        for row in db.query(
            models.ChargingSession.vehicle_id,
            models.ChargingSession.start_time,
            models.ChargingSession.id,
        )
        .filter(models.ChargingSession.user_id == user.id)
        .all()
    }

    # Alte ID -> ID unter DIESEM Nutzer. Normalerweise identisch (Original-IDs
    # bleiben erhalten), weicht aber ab, sobald die ID schon einem anderen
    # Nutzer gehoert und deshalb neu vergeben werden musste - dann muessen die
    # Fremdschluessel in locations.csv/sessions.csv mitgezogen werden.
    vehicle_id_map: dict[str, str] = {}
    provider_id_map: dict[str, str] = {}
    location_id_map: dict[str, str] = {}

    vehicles_imported = vehicles_skipped = 0
    for row in _read_csv(zf.read("vehicles.csv")):
        old_id = row["id"]
        # Eigenes Fahrzeug mit derselben external_id, aber anderer UUID (ZIP von
        # einem anderen Server): als dasselbe Fahrzeug behandeln, damit die
        # Ladevorgaenge daran haengen statt verworfen zu werden.
        duplicate_id = own_vehicle_ids_by_external_id.get(row["external_id"])
        new_id = _target_id(old_id, existing_vehicle_owners.get(old_id), user.id)
        if new_id is None or duplicate_id:
            vehicle_id_map[old_id] = duplicate_id or old_id
            vehicles_skipped += 1
            continue
        db.add(
            models.Vehicle(
                id=new_id,
                user_id=user.id,
                external_id=row["external_id"],
                name=row["name"],
                brand=_parse_str(row.get("brand")),
                model=_parse_str(row.get("model")),
                battery_capacity_kwh=_parse_float(row.get("battery_capacity_kwh")),
                is_active=_parse_bool(row.get("is_active")),
                created_at=_parse_dt(row.get("created_at")),
                ownership=_parse_ownership(row.get("ownership")),
                purchase_odometer_km=_parse_int(row.get("purchase_odometer_km")),
                **{f: _parse_float(row.get(f)) for f in OWNERSHIP_FLOAT_FIELDS},
                **{f: _parse_dt(row.get(f)) for f in OWNERSHIP_DATE_FIELDS},
                reimbursement_mode="actual" if row.get("reimbursement_mode") == "actual" else "flat",
                **{f: _parse_bool(row.get(f)) for f in COMPANY_CAR_BOOL_FIELDS},
                **{f: _parse_str(row.get(f)) for f in COMPANY_CAR_STR_FIELDS},
            )
        )
        existing_vehicle_owners[new_id] = user.id
        own_vehicle_ids_by_external_id[row["external_id"]] = new_id
        vehicle_id_map[old_id] = new_id
        vehicles_imported += 1
    db.commit()

    providers_imported = providers_skipped = 0
    for row in _read_csv(zf.read("providers.csv")):
        old_id = row["id"]
        # Wie bei Fahrzeugen: eigener Anbieter gleichen Namens ist derselbe
        # Anbieter, auch wenn die UUID abweicht (name ist pro Nutzer eindeutig).
        duplicate_id = own_provider_ids_by_name.get(row["name"])
        new_id = _target_id(old_id, existing_provider_owners.get(old_id), user.id)
        if new_id is None or duplicate_id:
            provider_id_map[old_id] = duplicate_id or old_id
            providers_skipped += 1
            continue
        db.add(
            models.Provider(
                id=new_id,
                user_id=user.id,
                name=row["name"],
                last_price_ac_per_kwh=_parse_float(row.get("last_price_ac_per_kwh")),
                last_price_dc_per_kwh=_parse_float(row.get("last_price_dc_per_kwh")),
                notes=_parse_str(row.get("notes")),
                created_at=_parse_dt(row.get("created_at")),
                energy_meter=_parse_meter(row.get("energy_meter")) or "charger",
                charging_place=_parse_place(row.get("charging_place")),
            )
        )
        existing_provider_owners[new_id] = user.id
        own_provider_ids_by_name[row["name"]] = new_id
        provider_id_map[old_id] = new_id
        providers_imported += 1
    db.commit()

    locations_imported = locations_skipped = 0
    for row in _read_csv(zf.read("locations.csv")):
        old_id = row["id"]
        duplicate_id = own_location_ids_by_key.get(
            _location_key(row["name"], float(row["latitude"]), float(row["longitude"]))
        )
        new_id = _target_id(old_id, existing_location_owners.get(old_id), user.id)
        if new_id is None or duplicate_id:
            location_id_map[old_id] = duplicate_id or old_id
            locations_skipped += 1
            continue
        default_provider_id = _parse_str(row.get("default_provider_id"))
        if default_provider_id:
            default_provider_id = provider_id_map.get(default_provider_id, default_provider_id)
        if default_provider_id and existing_provider_owners.get(default_provider_id) != user.id:
            default_provider_id = None
        db.add(
            models.ChargingLocation(
                id=new_id,
                user_id=user.id,
                name=row["name"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                radius_m=_parse_int(row.get("radius_m")) or 100,
                default_provider_id=default_provider_id,
                created_at=_parse_dt(row.get("created_at")),
            )
        )
        existing_location_owners[new_id] = user.id
        own_location_ids_by_key[
            _location_key(row["name"], float(row["latitude"]), float(row["longitude"]))
        ] = new_id
        location_id_map[old_id] = new_id
        locations_imported += 1
    db.commit()

    sessions_imported = sessions_skipped = 0
    for row in _read_csv(zf.read("sessions.csv")):
        old_id = row["id"]
        new_id = _target_id(old_id, existing_session_owners.get(old_id), user.id)
        if new_id is None:
            sessions_skipped += 1
            continue
        vehicle_id = _parse_str(row.get("vehicle_id"))
        if vehicle_id:
            vehicle_id = vehicle_id_map.get(vehicle_id, vehicle_id)
        if not vehicle_id or existing_vehicle_owners.get(vehicle_id) != user.id:
            sessions_skipped += 1
            continue

        # Fachlicher Schluessel: dasselbe Fahrzeug kann nicht zweimal zur selben
        # Sekunde zu laden beginnen. Faengt den zweiten Import derselben ZIP ab,
        # auch wenn die IDs inzwischen neu vergeben wurden.
        start_time = _parse_dt(row.get("start_time"))
        if (vehicle_id, start_time) in own_session_ids_by_key:
            sessions_skipped += 1
            continue

        provider_id = _parse_str(row.get("provider_id"))
        if provider_id:
            provider_id = provider_id_map.get(provider_id, provider_id)
        if provider_id and existing_provider_owners.get(provider_id) != user.id:
            provider_id = None
        location_id = _parse_str(row.get("location_id"))
        if location_id:
            location_id = location_id_map.get(location_id, location_id)
        if location_id and existing_location_owners.get(location_id) != user.id:
            location_id = None

        charging_type_raw = _parse_str(row.get("charging_type"))
        charging_type = models.ChargingType(charging_type_raw) if charging_type_raw else None
        source = models.SessionSource(_parse_str(row.get("source")) or "import")
        # Fehlt die Spalte (Backup vor v0.24.0) oder traegt sie einen
        # unbekannten Wert, bleibt die Herkunft NULL statt geraten zu werden -
        # ein falsches "vom Fahrzeug" waere schlimmer als gar keine Angabe.
        temp_source_raw = _parse_str(row.get("outside_temp_source"))
        try:
            temp_source = (
                models.TemperatureSource(temp_source_raw) if temp_source_raw else None
            )
        except ValueError:
            temp_source = None

        db.add(
            models.ChargingSession(
                id=new_id,
                user_id=user.id,
                vehicle_id=vehicle_id,
                provider_id=provider_id,
                location_id=location_id,
                start_time=start_time,
                end_time=_parse_dt(row.get("end_time")),
                charging_type=charging_type,
                soc_start=_parse_int(row.get("soc_start")),
                soc_end=_parse_int(row.get("soc_end")),
                energy_kwh=_parse_float(row.get("energy_kwh")),
                energy_is_estimated=_parse_bool(row.get("energy_is_estimated")),
                odometer_km=_parse_int(row.get("odometer_km")),
                outside_temp_c=_parse_float(row.get("outside_temp_c")),
                outside_temp_source=temp_source,
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
                energy_meter=_parse_meter(row.get("energy_meter")),
            )
        )
        existing_session_owners[new_id] = user.id
        own_session_ids_by_key[(vehicle_id, start_time)] = new_id
        sessions_imported += 1
    db.commit()

    fees_imported = fees_skipped = 0
    if "fees.csv" in names:
        existing_fee_owners = {
            row[0]: row[1] for row in db.query(models.ProviderFee.id, models.ProviderFee.user_id).all()
        }
        own_fee_keys = {
            _fee_key(row[0], row[1], row[2], row[3])
            for row in db.query(
                models.ProviderFee.provider_id,
                models.ProviderFee.start_date,
                models.ProviderFee.interval,
                models.ProviderFee.amount,
            )
            .filter(models.ProviderFee.user_id == user.id)
            .all()
        }
        for row in _read_csv(zf.read("fees.csv")):
            old_id = row["id"]
            new_id = _target_id(old_id, existing_fee_owners.get(old_id), user.id)
            provider_id = _parse_str(row.get("provider_id"))
            if provider_id:
                provider_id = provider_id_map.get(provider_id, provider_id)
            start_date = _parse_dt(row.get("start_date"))
            amount = _parse_float(row.get("amount"))
            try:
                interval = models.FeeInterval(_parse_str(row.get("interval")) or "monthly")
            except ValueError:
                interval = None
            if (
                new_id is None
                or not provider_id
                or existing_provider_owners.get(provider_id) != user.id
                or start_date is None
                or amount is None
                or interval is None
            ):
                fees_skipped += 1
                continue
            key = _fee_key(provider_id, start_date, interval, amount)
            if key in own_fee_keys:
                fees_skipped += 1
                continue
            db.add(
                models.ProviderFee(
                    id=new_id,
                    user_id=user.id,
                    provider_id=provider_id,
                    amount=amount,
                    interval=interval,
                    start_date=start_date,
                    end_date=_parse_dt(row.get("end_date")),
                    label=_parse_str(row.get("label")),
                    notes=_parse_str(row.get("notes")),
                    created_at=_parse_dt(row.get("created_at")),
                )
            )
            existing_fee_owners[new_id] = user.id
            own_fee_keys.add(key)
            fees_imported += 1
        db.commit()

    costs_imported = costs_skipped = 0
    if "costs.csv" in names:
        existing_cost_owners = {
            row[0]: row[1] for row in db.query(models.VehicleCost.id, models.VehicleCost.user_id).all()
        }
        own_cost_keys = {
            _cost_key(c.vehicle_id, c.start_date, c.category, c.interval, c.amount)
            for c in db.query(models.VehicleCost).filter(models.VehicleCost.user_id == user.id).all()
        }
        for row in _read_csv(zf.read("costs.csv")):
            old_id = row["id"]
            new_id = _target_id(old_id, existing_cost_owners.get(old_id), user.id)
            vehicle_id = _parse_str(row.get("vehicle_id"))
            if vehicle_id:
                vehicle_id = vehicle_id_map.get(vehicle_id, vehicle_id)
            start_date = _parse_dt(row.get("start_date"))
            amount = _parse_float(row.get("amount"))
            category = _parse_str(row.get("category")) or "other"
            interval = _parse_str(row.get("interval")) or "once"
            if (
                new_id is None
                or not vehicle_id
                or existing_vehicle_owners.get(vehicle_id) != user.id
                or start_date is None
                or amount is None
                or interval not in ("once", "monthly", "yearly")
            ):
                costs_skipped += 1
                continue
            key = _cost_key(vehicle_id, start_date, category, interval, amount)
            if key in own_cost_keys:
                costs_skipped += 1
                continue
            db.add(
                models.VehicleCost(
                    id=new_id,
                    user_id=user.id,
                    vehicle_id=vehicle_id,
                    amount=amount,
                    category=category,
                    interval=interval,
                    start_date=start_date,
                    end_date=_parse_dt(row.get("end_date")) if interval != "once" else None,
                    odometer_km=_parse_int(row.get("odometer_km")),
                    label=_parse_str(row.get("label")),
                    notes=_parse_str(row.get("notes")),
                    created_at=_parse_dt(row.get("created_at")),
                )
            )
            existing_cost_owners[new_id] = user.id
            own_cost_keys.add(key)
            costs_imported += 1
        db.commit()

    reminders_imported = reminders_skipped = 0
    if "reminders.csv" in names:
        existing_reminder_owners = {
            row[0]: row[1] for row in db.query(models.Reminder.id, models.Reminder.user_id).all()
        }
        own_reminder_keys = {
            _reminder_key(r.vehicle_id, r.kind, r.title, r.due_date, r.due_km)
            for r in db.query(models.Reminder).filter(models.Reminder.user_id == user.id).all()
        }
        for row in _read_csv(zf.read("reminders.csv")):
            old_id = row["id"]
            new_id = _target_id(old_id, existing_reminder_owners.get(old_id), user.id)
            vehicle_id = _parse_str(row.get("vehicle_id"))
            if vehicle_id:
                vehicle_id = vehicle_id_map.get(vehicle_id, vehicle_id)
            kind = _parse_str(row.get("kind")) or "custom"
            if kind not in REMINDER_KINDS:
                kind = "custom"
            title = _parse_str(row.get("title"))
            due_date = _parse_dt(row.get("due_date"))
            due_km = _parse_int(row.get("due_km"))
            if new_id is None or not vehicle_id or existing_vehicle_owners.get(vehicle_id) != user.id:
                reminders_skipped += 1
                continue
            key = _reminder_key(vehicle_id, kind, title, due_date, due_km)
            if key in own_reminder_keys:
                reminders_skipped += 1
                continue
            lead_days = _parse_int(row.get("lead_days"))
            lead_km = _parse_int(row.get("lead_km"))
            db.add(
                models.Reminder(
                    id=new_id,
                    user_id=user.id,
                    vehicle_id=vehicle_id,
                    kind=kind,
                    title=title,
                    due_date=due_date,
                    due_km=due_km,
                    repeat_months=_parse_int(row.get("repeat_months")),
                    repeat_km=_parse_int(row.get("repeat_km")),
                    lead_days=30 if lead_days is None else lead_days,
                    lead_km=1000 if lead_km is None else lead_km,
                    notes=_parse_str(row.get("notes")),
                    last_done_date=_parse_dt(row.get("last_done_date")),
                    last_done_km=_parse_int(row.get("last_done_km")),
                    created_at=_parse_dt(row.get("created_at")),
                )
            )
            existing_reminder_owners[new_id] = user.id
            own_reminder_keys.add(key)
            reminders_imported += 1
        db.commit()

    rates_imported = rates_skipped = 0
    if "reimbursement_rates.csv" in names:
        existing_rate_owners = {
            row[0]: row[1]
            for row in db.query(models.ReimbursementRate.id, models.ReimbursementRate.user_id).all()
        }
        own_rate_keys = {
            (r.vehicle_id, r.valid_from)
            for r in db.query(models.ReimbursementRate)
            .filter(models.ReimbursementRate.user_id == user.id)
            .all()
        }
        for row in _read_csv(zf.read("reimbursement_rates.csv")):
            old_id = row["id"]
            new_id = _target_id(old_id, existing_rate_owners.get(old_id), user.id)
            vehicle_id = _parse_str(row.get("vehicle_id"))
            if vehicle_id:
                vehicle_id = vehicle_id_map.get(vehicle_id, vehicle_id)
            valid_from = _parse_dt(row.get("valid_from"))
            rate = _parse_float(row.get("rate_per_kwh"))
            key = (vehicle_id, valid_from)
            if (
                new_id is None
                or not vehicle_id
                or existing_vehicle_owners.get(vehicle_id) != user.id
                or valid_from is None
                or rate is None
                or key in own_rate_keys
            ):
                rates_skipped += 1
                continue
            db.add(
                models.ReimbursementRate(
                    id=new_id,
                    user_id=user.id,
                    vehicle_id=vehicle_id,
                    valid_from=valid_from,
                    rate_per_kwh=rate,
                    created_at=_parse_dt(row.get("created_at")),
                )
            )
            existing_rate_owners[new_id] = user.id
            own_rate_keys.add(key)
            rates_imported += 1
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
        fees_imported=fees_imported,
        fees_skipped=fees_skipped,
        costs_imported=costs_imported,
        costs_skipped=costs_skipped,
        reminders_imported=reminders_imported,
        reminders_skipped=reminders_skipped,
        rates_imported=rates_imported,
        rates_skipped=rates_skipped,
    )
