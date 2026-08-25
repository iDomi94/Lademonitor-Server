"""WebDAV-Auto-Backup: laedt in konfigurierbaren Abstaenden dieselbe ZIP wie
der manuelle Export (`routers/backup.py::build_backup_zip`) per HTTP PUT auf
ein WebDAV-Ziel hoch (z.B. Nextcloud) und raeumt dort wieder auf, was aelter
als die konfigurierte Aufbewahrungsfrist ist.

Bewusst KEIN PROPFIND-Verzeichnislisting fuers Aufraeumen (WebDAV-Server
antworten darauf mit teils sehr unterschiedlichem XML) - stattdessen fuehrt
`WebdavBackupFile` selbst Buch ueber die eigenen Uploads, das reicht fuer
reines Retention-Housekeeping.

Alles hier ist bewusst synchron (sync SQLAlchemy Session, sync httpx.Client)
wie der Rest der App - der Scheduler-Task in main.py ruft `run_due_backups()`
ueber `asyncio.to_thread()` auf, damit ein Backup-Lauf den Event-Loop nicht
blockiert, ohne dass irgendwo async/await mit der sonst komplett synchronen
DB-Schicht gemischt werden muss."""

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal
from .routers.backup import build_backup_zip

logger = logging.getLogger(__name__)

FREQUENCY_INTERVAL = {
    models.WebdavBackupFrequency.DAILY: timedelta(days=1),
    models.WebdavBackupFrequency.WEEKLY: timedelta(days=7),
    models.WebdavBackupFrequency.MONTHLY: timedelta(days=30),
}


def _is_due(config: models.WebdavBackupConfig) -> bool:
    if not config.last_run_at:
        return True
    return datetime.utcnow() - config.last_run_at >= FREQUENCY_INTERVAL[config.frequency]


def _cleanup_old_backups(
    db: Session, client: httpx.Client, base: str, user_id: str, retention_days: int
) -> None:
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    old_files = (
        db.query(models.WebdavBackupFile)
        .filter(models.WebdavBackupFile.user_id == user_id, models.WebdavBackupFile.created_at < cutoff)
        .all()
    )
    for f in old_files:
        try:
            resp = client.delete(f"{base}/{f.filename}")
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Naechster Lauf versucht es erneut - DB-Zeile bleibt bewusst stehen,
            # sonst wuerde eine fehlgeschlagene Loeschung die Datei fuer immer
            # "vergessen" und sie bliebe auf dem WebDAV-Server liegen
            logger.warning("Konnte alte WebDAV-Backup-Datei %s nicht loeschen: %s", f.filename, exc)
            continue
        db.delete(f)
    db.commit()


def run_backup_for_user(
    db: Session, user: models.User, config: models.WebdavBackupConfig
) -> None:
    """Baut die Backup-ZIP, laedt sie hoch, raeumt alte Dateien auf und
    schreibt das Ergebnis (Erfolg oder Fehlertext) in `config` zurueck.
    Wirft bewusst NICHT weiter - Aufrufer (Scheduler-Schleife ueber mehrere
    Nutzer, oder der manuelle "Jetzt sichern"-Endpunkt) lesen den Status aus
    `config.last_status`/`last_error` statt eine Exception behandeln zu muessen."""
    # Mikrosekunden-Praezision statt nur Sekunden - verhindert, dass zwei
    # schnell aufeinanderfolgende Laeufe (z.B. doppelt geklicktes "Jetzt
    # sichern") denselben Dateinamen erzeugen. Ein Namenskollision waere hier
    # nicht nur kosmetisch: die Aufraeum-Logik unten identifiziert alte
    # Dateien ueber genau diesen Dateinamen - bei einer Kollision koennte sie
    # sonst faelschlich die gerade erst hochgeladene Datei loeschen.
    filename = f"lademonitor-backup-{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S-%f')}.zip"
    try:
        content = build_backup_zip(db, user)
        base = config.url.rstrip("/")
        auth = (config.username, config.password) if config.username else None
        with httpx.Client(auth=auth, timeout=60.0, follow_redirects=True) as client:
            try:
                # Bestmoeglicher Versuch, den Zielordner anzulegen (nicht
                # rekursiv - Elternordner muessen bereits existieren). Schlaegt
                # bei den meisten Servern mit 405 fehl, wenn er schon da ist -
                # das ist dann kein Fehler, der folgende PUT zeigt echte
                # Probleme (falscher Pfad/Zugangsdaten) klarer an.
                client.request("MKCOL", base)
            except httpx.HTTPError:
                pass

            resp = client.put(f"{base}/{filename}", content=content)
            resp.raise_for_status()

            db.add(models.WebdavBackupFile(user_id=user.id, filename=filename))
            db.commit()

            _cleanup_old_backups(db, client, base, user.id, config.retention_days)

        config.last_status = "success"
        config.last_error = None
    except Exception as exc:
        logger.warning("WebDAV-Backup fuer Nutzer %s fehlgeschlagen: %s", user.id, exc)
        config.last_status = "error"
        config.last_error = str(exc)[:500]
    finally:
        config.last_run_at = datetime.utcnow()
        db.commit()


def run_due_backups() -> None:
    """Vom Scheduler-Task in main.py periodisch aufgerufen - prueft alle
    aktivierten Konfigurationen und sichert nur die, deren Intervall
    abgelaufen ist. Ein fehlschlagender Nutzer blockiert die anderen nicht."""
    db = SessionLocal()
    try:
        configs = (
            db.query(models.WebdavBackupConfig)
            .filter(models.WebdavBackupConfig.enabled == True)  # noqa: E712
            .all()
        )
        for config in configs:
            if not config.url or not _is_due(config):
                continue
            user = db.get(models.User, config.user_id)
            if not user:
                continue
            try:
                run_backup_for_user(db, user, config)
            except Exception:
                logger.exception("Unerwarteter Fehler im WebDAV-Backup fuer Nutzer %s", config.user_id)
    finally:
        db.close()
