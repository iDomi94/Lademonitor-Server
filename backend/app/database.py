import hashlib
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import crypto

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


def _column_data_type(conn, table: str, column: str) -> str | None:
    row = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return row[0] if row else None


# Alle Spalten, die crypto.EncryptedFloat/EncryptedString verwenden (siehe
# models.py) - an einer Stelle gepflegt, weil sowohl die Typ-Migration als
# auch der verwaiste-Ciphertext-Check (siehe unten) dieselbe Liste brauchen.
_ENCRYPTABLE_COORDINATE_COLUMNS = (
    ("charging_sessions", "latitude", False),
    ("charging_sessions", "longitude", False),
    ("charging_locations", "latitude", True),
    ("charging_locations", "longitude", True),
)
_ENCRYPTABLE_TEXT_COLUMNS = (
    ("charging_sessions", "notes"),
    ("charging_sessions", "geocoded_place"),
)


def _encrypt_legacy_coordinate_column(conn, table: str, column: str, not_null: bool = False) -> None:
    """Wandelt eine noch unverschluesselte GPS-Spalte (DOUBLE PRECISION aus
    der Zeit vor der Feld-Verschluesselung, siehe crypto.py) in eine
    Text-Spalte um - UNABHAENGIG davon, ob FIELD_ENCRYPTION_KEY gesetzt ist:
    das Modell (models.py, EncryptedFloat) erwartet so oder so eine
    Text-Spalte, ob der Inhalt dabei tatsaechlich verschluesselt wird,
    entscheidet crypto.encrypt_str() selbst (No-Op ohne gesetzten Schluessel).
    Idempotent ueber den Spaltentyp: nach dem ersten Lauf ist die Spalte
    VARCHAR, jeder weitere Aufruf ist dann ein No-Op. Auf einer neuen
    Installation legt create_all() die Spalte direkt als VARCHAR an - dort
    greift dieser Codepfad also gar nicht erst.

    `not_null` haelt die urspruengliche NOT-NULL-Eigenschaft der Spalte
    aufrecht (ChargingLocation.latitude/longitude sind Pflichtfelder) - ohne
    das wuerde die Spalte nach dem Umbau ueber ADD COLUMN/RENAME COLUMN
    nullable, obwohl das Modell (models.py) sie weiterhin als Mapped[float]
    ohne Optional deklariert."""
    if _column_data_type(conn, table, column) not in ("double precision", "real", "numeric"):
        return
    tmp_column = f"{column}_enc_tmp"
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {tmp_column} VARCHAR"))
    rows = conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).all()
    for row_id, value in rows:
        conn.execute(
            text(f"UPDATE {table} SET {tmp_column} = :v WHERE id = :i"),
            {"v": crypto.encrypt_str(str(value)), "i": row_id},
        )
    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {tmp_column} TO {column}"))
    if not_null:
        conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"))


def _encrypt_pending_plaintext(conn, table: str, column: str) -> None:
    """Verschluesselt noch unverschluesselte Bestandswerte einer bereits als
    VARCHAR/TEXT angelegten Spalte. Deckt zwei Faelle ab: notes/geocoded_place
    (waren schon vor der Feld-Verschluesselung VARCHAR/TEXT) UND GPS-Spalten,
    die schon zu VARCHAR umgebaut wurden (siehe _encrypt_legacy_coordinate_column)
    aber noch als Klartext befuellt sind, weil FIELD_ENCRYPTION_KEY beim
    letzten Start noch nicht gesetzt war - erst wenn der Schluessel gesetzt
    wird, gibt es hier etwas zu tun.

    No-Op ohne gesetzten Schluessel (kann nichts verschluesseln), und ueber
    crypto.is_encrypted() idempotent (ein zweiter Durchlauf ueberspringt
    bereits verschluesselte Werte statt sie erneut, und damit falsch, zu
    verschluesseln) - noetig, weil run_light_migrations() bei jedem
    Container-Start laeuft."""
    if not crypto.is_enabled():
        return
    rows = conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).all()
    for row_id, value in rows:
        if crypto.is_encrypted(value):
            continue
        conn.execute(
            text(f"UPDATE {table} SET {column} = :v WHERE id = :i"),
            {"v": crypto.encrypt_str(value), "i": row_id},
        )


