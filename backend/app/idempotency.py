"""Schutz vor Dubletten, wenn ein Client eine Anlage-Anfrage wiederholt.

Die Apps speichern im Server-Modus zuerst lokal und laden beim naechsten Sync
hoch. Kommt ein POST beim Server an, die Antwort aber nicht mehr beim Geraet
(Funkloch, Timeout), haelt die App die Zeile weiter fuer "noch nicht
hochgeladen" und schickt sie erneut - ohne Schutz entstuende sie doppelt.

Die Apps schicken deshalb ihre lokale ID als Header `Idempotency-Key` mit.
Der Endpunkt fragt vor dem Anlegen `existing()`: gibt es zu diesem Schluessel
schon eine Zeile desselben Typs und Nutzers, kommt sie unveraendert zurueck
(gleiche Antwort wie beim ersten Mal). Sonst wird angelegt und der Schluessel
mit `remember()` im SELBEN Commit vermerkt - sonst gaebe es den Zustand "Zeile
da, Schluessel fehlt", also genau die Luecke, die das hier schliessen soll.

Ohne Header verhaelt sich jeder Endpunkt wie bisher (Web-UI, Home Assistant,
aeltere Apps). Eine aeltere Server-Version ignoriert den Header einfach - der
Schutz greift dann nicht, kaputt geht aber nichts. Deshalb keine
Versionssperre in den Apps.

Ist die Zeile inzwischen geloescht, wird neu angelegt: ein Client, der sie
noch nicht kennt, hat sie lokal ja noch.
"""

from fastapi import Header
from sqlalchemy.orm import Session

from . import models

HEADER = "Idempotency-Key"
MAX_LENGTH = 200


def key_header(
    idempotency_key: str | None = Header(default=None, alias=HEADER, max_length=MAX_LENGTH),
) -> str | None:
    """FastAPI-Dependency: der Header, leer wie fehlend behandelt."""
    key = (idempotency_key or "").strip()
    return key or None


def existing(db: Session, user: models.User, key: str | None, model):
    """Die zu diesem Schluessel schon angelegte Zeile, sonst None."""
    if not key:
        return None
    row = (
        db.query(models.IdempotencyKey)
        .filter(
            models.IdempotencyKey.user_id == user.id,
            models.IdempotencyKey.entity_type == model.__tablename__,
            models.IdempotencyKey.key == key,
        )
        .first()
    )
    if row is None:
        return None
    obj = db.get(model, row.entity_id)
    if obj is None or getattr(obj, "user_id", None) != user.id:
        # Geloescht (oder nie zugaenglich): Merker weg, neu anlegen lassen.
        db.delete(row)
        db.flush()
        return None
    return obj


def remember(db: Session, user: models.User, key: str | None, obj) -> None:
    """Vermerkt den Schluessel fuer `obj`; vor dem Commit aufrufen."""
    if not key:
        return
    db.flush()  # vergibt obj.id, falls noch nicht geschehen
    db.add(
        models.IdempotencyKey(
            user_id=user.id,
            entity_type=type(obj).__tablename__,
            key=key,
            entity_id=obj.id,
        )
    )
