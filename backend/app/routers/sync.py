from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/deletions", response_model=schemas.DeletionsOut)
def list_deletions(
    since: datetime | None = Query(
        default=None,
        description=(
            "Nur Grabsteine ab diesem Zeitpunkt (der `server_time`-Wert des "
            "vorherigen Aufrufs). Ohne Angabe: alle - das ist der erste Abgleich "
            "eines Geraets."
        ),
    ),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Liefert die seit `since` geloeschten Datensaetze des angemeldeten Nutzers.

    Gegenstueck zu den vier Listen-Endpunkten: die liefern, WAS es gibt, dieser
    liefert ausdruecklich, was es NICHT MEHR gibt. Warum das nicht aus der
    Abwesenheit in einer Liste ableitbar ist, steht in models.DeletedRecord.

    `server_time` wird VOR der Abfrage genommen, nicht danach: wird waehrend der
    laufenden Abfrage geloescht, faellt dieser Grabstein damit in das naechste
    Fenster statt zwischen beide zu fallen. Ein Grabstein doppelt zu liefern ist
    folgenlos (das Loeschen einer bereits fehlenden Zeile ist ein No-Op), ihn zu
    ueberspringen waere eine dauerhafte Geisterzeile.
    """
    server_time = datetime.utcnow()

    q = db.query(models.DeletedRecord).filter(models.DeletedRecord.user_id == user.id)
    if since is not None:
        # Naive Vergleichsbasis: alle Zeitstempel dieser App liegen als naives
        # UTC in der DB (datetime.utcnow()). Ein Client, der einen Zeitzonen-
        # Offset mitschickt, wuerde sonst am Vergleich scheitern.
        if since.tzinfo is not None:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
        q = q.filter(models.DeletedRecord.deleted_at >= since)

    deletions = q.order_by(models.DeletedRecord.deleted_at).all()
    return schemas.DeletionsOut(server_time=server_time, deletions=deletions)
