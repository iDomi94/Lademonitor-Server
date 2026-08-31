import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://charging:charging@db:5432/charging"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_light_migrations() -> None:
    """Ergaenzt nachtraeglich hinzugekommene Spalten, da wir bewusst kein
    Alembic o.ae. einsetzen (Ein-Tabellen-Aenderungen sind selten genug,
    dass ADD COLUMN IF NOT EXISTS reicht)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE charging_sessions "
                "ADD COLUMN IF NOT EXISTS geocoded_place VARCHAR"
            )
        )

        # i18n: UI-Sprache pro Nutzer (siehe models.User.language). DEFAULT 'de'
        # deckt sowohl neue Zeilen als auch - via UPDATE - bereits bestehende
        # Nutzer ab, die die Spalte noch nicht hatten (Postgres setzt den
        # DEFAULT bei ADD COLUMN nicht rueckwirkend fuer Bestandszeilen).
        conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR DEFAULT 'de'"
            )
        )
        conn.execute(text("UPDATE users SET language = 'de' WHERE language IS NULL"))

        # Multi-User-Umstellung: user_id auf allen vier Kern-Tabellen ergaenzen.
        # Bestehende Zeilen (aus der Zeit vor Multi-User) werden dem ERSTEN
        # jemals registrierten Nutzer zugeordnet - das ist zuverlaessig der
        # richtige Owner, weil dieser Nutzer beim Registrieren automatisch
        # Admin wird (siehe routers/auth.py) und somit die "alten" Daten
        # ohnehin schon als seine eigenen wahrnimmt. Wirkt sich nur aus,
        # sobald mindestens ein Nutzer existiert - auf einer komplett neuen
        # Installation (noch kein Nutzer registriert) betrifft das UPDATE
        # 0 Zeilen und ist damit ein No-Op.
        for table in ("vehicles", "providers", "charging_locations", "charging_sessions"):
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id VARCHAR")
            )
            conn.execute(
                text(
                    f"UPDATE {table} SET user_id = "
                    "(SELECT id FROM users ORDER BY created_at LIMIT 1) "
                    "WHERE user_id IS NULL"
                )
            )

        # Vehicle.external_id/Provider.name waren vor Multi-User GLOBAL eindeutig
        # (unique=True) - jetzt nur noch pro Nutzer (siehe models.py __table_args__).
        # Alte globale Constraints/Indexe entfernen und durch zusammengesetzte
        # ersetzen. Empirisch mit echtem Postgres verifiziert: SQLAlchemy erzeugt
        # fuer `unique=True, index=True` (Vehicle.external_id) einen eigenstaendigen
        # UNIQUE INDEX (ix_vehicles_external_id), fuer `unique=True` alleine
        # (Provider.name) dagegen einen table-level UNIQUE CONSTRAINT
        # (providers_name_key) - deshalb zwei unterschiedliche DROP-Befehle.
        conn.execute(text("DROP INDEX IF EXISTS ix_vehicles_external_id"))
        conn.execute(text("ALTER TABLE providers DROP CONSTRAINT IF EXISTS providers_name_key"))

        # Postgres kennt kein "ADD CONSTRAINT IF NOT EXISTS" - daher vorher in
        # pg_constraint nachsehen, um bei jedem Neustart idempotent zu bleiben.
        for table, constraint_name, columns in (
            ("vehicles", "uq_vehicles_user_external_id", "user_id, external_id"),
            ("providers", "uq_providers_user_name", "user_id, name"),
        ):
            exists = conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
                {"name": constraint_name},
            ).first()
            if not exists:
                conn.execute(
                    text(
                        f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} "
                        f"UNIQUE ({columns})"
                    )
                )
