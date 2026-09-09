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

from .crypto import EncryptedFloat, EncryptedString
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


class SmtpSecurity(str, enum.Enum):
    """Wie die Verbindung zum Mailserver abgesichert wird. STARTTLS (Port 587)
    ist bei den meisten Anbietern der Normalfall, SSL/TLS (Port 465) der
    aeltere implizite Weg. NONE existiert nur fuer einen Relay im eigenen
    Netz - ueber das offene Internet gehoert es nicht benutzt."""
    NONE = "none"
    STARTTLS = "starttls"
    SSL = "ssl"


class UserTokenPurpose(str, enum.Enum):
    """Wofuer ein einmaliger, per Mail verschickter Token gilt. Bewusst EINE
    Tabelle mit Verwendungszweck statt drei fast identischer Tabellen -
    Erzeugung, Ablauf, Einloesung und Aufraeumen sind in allen drei Faellen
    derselbe Code."""
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFY = "email_verify"
    INVITE = "invite"


class ReviewDigestFrequency(str, enum.Enum):
    """Sammelmeldung ueber noch zu pruefende Ladevorgaenge - pro Nutzer
    einstellbar, weil das je nach Fahrprofil taeglich sinnvoll oder laestig
    ist."""
    OFF = "off"
    DAILY = "daily"
    WEEKLY = "weekly"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    # Erster jemals registrierter Nutzer wird automatisch Admin (siehe routers/auth.py)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # UI-Sprache ("de"/"en"), manuell in den Einstellungen gewaehlt (siehe
    # routers/auth.py::set_language) - kein Auto-Erkennen ueber Accept-Language
    # als primaerer Mechanismus. Liegt bewusst direkt am User statt in einer
    # eigenen Settings-Tabelle (die es fuer sowas noch nicht gibt) und wird
    # zusaetzlich in ein Cookie gespiegelt (main.py::_resolve_language), damit
    # auch die noch nicht eingeloggten Login-/Registrieren-Seiten die zuletzt
    # gewaehlte Sprache kennen.
    language: Mapped[str] = mapped_column(String, default="de")

    # Optional - ein Konto funktioniert komplett ohne Adresse, nur eben ohne
    # Mailversand und ohne "Passwort vergessen". Eindeutig ueber einen
    # funktionalen Index auf lower(email) (siehe database.py): ohne den
    # koennten zwei Konten dieselbe Adresse tragen und ein Reset waere nicht
    # mehr eindeutig zuzuordnen.
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    # Gesetzt, sobald der Bestaetigungslink angeklickt wurde. Fuer
    # Benachrichtigungen reicht eine unbestaetigte Adresse; zum ZURUECKSETZEN
    # des Passworts nicht - eine vertippte Adresse wuerde sonst einem Fremden
    # einen gueltigen Token fuer dieses Konto in die Hand geben.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Benachrichtigungen, jeweils pro Nutzer abschaltbar. Standardmaessig an -
    # wer eine Adresse hinterlegt, will in aller Regel genau die Meldungen,
    # die sonst nur beim Aufrufen der Einstellungen sichtbar waeren.
    notify_backup_failed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_myskoda_error: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_monthly_report: Mapped[bool] = mapped_column(Boolean, default=False)
    # Nur fuer Admins relevant (offene Registrierung, siehe routers/auth.py)
    notify_new_registration: Mapped[bool] = mapped_column(Boolean, default=True)

    review_digest: Mapped[ReviewDigestFrequency] = mapped_column(
        Enum(ReviewDigestFrequency), default=ReviewDigestFrequency.OFF
    )
    # Merker gegen Doppelversand: der Scheduler laeuft alle 15 Minuten, ohne
    # diese Zeitstempel wuerde derselbe Bericht bis zum naechsten Tageswechsel
    # in jedem Durchlauf erneut rausgehen.
    last_review_digest_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_monthly_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tokens: Mapped[list["AuthToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    onetime_tokens: Mapped[list["UserToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthToken(Base):
    """Opake, server-seitig gespeicherte Tokens statt JWT - einfacher zu
    widerrufen (Logout = Zeile loeschen) und ohne Signatur-/Clock-Skew-Themen.
    Laufen bewusst NICHT ab: werden fuer Browser-Cookie-Sessions genauso wie
    fuer die iOS-App und den Home-Assistant-rest_command-Header verwendet -
    letzterer kann nicht interaktiv neu einloggen, ein staendig ablaufendes
    Token wuerde die Automation regelmaessig kaputt machen. Widerruf laeuft
    ausschliesslich ueber Logout bzw. Loeschen des Nutzers durch einen Admin.

    Gespeichert wird seit 2026-09 nur noch der SHA-256-Hash des Tokens, nicht
    mehr der Token selbst: ein Auth-Token IST eine fertige Anmeldung, wer die
    Tabelle lesen konnte, war damit sofort jeder Nutzer. Bewusst SHA-256 und
    NICHT bcrypt wie beim Passwort - der Lookup geht ueber Gleichheit, ein
    gesalzener, absichtlich langsamer Hash koennte gar nicht nachgeschlagen
    werden. Das ist hier auch nicht noetig: der Token ist mit
    `secrets.token_urlsafe(32)` bereits 256 Bit Zufall, da gibt es kein
    Woerterbuch und nichts zu erraten (anders als bei einem menschlichen
    Passwort, das genau deshalb bcrypt braucht)."""
    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="tokens")


class UserToken(Base):
    """Einmalige, per Mail verschickte Tokens: Passwort zuruecksetzen,
    Adresse bestaetigen, Einladung annehmen (siehe UserTokenPurpose).

    Auch hier nur der Hash, aus demselben Grund wie bei AuthToken - ein
    gueltiger Reset-Token ist so gut wie das Passwort selbst.

    `used_at` statt Loeschen beim Einloesen: ein zweiter Klick auf denselben
    Link soll "Link bereits verwendet" melden koennen statt "ungueltig" -
    das ist der haeufigste Support-Fall ueberhaupt (Mail-Client oeffnet den
    Link beim Vorschauen, Nutzer klickt danach nochmal)."""
    __tablename__ = "user_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[UserTokenPurpose] = mapped_column(Enum(UserTokenPurpose), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Bei EMAIL_VERIFY die zu bestaetigende Adresse: sie steht bis zur
    # Bestaetigung schon am Nutzer, der Token muss aber wissen, WELCHE Adresse
    # er bestaetigt - sonst wuerde ein alter Link eine inzwischen geaenderte
    # Adresse als geprueft markieren.
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    user: Mapped["User"] = relationship(back_populates="onetime_tokens")


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
    # NICHT verschluesselt (siehe crypto.py) - wird exakt per SQL abgefragt
    # (HA-Push in routers/sessions.py, Dublettencheck in routers/vehicles.py)
    # und ist per __table_args__ eindeutig pro Nutzer; Fernet ist nicht
    # deterministisch (Zufalls-IV), zwei Verschluesselungen desselben Werts
    # ergeben unterschiedlichen Ciphertext - ein SQL-Gleichheitsvergleich UND
    # der DB-Unique-Constraint wuerden beide sang- und klanglos aufhoeren zu
    # funktionieren. Ohne Blind-Index (deterministischer Zweit-Hash als
    # eigene Spalte) nicht sinnvoll verschluesselbar - siehe CLAUDE.md.
    external_id: Mapped[str] = mapped_column(String, index=True)
    # Verschluesselt (siehe crypto.py) - reines Anzeigelabel, keine
    # Unique-Constraint und keine SQL-Filterung darauf (im Gegensatz zu
    # external_id oben).
    name: Mapped[str] = mapped_column(EncryptedString)
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
    # NICHT verschluesselt (siehe crypto.py) - eindeutig PRO NUTZER
    # (__table_args__ oben) UND per SQL-Gleichheitsvergleich beim Anlegen auf
    # Dubletten geprueft (routers/providers.py::create_provider). Fernet ist
    # nicht deterministisch, ein Ciphertext-Vergleich wuerde nie treffen und
    # der DB-Unique-Constraint waere wirkungslos - ohne Blind-Index nicht
    # sinnvoll verschluesselbar, siehe CLAUDE.md. Ausserdem eher ein
    # Anbietername ("EnBW") als personenbezogenes Datum.
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
    # Verschluesselt (siehe crypto.py) - der schaerfste Fall personenbezogener
    # Daten dieser App ist ein Ladeort mit dem Namen "Zuhause". Keine
    # Unique-Constraint und keine SQL-Filterung darauf (nur order_by, siehe
    # routers/locations.py - sortiert deshalb seit hier in Python), im
    # Gegensatz zu Vehicle.external_id/Provider.name.
    name: Mapped[str] = mapped_column(EncryptedString)
    # Verschluesselt (siehe crypto.py), aus demselben Grund wie name oben.
    latitude: Mapped[float] = mapped_column(EncryptedFloat)
    longitude: Mapped[float] = mapped_column(EncryptedFloat)
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

    # Verschluesselt (siehe crypto.py) - GPS-Position eines Ladevorgangs ist
    # genauso personenbezogen wie ein gespeicherter Ladeort.
    latitude: Mapped[float | None] = mapped_column(EncryptedFloat, nullable=True)
    longitude: Mapped[float | None] = mapped_column(EncryptedFloat, nullable=True)
    # Automatisch per Offline-Reverse-Geocoding ermittelter Ortsname, nur gesetzt wenn
    # kein bekannter ChargingLocation-Eintrag zu den Koordinaten passt. Ebenfalls
    # verschluesselt - ein Ortsname wie "Leonberg" waere sonst trotz verschluesselter
    # Koordinaten im Klartext lesbar.
    geocoded_place: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    source: Mapped[SessionSource] = mapped_column(Enum(SessionSource), default=SessionSource.MANUAL)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    external_session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    # Verschluesselt (siehe crypto.py) - Freitext, oft mit Ortsangaben aus der
    # MyŠkoda-Rueckdatierung oder eigenen Notizen des Nutzers.
    notes: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

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
    # Verschluesselt (siehe crypto.py) - kein API-Zugriff/keine Unique-
    # Constraint darauf, wird nur an httpx fuer den WebDAV-PUT weitergereicht.
    password: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    frequency: Mapped[WebdavBackupFrequency] = mapped_column(
        Enum(WebdavBackupFrequency), default=WebdavBackupFrequency.DAILY
    )
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Wann zuletzt wegen eines Fehlers eine Mail rausging. Gemeldet wird der
    # Uebergang von "laeuft" nach "kaputt" und danach hoechstens woechentlich
    # eine Erinnerung - taeglich waere bei einem dauerhaft falschen Passwort
    # schlicht Spam, gar nicht mehr zu melden wuerde den Fehler dagegen in
    # Vergessenheit geraten lassen.
    last_failure_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class MySkodaConfig(Base):
    """Konfiguration UND Zustandsspeicher der automatischen Ladeerkennung ueber
    die MyŠkoda Public API - eine Zeile pro Fahrzeug.

    Zweite, von Home Assistant unabhaengige Quelle fuer `source=AUTOMATIC`-
    Ladevorgaenge; der HA-Push auf `/api/sessions/auto` bleibt unveraendert
    daneben bestehen (siehe myskoda_poller.py).

    Der `api_key` liegt bewusst im Klartext in der DB, genau wie die uebrigen
    Zugangsdaten dieser App (Auth-Tokens, WebDAV-Passwort) - kein
    Secrets-Vault vorhanden, Postgres ist ohnehin nur containerlokal
    erreichbar. Er wird allerdings NICHT in die Backup-ZIP exportiert
    (siehe routers/backup.py), weil die ZIP typischerweise auf fremdem
    Speicher (WebDAV/Nextcloud) landet.

    Die `open_*`-Spalten halten den gerade laufenden, noch nicht abgeschlossenen
    Ladevorgang. Sie liegen bewusst in der DB und nicht im Prozessspeicher:
    ein Container-Neustart mitten im Laden ist der Normalfall (Update, Reboot
    des Hosts) und wuerde sonst den halben Vorgang verlieren.
    """
    __tablename__ = "myskoda_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), unique=True, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Verschluesselt (siehe crypto.py) - keine SQL-Filterung/Unique-Constraint
    # darauf, wird nur an httpx fuer den MyŠkoda-API-Aufruf weitergereicht.
    api_key: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    vin: Mapped[str | None] = mapped_column(String, nullable=True)

    # Adaptives Polling: das Kontingent von 20 Anfragen/Stunde pro API-Key
    # reicht nicht fuer durchgaengig enge Abfragen. Im Leerlauf selten, waehrend
    # eines laufenden Ladevorgangs haeufiger - Standard 20/5 Minuten ergibt
    # 3/h im Leerlauf und 12/h beim Laden, beides mit Reserve.
    poll_interval_idle_minutes: Mapped[int] = mapped_column(Integer, default=20)
    poll_interval_active_minutes: Mapped[int] = mapped_column(Integer, default=5)

    # Nachtraegliche Erkennung: wenn das Fahrzeug zwischen zwei Abfragen
    # geschlafen hat, kann ein kompletter Ladevorgang unbemerkt bleiben und sich
    # nur als SoC-Sprung zeigen. Schwelle bewusst nicht zu klein - Rekuperation
    # auf langer Gefaellestrecke hebt den SoC ebenfalls um ein paar Prozent.
    detect_missed_sessions: Mapped[bool] = mapped_column(Boolean, default=True)
    missed_session_min_soc_delta: Mapped[int] = mapped_column(Integer, default=5)

    # Rueckdatierung des Ladebeginns auf den SoC des letzten Abrufs VOR dem
    # Einstecken - der Teil des Ladevorgangs, den das Abfrageintervall sonst
    # verschluckt (bei DC-Schnellladen zweistellige Prozentpunkte). Warum das
    # zulaessig ist und welche zwei Waechter es begrenzen: ausfuehrlich in
    # myskoda_poller.py, Abschnitt "Rueckdatierung des Ladebeginns".
    backdate_session_start: Mapped[bool] = mapped_column(Boolean, default=True)
    # 0 = automatisch, dann das Doppelte des Leerlaufintervalls. Bewusst kein
    # fester Default in Minuten: wer selten abfragt, braucht ein groesseres
    # Fenster, sonst greift die Korrektur nie.
    backdate_max_gap_minutes: Mapped[int] = mapped_column(Integer, default=0)

    # Debug-Protokoll (siehe MySkodaLogEntry). Solange unklar ist, wie die API
    # waehrend eines echten Ladevorgangs tatsaechlich antwortet, ist das der
    # einzige Weg, die Erkennung nachtraeglich zu beurteilen.
    log_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    log_raw_payload: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Zustand des letzten Abrufs (nur lesend fuer die Web-UI) -------------
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_charging_state: Mapped[str | None] = mapped_column(String, nullable=True)
    last_soc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    api_key_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Welche Ablaufwarnung (14/7/1 Tage) schon per Mail rausging - ohne den
    # Merker wuerde der Scheduler die Warnung viertelstuendlich wiederholen.
    # Wird auf NULL zurueckgesetzt, sobald ein neuer API-Key eingetragen wird.
    api_key_expiry_notified_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_resets_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- Laufender, noch nicht abgeschlossener Ladevorgang -------------------
    open_start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    open_soc_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SoC direkt VOR dem ersten "steckt"-Abruf. Nur fuer die Diagnose: die
    # Differenz zu open_soc_start ist genau der Teil des Ladevorgangs, den das
    # Polling-Intervall verschluckt hat (bei DC-Schnellladen der relevante
    # Fehler). Landet als Notiz am angelegten Ladevorgang.
    open_soc_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_soc_last: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_charging_type: Mapped[str | None] = mapped_column(String, nullable=True)
    open_max_power_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_odometer_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_poll_count: Mapped[int] = mapped_column(Integer, default=0)
    open_gap_before_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Beim EINSTECKEN erfasst, nicht beim Ausstecken: da steht das Fahrzeug
    # sicher am Ladepunkt, waehrend es beim Ausstecken oft schon wieder faehrt
    open_in_saved_location: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    vehicle: Mapped["Vehicle"] = relationship()


class MySkodaLogEntry(Base):
    """Debug-Protokoll der MyŠkoda-Abfragen, pro Fahrzeug.

    Existiert, weil das Antwortverhalten der API waehrend eines echten
    Ladevorgangs noch nicht bekannt ist: welche Zustaende in welcher
    Reihenfolge kommen, wie stark `carCapturedTimestamp` nachlaeuft, ob
    `chargeType` beim Ladeende schon wieder auf OFF steht. Die kleinen
    Zusammenfassungsspalten machen die Tabelle in der Web-UI ohne Aufklappen
    lesbar, `payload` haelt optional die komplette Rohantwort.

    Wird pro Fahrzeug auf LOG_MAX_ENTRIES Zeilen begrenzt (siehe
    myskoda_poller.py), damit ein dauerhaft laufender Poller die DB nicht
    unbegrenzt fuellt.
    """
    __tablename__ = "myskoda_log_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    # info | warning | error
    level: Mapped[str] = mapped_column(String, default="info")
    # Maschinenlesbarer Anlass, z.B. poll | session_started | session_finished |
    # session_discarded | missed_session | api_error | rate_limit | test
    event: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)

    charging_state: Mapped[str | None] = mapped_column(String, nullable=True)
    soc_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charge_power_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Komplette Rohantwort als JSON-Text, nur wenn log_raw_payload aktiv ist
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class SmtpConfig(Base):
    """SMTP-Zugang der GESAMTEN Installation - genau eine Zeile, nur von
    Admins aenderbar.

    Bewusst NICHT pro Nutzer wie WebdavBackupConfig: dort sichert jeder seine
    eigenen Daten auf sein eigenes Ziel, hier verschickt der Server Mails im
    Namen der Anwendung. Entscheidend ist der Passwort-vergessen-Fall - in dem
    Moment ist niemand angemeldet, eine am Nutzer haengende Konfiguration
    waere also gar nicht erreichbar.

    `password` liegt im Klartext, weil es gar nicht anders geht: SMTP-AUTH
    uebertraegt das Passwort selbst, aus einem Hash liesse es sich nicht
    zurueckgewinnen (anders als beim Nutzer-Login, wo nur VERGLICHEN wird).
    Dieselbe Lage wie beim WebDAV-Passwort und beim MyŠkoda-API-Key. Der
    wirksame Schutz ist deshalb nicht Verstecken, sondern den Schaden
    begrenzen: In der Web-UI steht der Hinweis, ein App-spezifisches Passwort
    bzw. ein eigenes Absender-Konto zu verwenden. Ausgegeben wird das Passwort
    ueber die API nie (nur `has_password`), und in die Backup-ZIP kommt es
    ebenso wenig wie der MyŠkoda-Key (siehe routers/backup.py)."""
    __tablename__ = "smtp_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    host: Mapped[str] = mapped_column(String, default="")
    port: Mapped[int] = mapped_column(Integer, default=587)
    security: Mapped[SmtpSecurity] = mapped_column(
        Enum(SmtpSecurity), default=SmtpSecurity.STARTTLS
    )
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    # Verschluesselt (siehe crypto.py) - keine SQL-Filterung/Unique-Constraint
    # darauf, wird nur an smtplib fuer SMTP-AUTH weitergereicht.
    password: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    from_address: Mapped[str] = mapped_column(String, default="")
    from_name: Mapped[str] = mapped_column(String, default="Lademonitor")

    # Absolute Basis-Adresse der Installation, z.B. "https://lade.example.de".
    # MUSS konfiguriert werden und wird bewusst NICHT aus dem Request
    # abgeleitet: der Host-Header kommt vom Client, wer den beim
    # Passwort-vergessen-POST faelscht, laesst dem Opfer eine Mail mit einem
    # Link auf seine eigene Domain zustellen (Host-Header-Poisoning). Ausserdem
    # gibt es bei geplanten Mails (naechtliche Backup-Warnung) ueberhaupt
    # keinen Request, aus dem sich etwas ableiten liesse. Ist das Feld leer,
    # sind alle Funktionen mit Link (Reset, Verifikation, Einladung) aus.
    base_url: Mapped[str] = mapped_column(String, default="")

    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailLogEntry(Base):
    """Versandprotokoll, nach dem Muster von MySkodaLogEntry und aus demselben
    Grund: ein fehlgeschlagener Versand ist sonst voellig unsichtbar. "Ich habe
    nie eine Mail bekommen" laesst sich ohne das nicht von "der Server hat nie
    eine geschickt" unterscheiden, ohne in die Container-Logs zu steigen.

    Auf LOG_MAX_ENTRIES Zeilen gedeckelt (siehe mailer.py). Der Mailtext wird
    NICHT mitgeschrieben - er enthaelt bei Reset und Einladung den Token."""
    __tablename__ = "email_log_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    # Nullable: die Passwort-vergessen-Anfrage kann auf ein unbekanntes Konto
    # laufen, und Testmails haengen am Admin, nicht am Empfaenger.
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    to_address: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    # test | password_reset | email_verify | invite | backup_failed |
    # myskoda_error | myskoda_key_expiring | review_digest | monthly_report |
    # new_registration
    kind: Mapped[str] = mapped_column(String, index=True)
    # sent | failed | skipped
    status: Mapped[str] = mapped_column(String, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
