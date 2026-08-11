import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class ChargingType(str, enum.Enum):
    AC = "AC"
    DC = "DC"


class SessionSource(str, enum.Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    IMPORT = "import"


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    # Kurzer, stabiler Schluessel z.B. fuer HA-Automations ("enyaq"), getrennt von der DB-ID
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    brand: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    battery_capacity_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sessions: Mapped[list["ChargingSession"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String, unique=True)
    last_price_ac_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_dc_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sessions: Mapped[list["ChargingSession"]] = relationship(back_populates="provider")
    locations: Mapped[list["ChargingLocation"]] = relationship(back_populates="default_provider")


class ChargingLocation(Base):
    __tablename__ = "charging_locations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[int] = mapped_column(Integer, default=100)
    default_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("providers.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    default_provider: Mapped["Provider"] = relationship(back_populates="locations")
    sessions: Mapped[list["ChargingSession"]] = relationship(back_populates="location")


class ChargingSession(Base):
    __tablename__ = "charging_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("charging_locations.id"), nullable=True
    )

    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    charging_type: Mapped[ChargingType | None] = mapped_column(
        Enum(ChargingType), nullable=True
    )

    soc_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    soc_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # energy_kwh: entweder direkt gemessen (manuell/Wallbox) oder aus SoC-Delta berechnet
    energy_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)

    odometer_km: Mapped[int | None] = mapped_column(Integer, nullable=True)

    price_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)

    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Automatisch per Offline-Reverse-Geocoding ermittelter Ortsname, nur gesetzt wenn
    # kein bekannter ChargingLocation-Eintrag zu den Koordinaten passt
    geocoded_place: Mapped[str | None] = mapped_column(String, nullable=True)

    source: Mapped[SessionSource] = mapped_column(Enum(SessionSource), default=SessionSource.MANUAL)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    external_session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    vehicle: Mapped["Vehicle"] = relationship(back_populates="sessions")
    provider: Mapped["Provider"] = relationship(back_populates="sessions")
    location: Mapped["ChargingLocation"] = relationship(back_populates="sessions")
