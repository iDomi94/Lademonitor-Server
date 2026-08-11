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
