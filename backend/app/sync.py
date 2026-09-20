"""Grabsteine (Tombstones) fuer geloeschte Datensaetze.

Hintergrund und Begruendung stehen im Docstring von models.DeletedRecord: die
Apps duerfen eine Loeschung nicht aus der Abwesenheit einer Zeile in einer
Pull-Antwort ableiten, brauchen dafuer also ein ausdrueckliches Signal.

Die Gegenstelle ist routers/sync.py (`GET /api/sync/deletions?since=`).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from . import models


def record_deletion(
    db: Session,
    user_id: str | None,
    entity_type: models.SyncEntityType,
    entity_id: str,
) -> models.DeletedRecord:
    """Legt den Grabstein an. Wird VOR dem eigentlichen `db.delete()` gerufen und
    faellt mit ihm in dieselbe Transaktion/denselben Commit - sonst gaebe es den
    Fall "Zeile weg, Grabstein fehlt", in dem die Loeschung bei den Apps nie
    ankaeme (genau der Zustand, den dieser Mechanismus beseitigen soll)."""
    tombstone = models.DeletedRecord(
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        deleted_at=datetime.utcnow(),
    )
    db.add(tombstone)
    return tombstone


def record_deletions(
    db: Session,
    user_id: str | None,
    entity_type: models.SyncEntityType,
    entity_ids: list[str],
) -> None:
    """Sammelvariante fuer kaskadierende Loeschungen (z.B. alle Ladevorgaenge
    eines Fahrzeugs)."""
    for entity_id in entity_ids:
        record_deletion(db, user_id, entity_type, entity_id)
