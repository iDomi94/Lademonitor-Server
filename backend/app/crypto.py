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

**Bewusst OPT-IN, nicht Pflicht:** wer den Server nur im eigenen Heimnetz
betreibt, braucht den zusaetzlichen Aufwand (Schluessel generieren, sicher
verwahren, bei Verlust sind die Felder futsch) nicht - siehe is_enabled()
weiter unten. Ist FIELD_ENCRYPTION_KEY nicht gesetzt, verhalten sich
EncryptedString/EncryptedFloat wie ganz normale String/Float-Spalten (nur mit
Text als DB-Typ). Erzeugen, falls gewuenscht - reine Python-Standardbibliothek
reicht, ein Fernet-Schluessel ist einfach 32 zufaellige Bytes, urlsafe-
base64-kodiert (kein `pip install cryptography` fuer die Erzeugung selbst
noetig, nur zur Laufzeit hier im Backend):

    python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

Der Schluessel kommt AUSSCHLIESSLICH aus der Umgebungsvariable
FIELD_ENCRYPTION_KEY, NIEMALS aus der DB oder aus /config - genau das waere
sonst wieder gemeinsam mit den verschluesselten Daten im selben Backup, der
Schutz waere wirkungslos.

**Einmal verschluesselt, bleibt verschluesselt:** wurde der Schluessel
irgendwann gesetzt und hat dadurch Bestandsdaten verschluesselt (siehe
database.py-Migration), darf er NICHT mehr entfernt werden - ohne ihn sind
genau diese Werte nicht mehr lesbar. database.py prueft das beim Start
(verschluesselte Werte ohne gesetzten Schluessel -> Start wird verweigert,
statt kaputte/garantiert-falsche Werte still auszuliefern).
"""

import os
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

ENV_VAR = "FIELD_ENCRYPTION_KEY"

# Ein Fernet-Token beginnt immer mit dem Versions-Byte 0x80, das in
# urlsafe-Base64 immer als "gAAAAA" kodiert wird (gilt bis ca. Jahr 2106,
# danach kippt das erste Zeitstempel-Byte) - reicht als Heuristik, um
# verschluesselte von Klartext-Werten zu unterscheiden, OHNE dafuer selbst
# einen Schluessel zu brauchen (wichtig fuer is_encrypted(), siehe unten:
# muss auch funktionieren, wenn FIELD_ENCRYPTION_KEY gerade NICHT gesetzt
# ist, um verwaiste Ciphertexte erkennen zu koennen).
FERNET_PREFIX = "gAAAAA"


@lru_cache(maxsize=1)
def _fernet_or_none() -> Fernet | None:
    key = os.getenv(ENV_VAR)
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{ENV_VAR} ist gesetzt, aber kein gueltiger Fernet-Schluessel (32 "
            "zufaellige Bytes, urlsafe-base64-kodiert). Erzeugen mit: python3 -c "
            "\"import base64, os; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        ) from exc


def is_enabled() -> bool:
    """Ob Feld-Verschluesselung gerade aktiv ist - schlicht ob
    FIELD_ENCRYPTION_KEY gesetzt (und gueltig formatiert) ist. Bewusst
    opt-in, siehe Modul-Docstring."""
    return _fernet_or_none() is not None


def check_configured() -> None:
    """Beim App-Start aufgerufen (main.py): validiert nur das FORMAT eines
    GESETZTEN Schluessels - ein fehlender Schluessel ist kein Fehler
    (Verschluesselung ist opt-in). Ob ein fehlender Schluessel trotz
    bereits verschluesselter Bestandsdaten ein Problem ist, prueft
    database.py (braucht dafuer die DB-Verbindung, siehe Modul-Docstring)."""
    _fernet_or_none()


def encrypt_str(value: str | None) -> str | None:
    if value is None:
        return None
    fernet = _fernet_or_none()
    if fernet is None:
        return value
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(value: str | None) -> str | None:
    if value is None:
        return None
    if not is_encrypted(value):
        # War nie verschluesselt (Verschluesselung war beim Schreiben aus,
        # oder Altbestand vor dieser Version) - unveraendert zurueckgeben.
        return value
    fernet = _fernet_or_none()
    if fernet is None:
        # Sollte database.py's Start-Check eigentlich schon verhindert haben
        # (siehe Modul-Docstring) - hier trotzdem eine klare Fehlermeldung
        # statt eines kryptischen AttributeError.
        raise RuntimeError(
            f"Gespeicherter Wert ist verschluesselt, aber {ENV_VAR} ist nicht "
            "gesetzt - Schluessel wieder eintragen, sonst ist dieser Wert nicht "
            "lesbar."
        )
    return fernet.decrypt(value.encode("ascii")).decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Erkennt bereits verschluesselte Werte - ueber das Praefix, NICHT ueber
    einen Entschluesselungsversuch, damit das auch ohne gesetzten Schluessel
    funktioniert (database.py braucht das z.B., um verwaiste Ciphertexte zu
    erkennen, wenn der Schluessel gerade fehlt)."""
    return value.startswith(FERNET_PREFIX)


class EncryptedString(TypeDecorator):
    """Transparent ver-/entschluesselter Text, sofern FIELD_ENCRYPTION_KEY
    gesetzt ist - sonst ganz normaler Text (nur mit VARCHAR/TEXT als
    DB-Spaltentyp, unabhaengig vom aktuellen Modus, siehe Modul-Docstring).
    Anwendungscode (Router, ORM-Zugriffe, Pydantic-Schemas) sieht so oder so
    weiterhin normale Python-Strings."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        return decrypt_str(value)


class EncryptedFloat(TypeDecorator):
    """Wie EncryptedString, aber mit float als Python-Typ (fuer GPS-
    Koordinaten) - liegt als Text in der DB (verschluesselt oder nicht, siehe
    EncryptedString), da eine numerische Spalte selbst nicht verschluesselt
    gespeichert werden kann."""

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
