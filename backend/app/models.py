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
    UniqueConstraint,
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


class WebdavBackupFrequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    # Erster jemals registrierter Nutzer wird automatisch Admin (siehe routers/auth.py)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tokens: Mapped[list["AuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthToken(Base):
    """Opake, server-seitig gespeicherte Tokens statt JWT - einfacher zu
    widerrufen (Logout = Zeile loeschen) und ohne Signatur-/Clock-Skew-Themen.
    Laufen bewusst NICHT ab: werden fuer Browser-Cookie-Sessions genauso wie
    fuer die iOS-App und den Home-Assistant-rest_command-Header verwendet -
    letzterer kann nicht interaktiv neu einloggen, ein staendig ablaufendes
    Token wuerde die Automation regelmaessig kaputt machen. Widerruf laeuft
    ausschliesslich ueber Logout bzw. Loeschen des Nutzers durch einen Admin."""
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="tokens")


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_vehicles_user_external_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    # Nullable, da bestehende Zeilen aus der Zeit vor Multi-User per Migration
    # nachtraeglich dem ersten Nutzer zugeordnet werden (siehe database.py)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Kurzer, stabiler Schluessel z.B. fuer HA-Automations ("enyaq"), getrennt von der DB-ID.
    # Eindeutig PRO NUTZER (siehe __table_args__), nicht global - zwei Nutzer
    # koennen also beide ein Fahrzeug "enyaq" haben.
    external_id: Mapped[str] = mapped_column(String, index=True)
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
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_providers_user_name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Eindeutig PRO NUTZER (siehe __table_args__), nicht global
    name: Mapped[str] = mapped_column(String)
    last_price_ac_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_dc_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sessions: Mapped[list["ChargingSession"]] = relationship(back_populates="provider")
    locations: Mapped[list["ChargingLocation"]] = relationship(back_populates="default_provider")


class ChargingLocation(Base):
    __tablename__ = "charging_locations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
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
    # Denormalisiert (waere ueber vehicle.user_id ableitbar) fuer einfache
    # WHERE-Filter in jedem Router, ohne ueberall zu joinen
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
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


class WebdavBackupConfig(Base):
    """Ein Konfigurationssatz pro Nutzer (passend zur Pro-Nutzer-
    Datentrennung im Rest der App - jeder Nutzer sichert nur seine eigenen
    Daten auf sein eigenes WebDAV-Ziel). `password` liegt bewusst im Klartext
    in der DB, genau wie die uebrigen Zugangsdaten dieser App (z.B.
    Auth-Tokens) - kein Secrets-Vault vorhanden, Postgres ist ohnehin nur via
    localhost im selben Container erreichbar."""
    __tablename__ = "webdav_backup_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(String, default="")
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    password: Mapped[str | None] = mapped_column(String, nullable=True)
    frequency: Mapped[WebdavBackupFrequency] = mapped_column(
        Enum(WebdavBackupFrequency), default=WebdavBackupFrequency.DAILY
    )
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WebdavBackupFile(Base):
    """Protokoll der selbst hochgeladenen Backup-Dateien pro Nutzer, damit
    die Aufbewahrungsfrist durchgesetzt werden kann, ohne bei jedem Lauf ein
    WebDAV-PROPFIND-Verzeichnislisting parsen zu muessen (Server-Antworten
    dafuer unterscheiden sich stark) - wir kennen unsere eigenen Uploads
    bereits aus der DB."""
    __tablename__ = "webdav_backup_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