def _check_no_orphaned_ciphertext(conn) -> None:
    """Verweigert den Start, wenn eine Spalte bereits verschluesselte Werte
    enthaelt, FIELD_ENCRYPTION_KEY aber gerade NICHT gesetzt ist - sonst
    wuerde jeder Request, der eine betroffene Zeile liest, erst mitten in der
    Antwort mit einem RuntimeError abbrechen (crypto.decrypt_str() wirft dann,
    siehe dort), statt schon beim Start klar zu sagen, woran es liegt.
    Passiert z.B., wenn jemand den Schluessel testweise gesetzt, Daten
    verschluesselt und ihn danach wieder entfernt hat - siehe crypto.py,
    "Einmal verschluesselt, bleibt verschluesselt". Rein String-basiert
    (LIKE 'gAAAAA%'), braucht dafuer bewusst KEINEN Schluessel (siehe
    crypto.is_encrypted())."""
    if crypto.is_enabled():
        return
    columns = [(t, c) for t, c, _ in _ENCRYPTABLE_COORDINATE_COLUMNS] + list(_ENCRYPTABLE_TEXT_COLUMNS)
    for table, column in columns:
        if _column_data_type(conn, table, column) not in ("character varying", "text"):
            continue  # noch nie zu Text migriert - kann keinen Ciphertext enthalten
        found = conn.execute(
            text(f"SELECT 1 FROM {table} WHERE {column} LIKE '{crypto.FERNET_PREFIX}%' LIMIT 1")
        ).first()
        if found:
            raise RuntimeError(
                f"{table}.{column} enthaelt bereits verschluesselte Werte, aber "
                f"{crypto.ENV_VAR} ist nicht (mehr) gesetzt - ohne den Schluessel "
                "sind diese Werte nicht lesbar. Schluessel wieder eintragen, bevor "
                "der Server startet (siehe CLAUDE.md, Abschnitt \"Verschluesselung "
                "personenbezogener Daten\")."
            )


