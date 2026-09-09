"""Feld-Verschluesselung fuer die schaerfsten personenbezogenen Daten dieser
App (GPS-Koordinaten, freie Notizen, automatisch ermittelte Ortsnamen) - siehe
CLAUDE.md, Abschnitt "Verschluesselung personenbezogener Daten" fuer die
vollstaendige Begruendung und die bewusst gezogene Grenze:

Das schuetzt gegen Diebstahl von DB-Dump/Backup/Datentraeger (relevant, sobald
der Server oeffentlich erreichbar ist) - es ist ausdruecklich KEIN
Zero-Knowledge-Schutz. Der Schluessel liegt im Environment des laufenden
Server-Prozesses, nicht beim einzelnen Nutzer; wer den Server-Prozess selbst
kontrolliert, kann ihn technisch auslesen. Echtes Zero-Knowledge braeuchte
clientseitige Verschluesselung und wuerde das Geo-Matching, die
Statistik-Aggregation und die komplette server-gerenderte Web-UI unmoeglich
machen (siehe CLAUDE.md).

Der Schluessel kommt AUSSCHLIESSLICH aus der Umgebungsvariable
FIELD_ENCRYPTION_KEY, NIEMALS aus der DB oder aus /config - genau das waere
sonst wieder gemeinsam mit den verschluesselten Daten im selben Backup, der
Schutz waere wirkungslos. Erzeugen mit:

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Fehlt die Variable oder ist sie ungueltig, startet die App bewusst gar nicht
erst (main.py ruft require_key() vor jedem DB-Zugriff auf) statt still
unverschluesselt weiterzulaufen.
"""

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

ENV_VAR = "FIELD_ENCRYPTION_KEY"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv(ENV_VAR)
    if not key:
        raise RuntimeError(
            f"{ENV_VAR} ist nicht gesetzt - ohne ihn koennen die verschluesselten "
            "Spalten (GPS-Koordinaten, Notizen, automatisch ermittelte Ortsnamen) "
            "weder gelesen noch geschrieben werden. Erzeugen mit: python3 -c "
            "\"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" und als Container-"
            f"Umgebungsvariable {ENV_VAR} setzen - NICHT unter /config ablegen, "
            "der Schluessel muss getrennt von der Datenbank/den Backups "
            "aufbewahrt werden, sonst ist die Verschluesselung wirkungslos. "
            "Schluessel danach sicher verwahren (z.B. Passwort-Manager) - "
            "verloren bedeutet dauerhaft unlesbare verschluesselte Felder."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{ENV_VAR} ist gesetzt, aber kein gueltiger Fernet-Schluessel (32 "
            "zufaellige Bytes, urlsafe-base64-kodiert - siehe generate_key() "
            "oben)."
        ) from exc


def require_key() -> None:
    """Beim App-Start aufgerufen (main.py), um sofort statt erst beim ersten
    Request oder bei der Migration mit einer verstaendlichen Meldung zu
    scheitern."""
    _fernet()


def encrypt_str(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Erkennt bereits verschluesselte Werte fuer die Migration in
    database.py - versucht schlicht zu entschluesseln. Genutzt, um
    run_light_migrations() idempotent zu halten (laeuft bei jedem
    Container-Start): ein zweiter Durchlauf ueberspringt damit Werte, die
    schon verschluesselt sind, statt sie erneut zu verschluesseln."""
    try:
        _fernet().decrypt(value.encode("utf-8"))
        return True
    except (InvalidToken, ValueError):
        return False


class EncryptedString(TypeDecorator):
    """Transparent ver-/entschluesselter Text - Anwendungscode (Router, ORM-
    Zugriffe, Pydantic-Schemas) sieht weiterhin normale Python-Strings, in der
    Datenbank liegt nur Ciphertext."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        return decrypt_str(value)


class EncryptedFloat(TypeDecorator):
    """Wie EncryptedString, aber mit float als Python-Typ (fuer GPS-
    Koordinaten) - liegt als verschluesselter Text in der DB, da eine
    numerische Spalte selbst nicht verschluesselt gespeichert werden kann."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_str(str(float(value)))

    def process_result_value(self, value, dialect):
        decrypted = decrypt_str(value)
        if decrypted is None:
            return None
        return float(decrypted)
