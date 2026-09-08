"""Benachrichtigungen per Mail.

Zwei Ausloeserarten, bewusst getrennt:

* **Ereignisgetrieben** - der Fehler ist gerade passiert, der Code, der ihn
  bemerkt, meldet ihn (`notify_backup_result` aus webdav_backup.py,
  `notify_myskoda_status` aus myskoda_poller.py). Gemeldet wird nur der
  UEBERGANG von "laeuft" nach "kaputt", nicht jeder Fehldurchlauf - sonst
  kommt bei einem dauerhaft falschen Passwort taeglich dieselbe Mail.
* **Zeitgesteuert** - `run_due_notifications()` laeuft im Scheduler und
  entscheidet anhand von Zeitstempeln in der DB, was faellig ist
  (Ablaufwarnung des API-Keys, Sammelmeldung zu pruefender Ladevorgaenge,
  Monatsbericht).

Warum die Merkerspalten (`last_failure_notified_at`,
`api_key_expiry_notified_days`, `last_review_digest_at`,
`last_monthly_report_at`): der Scheduler laeuft alle 15 Minuten. Ohne sie
ginge dieselbe Mail bis zum naechsten Tageswechsel in jedem Durchlauf erneut
raus.

Nichts hier wirft: eine fehlgeschlagene Benachrichtigung darf weder einen
Backup-Lauf noch einen API-Abruf noch den Scheduler abbrechen.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import mailer, models
from .database import SessionLocal
from .i18n import language_context, translate

logger = logging.getLogger(__name__)

# Alle 15 Minuten pruefen reicht: die feinste Faelligkeit hier ist "einmal
# taeglich". Derselbe Takt wie beim WebDAV-Backup.
SCHEDULER_INTERVAL_SECONDS = 15 * 60

# Solange ein Backup kaputt bleibt, hoechstens woechentlich erinnern.
FAILURE_REMINDER = timedelta(days=7)

# Vorwarnstufen fuer den Ablauf des MyŠkoda-API-Keys, absteigend.
KEY_EXPIRY_THRESHOLDS = (14, 7, 1)


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "–"


def _send(db: Session, user: models.User, mail: mailer.Mail, kind: str) -> None:
    mailer.send(db, to_address=user.email, mail=mail, kind=kind,
                language=user.language, user_id=user.id)


# ---------------------------------------------------------------------------
# Ereignisgetrieben
# ---------------------------------------------------------------------------

def notify_backup_result(db: Session, config: models.WebdavBackupConfig, previous_status: str | None) -> None:
    """Nach einem WebDAV-Backup-Lauf aufgerufen.

    Das ist die wertvollste Meldung dieser App: ein naechtliches Backup, das
    seit Wochen scheitert, sieht man sonst nur, wenn man zufaellig die
    Einstellungen aufmacht - und ein Backup, das man faelschlich fuer laufend
    haelt, ist schlimmer als gar keins.
    """
    try:
        user = db.get(models.User, config.user_id)
        if not user or not user.email or not user.notify_backup_failed:
            return

        if config.last_status == "success":
            # Erholt sich der Lauf wieder, wird der Merker geleert, damit ein
            # spaeterer Fehler erneut als Uebergang gilt.
            config.last_failure_notified_at = None
            db.commit()
            return

        now = datetime.utcnow()
        is_transition = previous_status != config.last_status
        is_reminder = (
            config.last_failure_notified_at is not None
            and now - config.last_failure_notified_at >= FAILURE_REMINDER
        )
        if not (is_transition or config.last_failure_notified_at is None or is_reminder):
            return

        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate("email.backup_failed.subject"),
                heading=translate("email.backup_failed.heading"),
                intro=[translate("email.backup_failed.intro")],
                rows=[
                    (translate("email.backup_failed.row_when"), _fmt_dt(config.last_run_at)),
                    (translate("email.backup_failed.row_target"), config.url or "–"),
                    (translate("email.backup_failed.row_error"), config.last_error or "–"),
                ],
                note=translate("email.backup_failed.note"),
            )
        _send(db, user, mail, "backup_failed")
        config.last_failure_notified_at = now
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Backup-Benachrichtigung fehlgeschlagen")


def notify_myskoda_status(db: Session, config: models.MySkodaConfig, previous_status: str | None) -> None:
    """Nach einem MyŠkoda-Abruf aufgerufen.

    Gemeldet wird nur `auth_error` - ein widerrufener oder abgelaufener Key
    heisst, dass die automatische Ladeerkennung ab sofort still steht, und
    zwar dauerhaft, bis jemand einen neuen Key eintraegt. Ein einzelner
    Netzwerkfehler (`error`) heilt dagegen von selbst beim naechsten Abruf,
    und `rate_limited` ist voellig normal - beides waere reines Rauschen.
    """
    try:
        if config.last_status != "auth_error" or previous_status == "auth_error":
            return
        user = db.get(models.User, config.user_id)
        if not user or not user.email or not user.notify_myskoda_error:
            return
        vehicle = db.get(models.Vehicle, config.vehicle_id)
        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate("email.myskoda_error.subject"),
                heading=translate("email.myskoda_error.heading"),
                intro=[translate("email.myskoda_error.intro")],
                rows=[
                    (translate("email.myskoda_error.row_vehicle"), vehicle.name if vehicle else "–"),
                    (translate("email.myskoda_error.row_when"), _fmt_dt(config.last_poll_at)),
                    (translate("email.myskoda_error.row_error"), config.last_error or "–"),
                ],
                note=translate("email.myskoda_error.note"),
            )
        _send(db, user, mail, "myskoda_error")
    except Exception:  # noqa: BLE001
        logger.exception("MyŠkoda-Benachrichtigung fehlgeschlagen")


# ---------------------------------------------------------------------------
# Zeitgesteuert
# ---------------------------------------------------------------------------

def _run_key_expiry_warnings(db: Session) -> None:
    """Der API-Key laeuft nach einiger Zeit ab; danach steht die automatische
    Erfassung. Der Ablauftermin ist bekannt (`api_key_expires_at`), also laesst
    sich vorwarnen, statt es erst an den fehlenden Ladevorgaengen zu merken."""
    now = datetime.utcnow()
    configs = (
        db.query(models.MySkodaConfig)
        .filter(models.MySkodaConfig.api_key_expires_at.isnot(None))
        .all()
    )
    for config in configs:
        days_left = (config.api_key_expires_at - now).days
        # Neuer Key mit spaeterem Ablauf: Merker leeren, damit die Stufen
        # beim naechsten Mal wieder von vorn durchlaufen werden.
        if days_left > KEY_EXPIRY_THRESHOLDS[0]:
            if config.api_key_expiry_notified_days is not None:
                config.api_key_expiry_notified_days = None
                db.commit()
            continue

        threshold = next(
            (
                t for t in KEY_EXPIRY_THRESHOLDS
                if days_left <= t
                and (config.api_key_expiry_notified_days is None or config.api_key_expiry_notified_days > t)
            ),
            None,
        )
        if threshold is None:
            continue

        user = db.get(models.User, config.user_id)
        if not user or not user.email or not user.notify_myskoda_error:
            # Merker trotzdem setzen, sonst laeuft die Schleife jede
            # Viertelstunde erneut durch dieselbe Pruefung.
            config.api_key_expiry_notified_days = threshold
            db.commit()
            continue

        vehicle = db.get(models.Vehicle, config.vehicle_id)
        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate("email.key_expiring.subject"),
                heading=translate("email.key_expiring.heading"),
                intro=[translate("email.key_expiring.intro", days=max(days_left, 0))],
                rows=[
                    (translate("email.key_expiring.row_vehicle"), vehicle.name if vehicle else "–"),
                    (translate("email.key_expiring.row_expires"), _fmt_dt(config.api_key_expires_at)),
                ],
                note=translate("email.key_expiring.note"),
            )
        _send(db, user, mail, "myskoda_key_expiring")
        config.api_key_expiry_notified_days = threshold
        db.commit()


def _digest_due(user: models.User, now: datetime) -> bool:
    if user.review_digest == models.ReviewDigestFrequency.OFF:
        return False
    if user.last_review_digest_at is None:
        return True
    interval = (
        timedelta(days=1)
        if user.review_digest == models.ReviewDigestFrequency.DAILY
        else timedelta(days=7)
    )
    return now - user.last_review_digest_at >= interval


def _run_review_digests(db: Session) -> None:
    """Sammelmeldung ueber automatisch erkannte Ladevorgaenge, die noch
    geprueft werden muessen. Pro Nutzer einstellbar (aus/taeglich/woechentlich),
    weil das je nach Fahrprofil hilfreich oder laestig ist - eine Mail pro
    Vorgang waere es in jedem Fall."""
    now = datetime.utcnow()
    users = (
        db.query(models.User)
        .filter(
            models.User.email.isnot(None),
            models.User.review_digest != models.ReviewDigestFrequency.OFF,
        )
        .all()
    )
    for user in users:
        if not _digest_due(user, now):
            continue

        pending = (
            db.query(models.ChargingSession)
            .filter(
                models.ChargingSession.user_id == user.id,
                models.ChargingSession.needs_review.is_(True),
            )
            .order_by(models.ChargingSession.start_time.desc())
            .all()
        )
        # Nichts zu tun: Zeitstempel trotzdem fortschreiben, sonst prueft der
        # Scheduler das viertelstuendlich neu.
        if not pending:
            user.last_review_digest_at = now
            db.commit()
            continue

        config = mailer.get_config(db)
        rows = []
        for session in pending[:10]:
            vehicle = db.get(models.Vehicle, session.vehicle_id)
            label = session.start_time.strftime("%d.%m.%Y %H:%M")
            value = " · ".join(
                part for part in (
                    vehicle.name if vehicle else None,
                    f"{session.energy_kwh:.1f} kWh" if session.energy_kwh is not None else None,
                    f"{session.soc_start}→{session.soc_end} %"
                    if session.soc_start is not None and session.soc_end is not None else None,
                ) if part
            )
            rows.append((label, value or "–"))

        with language_context(user.language):
            intro = [translate("email.review_digest.intro", count=len(pending))]
            if len(pending) > len(rows):
                intro.append(translate("email.review_digest.truncated", shown=len(rows)))
            mail = mailer.Mail(
                subject=translate("email.review_digest.subject", count=len(pending)),
                heading=translate("email.review_digest.heading"),
                intro=intro,
                button=(
                    mailer.MailButton(
                        translate("email.review_digest.button"),
                        mailer.build_url(config, "sessions"),
                    )
                    if mailer.are_links_available(config) else None
                ),
                rows=rows,
                note=translate("email.review_digest.note"),
            )
        _send(db, user, mail, "review_digest")
        user.last_review_digest_at = now
        db.commit()


def _run_monthly_reports(db: Session) -> None:
    """Kennzahlen des Vormonats. Nutzt denselben Aggregations-Code wie das
    Dashboard (`routers/stats.py::stats_summary`), damit Mail und Web-UI nicht
    unterschiedliche Zahlen zeigen koennen - der Endpunkt wird hier als
    gewoehnliche Funktion aufgerufen, die Depends-Parameter werden explizit
    uebergeben."""
    from .routers.stats import stats_summary  # lokal: vermeidet einen Importzyklus

    now = datetime.utcnow()
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_of_this_month - timedelta(days=1)
    period_start = last_month_end.replace(day=1).date()
    period_end = last_month_end.date()

    users = (
        db.query(models.User)
        .filter(models.User.email.isnot(None), models.User.notify_monthly_report.is_(True))
        .all()
    )
    for user in users:
        # Schon fuer diesen Monat verschickt?
        if user.last_monthly_report_at and user.last_monthly_report_at >= first_of_this_month:
            continue

        summary = stats_summary(
            vehicle_id=None, start_date=period_start, end_date=period_end, db=db, user=user
        )
        if summary.total_sessions == 0:
            user.last_monthly_report_at = now
            db.commit()
            continue

        config = mailer.get_config(db)
        with language_context(user.language):
            month_names = translate("dashboard.month_names").split("|")
            period = f"{month_names[period_start.month - 1]} {period_start.year}"
            rows = [
                (translate("email.monthly.row_sessions"), str(summary.total_sessions)),
                (translate("email.monthly.row_kwh"), f"{summary.total_kwh:.1f} kWh"),
                (translate("email.monthly.row_cost"), f"{summary.total_cost:.2f} €"),
            ]
            if summary.avg_price_per_kwh is not None:
                rows.append((translate("email.monthly.row_price"), f"{summary.avg_price_per_kwh:.3f} €/kWh"))
            if summary.avg_consumption_kwh_per_100km is not None:
                rows.append((
                    translate("email.monthly.row_consumption"),
                    f"{summary.avg_consumption_kwh_per_100km:.1f} kWh/100km",
                ))
            if summary.total_km_driven:
                rows.append((translate("email.monthly.row_km"), f"{summary.total_km_driven} km"))
            if summary.ac_share_pct is not None:
                rows.append((
                    translate("email.monthly.row_acdc"),
                    f"{summary.ac_kwh:.0f} kWh AC / {summary.dc_kwh:.0f} kWh DC",
                ))
            for entry in summary.by_provider[:5]:
                rows.append((entry.provider_name, f"{entry.total_kwh:.1f} kWh · {entry.total_cost:.2f} €"))

            mail = mailer.Mail(
                subject=translate("email.monthly.subject", period=period),
                heading=translate("email.monthly.heading", period=period),
                intro=[translate("email.monthly.intro", period=period)],
                button=(
                    mailer.MailButton(translate("email.monthly.button"), mailer.build_url(config, ""))
                    if mailer.are_links_available(config) else None
                ),
                rows=rows,
            )
        _send(db, user, mail, "monthly_report")
        user.last_monthly_report_at = now
        db.commit()


def run_due_notifications() -> None:
    """Einstiegspunkt des Scheduler-Tasks (siehe main.py). Faengt pro Bereich
    ab, damit ein Fehler in einem die uebrigen nicht mitreisst."""
    db = SessionLocal()
    try:
        if not mailer.is_sending_enabled(mailer.get_config(db)):
            return
        for step in (_run_key_expiry_warnings, _run_review_digests, _run_monthly_reports):
            try:
                step(db)
            except Exception:  # noqa: BLE001
                logger.exception("Benachrichtigungslauf %s fehlgeschlagen", step.__name__)
    finally:
        db.close()
