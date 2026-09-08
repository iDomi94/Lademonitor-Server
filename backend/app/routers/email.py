"""SMTP-Konfiguration, Testmail und Versandprotokoll - alles nur fuer Admins.

Die Konfiguration gilt fuer die GESAMTE Installation, nicht pro Nutzer (siehe
models.SmtpConfig): die Passwort-vergessen-Mail muss verschickt werden koennen,
wenn gerade niemand angemeldet ist.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import mailer, models, schemas
from ..auth import require_admin
from ..database import get_db

router = APIRouter(prefix="/api/email", tags=["email"])


def _to_out(config: models.SmtpConfig) -> schemas.SmtpConfigOut:
    out = schemas.SmtpConfigOut.model_validate(config)
    out.has_password = bool(config.password)
    return out


@router.get("/config", response_model=schemas.SmtpConfigOut)
def get_smtp_config(db: Session = Depends(get_db), _: models.User = Depends(require_admin)):
    return _to_out(mailer.get_config(db))


@router.put("/config", response_model=schemas.SmtpConfigOut)
def put_smtp_config(
    payload: schemas.SmtpConfigIn,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    config = mailer.get_config(db)
    config.enabled = payload.enabled
    config.host = payload.host.strip()
    config.port = payload.port
    config.security = payload.security
    config.username = payload.username or None
    config.from_address = payload.from_address
    config.from_name = payload.from_name.strip() or "Lademonitor"
    config.base_url = payload.base_url

    # Leeres Feld = bestehendes Passwort behalten. Genau wie beim WebDAV-Backup:
    # das Formular bekommt das gespeicherte Passwort nie zu sehen, ein leeres
    # Feld darf es also nicht loeschen. Zum Entfernen dient das Leeren des
    # Nutzernamens (ohne den wird gar nicht angemeldet).
    if payload.password:
        config.password = payload.password
    if not config.username:
        config.password = None

    if config.enabled and not (config.host and config.from_address):
        raise HTTPException(422, "Für den Versand werden Server und Absenderadresse benötigt")

    db.commit()
    db.refresh(config)
    return _to_out(config)


@router.post("/test", response_model=schemas.SmtpConfigOut)
def send_test_mail(
    payload: schemas.SmtpTestRequest,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Verschickt wirklich eine Mail - der einzige ehrliche Verbindungstest.
    Dieselbe Doppelrolle wie "Jetzt sichern" beim WebDAV-Backup.

    Antwortet auch bei Misserfolg mit 200: das Ergebnis steht in
    `last_status`/`last_error` der zurueckgegebenen Konfiguration, und die
    Oberflaeche zeigt es an derselben Stelle wie den Dauerzustand."""
    to_address = payload.to_address or admin.email
    if not to_address:
        raise HTTPException(
            400, "Keine Empfängeradresse - trage eine Adresse am eigenen Konto ein oder gib eine an."
        )
    config = mailer.get_config(db)
    if not config.host or not config.from_address:
        raise HTTPException(422, "Bitte zuerst Server und Absenderadresse speichern")

    mailer.send_test(db, to_address=to_address, language=admin.language, user_id=admin.id)
    db.refresh(config)
    return _to_out(config)


@router.get("/log", response_model=list[schemas.EmailLogOut])
def list_email_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    return (
        db.query(models.EmailLogEntry)
        .order_by(models.EmailLogEntry.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )


@router.delete("/log", status_code=204)
def clear_email_log(db: Session = Depends(get_db), _: models.User = Depends(require_admin)):
    db.query(models.EmailLogEntry).delete()
    db.commit()
