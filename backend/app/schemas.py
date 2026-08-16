from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from .models import ChargingType, SessionSource


# ---------- Auth ----------

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    is_admin: bool
    created_at: datetime


class LoginResponse(BaseModel):
    token: str
    user: UserOut


# ---------- Vehicle ----------

class VehicleBase(BaseModel):
    external_id: str
    name: str
    brand: str | None = None
    model: str | None = None
    battery_capacity_kwh: float | None = None
    is_active: bool = True


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    battery_capacity_kwh: float | None = None
    is_active: bool | None = None


class VehicleOut(VehicleBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


# ---------- Provider ----------

class ProviderBase(BaseModel):
    name: str
    last_price_ac_per_kwh: float | None = None
    last_price_dc_per_kwh: float | None = None
    notes: str | None = None


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: str | None = None
    last_price_ac_per_kwh: float | None = None
    last_price_dc_per_kwh: float | None = None
    notes: str | None = None


class ProviderOut(ProviderBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


# ---------- ChargingLocation ----------

class LocationBase(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_m: int = 100
    default_provider_id: str | None = None


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_m: int | None = None
    default_provider_id: str | None = None


class LocationOut(LocationBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


# ---------- ChargingSession ----------

class SessionBase(BaseModel):
    vehicle_id: str
    provider_id: str | None = None
    location_id: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    charging_type: ChargingType | None = None
    soc_start: int | None = None
    soc_end: int | None = None
    energy_kwh: float | None = None
    energy_is_estimated: bool = False
    odometer_km: int | None = None
    price_total: float | None = None
    price_per_kwh: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    # Freitext-Ortsname: entweder automatisch per Offline-Reverse-Geocoding gesetzt
    # (wenn kein bekannter ChargingLocation-Eintrag matcht) oder manuell eingetragen,
    # falls gar keine Koordinaten vorliegen. Wird nach dem Anlegen nie automatisch
    # ueberschrieben, daher bleibt eine manuelle Eingabe stabil.
    geocoded_place: str | None = None
    notes: str | None = None


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    provider_id: str | None = None
    location_id: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    charging_type: ChargingType | None = None
    soc_start: int | None = None
    soc_end: int | None = None
    energy_kwh: float | None = None
    energy_is_estimated: bool | None = None
    odometer_km: int | None = None
    price_total: float | None = None
    price_per_kwh: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    geocoded_place: str | None = None
    notes: str | None = None
    needs_review: bool | None = None


class SessionOut(SessionBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source: SessionSource
    needs_review: bool
    external_session_id: str | None = None
    consumption_kwh_per_100km: float | None = None
    consumption_method: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------- Home Assistant Push (automatischer Ladevorgang) ----------

class AutoSessionPush(BaseModel):
    """Payload, den Home Assistant nach Ladeende per POST schickt."""
    vehicle_external_id: str
    external_session_id: str
    start_time: datetime
    end_time: datetime | None = None
    charging_type: ChargingType | None = None
    soc_start: int | None = None
    soc_end: int | None = None
    odometer_km: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    energy_kwh: float | None = None

    @field_validator("charging_type", mode="before")
    @classmethod
    def _normalize_charging_type(cls, value: object) -> object:
        """MySkoda-Sensor (sensor.skoda_enyaq_charge_type) liefert kleingeschrieben
        ('ac'/'dc'), das ChargingType-Enum erwartet Großbuchstaben.

        Der Sensor faellt bei Ladeende oft schon auf 'unknown' zurueck, bevor die
        HA-Automation den Push abschickt (Timing, nicht loesbar ohne den Wert beim
        Start zwischenzuspeichern - siehe CLAUDE.md). Ein ungueltiger Wert soll den
        kompletten Push NICHT per 422 ablehnen: lieber ohne Lade-Art annehmen, statt
        den automatischen Import ganz zu verlieren - needs_review faengt das ab."""
        if not isinstance(value, str):
            return value
        upper = value.upper()
        return upper if upper in (t.value for t in ChargingType) else None


# ---------- Statistik ----------

class MonthlyStat(BaseModel):
    month: str  # "2026-08"
    total_cost: float
    total_kwh: float
    session_count: int
    # Km-gewichteter Monatsdurchschnitt (gleiche Fallback-Kette wie pro
    # Ladevorgang, siehe consumption.py) - None wenn in dem Monat kein
    # Vorgang einen berechenbaren Wert hat
    avg_consumption_kwh_per_100km: float | None = None


class ProviderStat(BaseModel):
    provider_name: str
    total_kwh: float
    total_cost: float


class GeocodeResult(BaseModel):
    display_name: str
    latitude: float
    longitude: float


class StatsSummary(BaseModel):
    total_sessions: int
    total_kwh: float
    total_cost: float
    avg_price_per_kwh: float | None
    avg_consumption_kwh_per_100km: float | None
    price_per_100km: float | None = None
    # Ab jetzt kWh-gewichtet statt Anteil an der Vorgangs-ANZAHL (siehe
    # CLAUDE.md) - ac_kwh/dc_kwh zusaetzlich fuer absolute Beschriftung
    ac_share_pct: float | None
    dc_share_pct: float | None
    ac_kwh: float = 0
    dc_kwh: float = 0
    total_km_driven: int | None = None
    by_provider: list[ProviderStat] = []
    monthly: list[MonthlyStat]