def run_light_migrations() -> None:
    """Ergaenzt nachtraeglich hinzugekommene Spalten, da wir bewusst kein
    Alembic o.ae. einsetzen (Ein-Tabellen-Aenderungen sind selten genug,
    dass ADD COLUMN IF NOT EXISTS reicht)."""
    with engine.begin() as conn:
        # Allererster Check, vor allem anderen: siehe _check_no_orphaned_ciphertext.
        _check_no_orphaned_ciphertext(conn)

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

        # Rueckdatierung des Ladebeginns (siehe myskoda_poller.py, Abschnitt
        # "Rueckdatierung des Ladebeginns"). Bestandszeilen bekommen dieselben
        # Werte wie neue: die Korrektur ist an, das Zeitfenster automatisch.
        # Wie oben bei users.language noetig, weil Postgres den DEFAULT bei
        # ADD COLUMN nicht rueckwirkend auf bestehende Zeilen anwendet.
        conn.execute(
            text(
                "ALTER TABLE myskoda_configs "
                "ADD COLUMN IF NOT EXISTS backdate_session_start BOOLEAN DEFAULT TRUE"
            )
        )
        conn.execute(
            text(
                "UPDATE myskoda_configs SET backdate_session_start = TRUE "
                "WHERE backdate_session_start IS NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE myskoda_configs "
                "ADD COLUMN IF NOT EXISTS backdate_max_gap_minutes INTEGER DEFAULT 0"
            )
        )
        conn.execute(
            text(
                "UPDATE myskoda_configs SET backdate_max_gap_minutes = 0 "
                "WHERE backdate_max_gap_minutes IS NULL"
            )
        )

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


        # ------------------------------------------------------------------
        # E-Mail-Funktionen (SMTP, Passwort-Reset, Benachrichtigungen)
        # ------------------------------------------------------------------

        # Der Postgres-Enum-Typ fuer users.review_digest muss von Hand angelegt
        # werden: `Base.metadata.create_all()` erzeugt Enum-Typen nur beim
        # Anlegen der zugehoerigen Tabelle, und `users` existiert laengst.
        # (Fuer smtp_configs/user_tokens sind es neue Tabellen, dort erledigt
        # create_all den Typ mit.) Postgres kennt kein
        # "CREATE TYPE IF NOT EXISTS", daher der Ausnahme-Block.
        conn.execute(
            text(
                "DO $$ BEGIN "
                "CREATE TYPE reviewdigestfrequency AS ENUM ('OFF', 'DAILY', 'WEEKLY'); "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        )

        # E-Mail-Adresse und Benachrichtigungseinstellungen am Nutzer. Wie bei
        # users.language weiter oben braucht jede Spalte mit DEFAULT ein
        # nachgezogenes UPDATE - Postgres wendet den DEFAULT bei ADD COLUMN
        # nicht rueckwirkend auf bestehende Zeilen an.
        for column, ddl_type, default in (
            ("email", "VARCHAR", None),
            ("email_verified_at", "TIMESTAMP", None),
            ("notify_backup_failed", "BOOLEAN", "TRUE"),
            ("notify_myskoda_error", "BOOLEAN", "TRUE"),
            ("notify_monthly_report", "BOOLEAN", "FALSE"),
            ("notify_new_registration", "BOOLEAN", "TRUE"),
            ("review_digest", "reviewdigestfrequency", "'OFF'"),
            ("last_review_digest_at", "TIMESTAMP", None),
            ("last_monthly_report_at", "TIMESTAMP", None),
        ):
            suffix = f" DEFAULT {default}" if default else ""
            conn.execute(
                text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} {ddl_type}{suffix}")
            )
            if default:
                conn.execute(
                    text(f"UPDATE users SET {column} = {default} WHERE {column} IS NULL")
                )

        # Eine Adresse darf nur einem Konto gehoeren, sonst waere beim
        # Zuruecksetzen des Passworts nicht entscheidbar, welches gemeint ist.
        # Funktionaler Index auf lower(): "Max@example.com" und
        # "max@example.com" sind dieselbe Mailbox. Partiell, damit beliebig
        # viele Konten ganz ohne Adresse moeglich bleiben (NULL ist in einem
        # gewoehnlichen UNIQUE-Index zwar ohnehin mehrfach erlaubt, der
        # WHERE-Zusatz macht die Absicht aber explizit und haelt den Index
        # klein).
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower "
                "ON users (lower(email)) WHERE email IS NOT NULL"
            )
        )

        # Auth-Tokens werden nur noch als SHA-256-Hash gespeichert (siehe
        # models.AuthToken): ein Auth-Token ist eine fertige Anmeldung, im
        # Klartext war die Tabelle also ein Generalschluessel fuer jedes Konto.
        # Reihenfolge wichtig - erst die neue Spalte fuellen, dann die alte
        # entfernen, damit niemand ausgeloggt wird (Browser-Cookie, iOS-App und
        # der Home-Assistant-Header gelten unveraendert weiter).
        conn.execute(
            text("ALTER TABLE auth_tokens ADD COLUMN IF NOT EXISTS token_hash VARCHAR")
        )
        has_plaintext = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'auth_tokens' AND column_name = 'token'"
            )
        ).first()
        if has_plaintext:
            # Bewusst in Python statt per SQL-`sha256()`: dieselbe Funktion,
            # die auth.py beim Nachschlagen benutzt, statt einer zweiten
            # Implementierung, die identisch bleiben muss.
            rows = conn.execute(
                text("SELECT id, token FROM auth_tokens WHERE token_hash IS NULL AND token IS NOT NULL")
            ).all()
            for token_id, token in rows:
                conn.execute(
                    text("UPDATE auth_tokens SET token_hash = :h WHERE id = :i"),
                    {"h": hashlib.sha256(token.encode("utf-8")).hexdigest(), "i": token_id},
                )
            # Zeilen ohne Klartext-Token koennen nicht migriert werden (sollte
            # es nicht geben) - die wuerden den Unique-Index sprengen.
            conn.execute(text("DELETE FROM auth_tokens WHERE token_hash IS NULL"))
            conn.execute(text("ALTER TABLE auth_tokens DROP COLUMN token"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_auth_tokens_token_hash "
                "ON auth_tokens (token_hash)"
            )
        )

        # Merker fuer die Benachrichtigungen (siehe notifications.py) - ohne
        # sie wuerde der Scheduler dieselbe Warnung in jedem Durchlauf erneut
        # verschicken. Kein UPDATE noetig: NULL ist hier der richtige
        # Ausgangswert ("noch nie gemeldet").
        conn.execute(
            text(
                "ALTER TABLE myskoda_configs "
                "ADD COLUMN IF NOT EXISTS api_key_expiry_notified_days INTEGER"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE webdav_backup_configs "
                "ADD COLUMN IF NOT EXISTS last_failure_notified_at TIMESTAMP"
            )
        )

        # ------------------------------------------------------------------
        # Verschluesselung personenbezogener Daten (siehe crypto.py und
        # CLAUDE.md, Abschnitt "Verschluesselung personenbezogener Daten").
        # Opt-in ueber FIELD_ENCRYPTION_KEY - die Typ-Migration (Double
        # Precision -> Text) laeuft trotzdem immer, weil das Modell (siehe
        # models.py) so oder so eine Text-Spalte erwartet; ob der Inhalt
        # dabei verschluesselt wird, entscheiden die Helper-Funktionen selbst
        # anhand von crypto.is_enabled().
        # ------------------------------------------------------------------
        for coord_table, coord_column, coord_not_null in _ENCRYPTABLE_COORDINATE_COLUMNS:
            _encrypt_legacy_coordinate_column(conn, coord_table, coord_column, not_null=coord_not_null)

        for pending_table, pending_column in (
            [(t, c) for t, c, _ in _ENCRYPTABLE_COORDINATE_COLUMNS] + list(_ENCRYPTABLE_TEXT_COLUMNS)
        ):
            _encrypt_pending_plaintext(conn, pending_table, pending_column)
