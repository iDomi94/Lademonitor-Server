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
  Monatsbericht, Jahresrueckblick, faellige Erinnerungen).

Warum die Merkerspalten (`last_failure_notified_at`,
`api_key_expiry_notified_days`, `last_review_digest_at`,
`last_monthly_report_at`, `last_yearly_review_year`): der Scheduler laeuft alle 15 Minuten. Ohne sie
ginge dieselbe Mail bis zum naechsten Tageswechsel in jedem Durchlauf erneut
raus.

Nichts hier wirft: eine fehlgeschlagene Benachrichtigung darf weder einen
Backup-Lauf noch einen API-Abruf noch den Scheduler abbrechen.
"""

import logging
from datetime import date, datetime, timedelta

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


def notify_cardata_status(
    db: Session, config: models.BmwCarDataConfig, previous_status: str | None
) -> None:
    """Gegenstueck zu notify_myskoda_status fuer BMW CarData: gemeldet wird nur
    der Uebergang nach `auth_error` - dann ist die Anmeldung abgelaufen oder
    widerrufen, und es kommen keine Ladevorgaenge mehr, bis jemand in den
    Einstellungen neu mit BMW verbindet. Haengt am selben Schalter wie die
    MyŠkoda-Meldung (beides ist "die automatische Erfassung steht still")."""
    try:
        if config.last_status != "auth_error" or previous_status == "auth_error":
            return
        user = db.get(models.User, config.user_id)
        if not user or not user.email or not user.notify_myskoda_error:
            return
        vehicle = db.get(models.Vehicle, config.vehicle_id)
        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate("email.cardata_error.subject"),
                heading=translate("email.cardata_error.heading"),
                intro=[translate("email.cardata_error.intro")],
                rows=[
                    (translate("email.myskoda_error.row_vehicle"), vehicle.name if vehicle else "–"),
                    (translate("email.myskoda_error.row_when"), _fmt_dt(config.last_poll_at)),
                    (translate("email.myskoda_error.row_error"), config.last_error or "–"),
                ],
                note=translate("email.cardata_error.note"),
            )
        _send(db, user, mail, "cardata_error")
    except Exception:  # noqa: BLE001
        logger.exception("CarData-Benachrichtigung fehlgeschlagen")


def notify_vw_data_act_status(
    db: Session, config: models.VwDataActConfig, previous_status: str | None
) -> None:
    """Gegenstueck zu notify_cardata_status fuer das VW-Datenportal. Gemeldet
    wird der Uebergang nach `auth_error` (Passwort, offene Zustimmung bei VW,
    geaenderter Login) und nach `no_request` (keine fortlaufende Datenanfrage
    mehr - sie laeuft nach rund zwoelf Monaten ab). Beides heisst: es kommen
    keine Ladevorgaenge mehr, bis jemand etwas tut. Haengt am selben Schalter
    wie die MyŠkoda-Meldung."""
    try:
        status = config.last_status
        if status not in ("auth_error", "no_request") or previous_status == status:
            return
        user = db.get(models.User, config.user_id)
        if not user or not user.email or not user.notify_myskoda_error:
            return
        vehicle = db.get(models.Vehicle, config.vehicle_id)
        key = "email.vwda_error" if status == "auth_error" else "email.vwda_no_request"
        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate(f"{key}.subject"),
                heading=translate(f"{key}.heading"),
                intro=[translate(f"{key}.intro")],
                rows=[
                    (translate("email.myskoda_error.row_vehicle"), vehicle.name if vehicle else "–"),
                    (translate("email.myskoda_error.row_when"), _fmt_dt(config.last_poll_at)),
                    (translate("email.myskoda_error.row_error"), config.last_error or "–"),
                ],
                note=translate(f"{key}.note"),
            )
        _send(db, user, mail, "vwda_error")
    except Exception:  # noqa: BLE001
        logger.exception("Benachrichtigung zum VW-Datenportal fehlgeschlagen")


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


def _fmt_pct_change(current: float | None, previous: float | None) -> str | None:
    """"+12 %" bzw. "-5 %" gegenueber der Vorperiode; None, wenn es nichts zu
    vergleichen gibt (kein Vorwert oder Vorwert 0)."""
    if current is None or not previous:
        return None
    pct = (current - previous) / previous * 100
    return f"{pct:+.0f} %"


def _compare(value: str, current, previous, previous_key: str) -> str:
    change = _fmt_pct_change(current, previous)
    return f"{value} ({translate(previous_key, change=change)})" if change else value


def _review_rows(review, *, totals: bool) -> list[tuple[str, str]]:
    """Mailzeilen aus einem Rueckblick (review.py). Muss innerhalb eines
    `language_context` laufen. `totals=False` laesst die grossen Zahlen weg -
    der Monatsbericht hat sie schon in seinen eigenen Zeilen.

    Wie auf der Seite: Zeilen ohne Daten fallen weg, statt "–" zu zeigen."""
    month_names = translate("dashboard.month_names").split("|")
    vs = "email.review.vs_year" if review.period == "year" else "email.review.vs_month"
    t, p = review.totals, review.previous
    rows: list[tuple[str, str]] = []

    if totals:
        rows.append((translate("email.monthly.row_sessions"),
                     _compare(str(t.sessions), t.sessions, p and p.sessions, vs)))
        rows.append((translate("email.monthly.row_kwh"),
                     _compare(f"{t.kwh:.1f} kWh", t.kwh, p and p.kwh, vs)))
        rows.append((translate("email.monthly.row_cost"),
                     _compare(f"{t.cost:.2f} €", t.cost, p and p.cost, vs)))
        if t.km > 0:
            rows.append((translate("email.monthly.row_km"),
                         _compare(f"{t.km:.0f} km", t.km, p and p.km, vs)))
        if t.avg_price_per_kwh is not None:
            rows.append((translate("email.monthly.row_price"), _compare(
                f"{t.avg_price_per_kwh:.3f} €/kWh", t.avg_price_per_kwh, p and p.avg_price_per_kwh, vs)))
        if t.avg_consumption_kwh_per_100km is not None:
            rows.append((translate("email.monthly.row_consumption"), _compare(
                f"{t.avg_consumption_kwh_per_100km:.1f} kWh/100km",
                t.avg_consumption_kwh_per_100km, p and p.avg_consumption_kwh_per_100km, vs)))

    if review.savings is not None:
        sv = review.savings
        rows.append((translate("email.review.row_saved_eur"), translate(
            "email.review.saved_eur_value", saved=f"{sv.saved_eur:.2f} €",
            fuel=f"{sv.fuel_cost:.2f} €", fuel_type=translate(f"review.fuel.{sv.fuel_type}"),
        )))
        rows.append((translate("email.review.row_saved_co2"), f"{sv.saved_co2_kg:.0f} kg CO₂"))

    r = review.records

    def session_value(record, text: str) -> str:
        parts = [text, record.start_time.strftime("%d.%m.%Y")]
        if record.place:
            parts.append(record.place)
        return " · ".join(parts)

    if r.cheapest_per_kwh:
        rows.append((translate("review.record.cheapest"),
                     session_value(r.cheapest_per_kwh, f"{r.cheapest_per_kwh.value:.3f} €/kWh")))
    if r.most_expensive_per_kwh:
        rows.append((translate("review.record.most_expensive"),
                     session_value(r.most_expensive_per_kwh, f"{r.most_expensive_per_kwh.value:.3f} €/kWh")))
    if r.biggest_charge:
        rows.append((translate("review.record.biggest_charge"),
                     session_value(r.biggest_charge, f"{r.biggest_charge.value:.1f} kWh")))
    if r.longest_distance:
        rows.append((translate("review.record.longest_distance"),
                     session_value(r.longest_distance, f"{r.longest_distance.value:.0f} km")))
    if r.coldest:
        rows.append((translate("review.record.coldest"),
                     session_value(r.coldest, f"{r.coldest.value:.1f} °C")))
    if r.warmest:
        rows.append((translate("review.record.warmest"),
                     session_value(r.warmest, f"{r.warmest.value:.1f} °C")))
    for label, rank in (("review.record.top_location", r.top_location),
                        ("review.record.top_provider", r.top_provider)):
        if rank:
            rows.append((translate(label), translate(
                "email.review.rank_value", name=rank.name, sessions=rank.sessions, kwh=f"{rank.kwh:.0f}")))
    for label, rec in (("review.record.lowest_month", r.lowest_consumption_month),
                       ("review.record.highest_month", r.highest_consumption_month)):
        if rec:
            rows.append((translate(label), f"{month_names[rec.month - 1]} · {rec.value:.1f} kWh/100km"))
    return rows


def _run_yearly_reviews(db: Session, today: date | None = None) -> None:
    """Jahresrueckblick (review.py) fuer das Vorjahr, einmal ab dem 1. Januar.
    `last_yearly_review_year` haelt das zuletzt behandelte Jahr fest - auch
    dann, wenn mangels Ladevorgaengen nichts verschickt wurde, sonst prueft der
    Scheduler das viertelstuendlich neu."""
    from .review import build_review  # lokal: vermeidet einen Importzyklus

    today = today or date.today()
    target = today.year - 1
    users = (
        db.query(models.User)
        .filter(models.User.email.isnot(None), models.User.notify_yearly_review.is_(True))
        .all()
    )
    for user in users:
        if user.last_yearly_review_year is not None and user.last_yearly_review_year >= target:
            continue
        review = build_review(db, user, "year", target)
        if review.totals.sessions == 0:
            user.last_yearly_review_year = target
            db.commit()
            continue

        config = mailer.get_config(db)
        with language_context(user.language):
            mail = mailer.Mail(
                subject=translate("email.yearly.subject", year=target),
                heading=translate("email.yearly.heading", year=target),
                intro=[translate("email.yearly.intro", year=target)],
                button=(
                    mailer.MailButton(
                        translate("email.yearly.button"),
                        mailer.build_url(config, f"review#period=year&year={target}"),
                    )
                    if mailer.are_links_available(config) else None
                ),
                rows=_review_rows(review, totals=True),
                note=(
                    translate("email.yearly.note_defaults")
                    if review.savings is not None and not review.savings.configured else None
                ),
            )
        _send(db, user, mail, "yearly_review")
        user.last_yearly_review_year = target
        db.commit()


def _run_monthly_reports(db: Session) -> None:
    """Kennzahlen des Vormonats. Nutzt denselben Aggregations-Code wie das
    Dashboard (`routers/stats.py::stats_summary`), damit Mail und Web-UI nicht
    unterschiedliche Zahlen zeigen koennen - der Endpunkt wird hier als
    gewoehnliche Funktion aufgerufen, die Depends-Parameter werden explizit
    uebergeben."""
    from .review import build_review  # lokal: vermeidet einen Importzyklus
    from .routers.stats import stats_summary

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

        # Seit v0.36.0 mit den Inhalten des Monatsrueckblicks (review.py):
        # Vergleich zum Vormonat, Ersparnis, Rekorde.
        review = build_review(db, user, "month", period_start.year, period_start.month)
        previous = review.previous
        config = mailer.get_config(db)
        with language_context(user.language):
            month_names = translate("dashboard.month_names").split("|")
            period = f"{month_names[period_start.month - 1]} {period_start.year}"
            vs = "email.review.vs_month"
            rows = [
                (translate("email.monthly.row_sessions"), _compare(
                    str(summary.total_sessions), review.totals.sessions, previous and previous.sessions, vs)),
                (translate("email.monthly.row_kwh"), _compare(
                    f"{summary.total_kwh:.1f} kWh", review.totals.kwh, previous and previous.kwh, vs)),
                (translate("email.monthly.row_cost"), _compare(
                    f"{summary.total_cost:.2f} €", review.totals.cost, previous and previous.cost, vs)),
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
            costs = summary.vehicle_costs
            if costs is not None and (costs.other_cost or costs.ownership_cost):
                rows.append((
                    translate("email.monthly.row_vehicle_costs"),
                    f"{costs.other_cost + costs.ownership_cost:.2f} € · "
                    + translate("email.monthly.vehicle_costs_total", total=f"{costs.total_cost:.2f} €"),
                ))
            for entry in summary.by_provider[:5]:
                rows.append((entry.provider_name, f"{entry.total_kwh:.1f} kWh · {entry.total_cost:.2f} €"))
            rows.extend(_review_rows(review, totals=False))

            mail = mailer.Mail(
                subject=translate("email.monthly.subject", period=period),
                heading=translate("email.monthly.heading", period=period),
                intro=[translate("email.monthly.intro", period=period)],
                button=(
                    mailer.MailButton(
                        translate("email.monthly.button"),
                        mailer.build_url(
                            config,
                            f"review#period=month&year={period_start.year}&month={period_start.month}",
                        ),
                    )
                    if mailer.are_links_available(config) else None
                ),
                rows=rows,
            )
        _send(db, user, mail, "monthly_report")
        user.last_monthly_report_at = now
        db.commit()


def _run_reminders(db: Session) -> None:
    """Erinnerungen (reminders.py), die in den Vorlauf kommen oder
    ueberfaellig werden. Je Stufe genau eine Mail: `notified_stage` haelt die
    zuletzt gemeldete fest und wird bei jeder Aenderung der Faelligkeit
    geleert (routers/reminders.py). Mehrere Erinnerungen eines Nutzers gehen
    in EINER Mail raus."""
    from datetime import date

    from .reminders import STATUS_RANK
    from .routers.reminders import list_with_state  # lokal: vermeidet einen Importzyklus

    users = (
        db.query(models.User)
        .filter(models.User.email.isnot(None), models.User.notify_reminders.is_(True))
        .all()
    )
    today = date.today()
    for user in users:
        states = list_with_state(db, user, today=today)
        if not states:
            continue
        rows_by_id = {r.id: r for r in db.query(models.Reminder).filter(models.Reminder.user_id == user.id)}
        vehicles = {v.id: v.name for v in db.query(models.Vehicle).filter(models.Vehicle.user_id == user.id)}
        fresh = []
        for state in states:
            row = rows_by_id.get(state.id)
            if row is None:
                continue
            notified = STATUS_RANK.get(row.notified_stage or "ok", 0)
            if STATUS_RANK[state.status] > notified:
                fresh.append((state, row))
        if not fresh:
            continue

        config = mailer.get_config(db)
        with language_context(user.language):
            rows = []
            for state, row in fresh:
                title = row.title or translate(f"reminder.kind.{row.kind}")
                if len(vehicles) > 1:
                    title = f"{title} · {vehicles.get(row.vehicle_id, '')}"
                parts = [translate(f"reminder.status.{state.status}")]
                if row.due_date:
                    parts.append(row.due_date.strftime("%d.%m.%Y"))
                if row.due_km is not None:
                    parts.append(f"{row.due_km} km")
                if state.km_forecast_date and state.status != "overdue":
                    parts.append(translate(
                        "email.reminders.forecast", date=state.km_forecast_date.strftime("%d.%m.%Y")
                    ))
                rows.append((title, " · ".join(parts)))
            mail = mailer.Mail(
                subject=translate("email.reminders.subject", count=len(fresh)),
                heading=translate("email.reminders.heading"),
                intro=[translate("email.reminders.intro")],
                button=(
                    mailer.MailButton(translate("email.reminders.button"), mailer.build_url(config, "vehicle"))
                    if mailer.are_links_available(config) else None
                ),
                rows=rows,
            )
        _send(db, user, mail, "reminders")
        for state, row in fresh:
            row.notified_stage = state.status
        db.commit()


def _run_reimbursement_reports(db: Session) -> None:
    """Monatsmail mit dem Erstattungsbericht des Vormonats als CSV-Anhang
    (reimbursement.py), je Dienstwagen mit eingeschalteter Monatsmail.
    `Vehicle.reimbursement_last_mailed` haelt den zuletzt verschickten Monat
    fest - wie beim Monatsbericht auch dann, wenn der Versand scheitert, sonst
    ginge der Versuch viertelstuendlich erneut raus."""
    from datetime import date

    from .reimbursement import build_report, to_csv
    from .routers.reimbursement import previous_month

    start, end = previous_month(date.today())
    key = start.strftime("%Y-%m")
    vehicles = (
        db.query(models.Vehicle)
        .filter(
            models.Vehicle.is_company_car.is_(True),
            models.Vehicle.reimbursement_monthly_mail.is_(True),
        )
        .all()
    )
    for vehicle in vehicles:
        if vehicle.reimbursement_last_mailed == key:
            continue
        user = db.get(models.User, vehicle.user_id) if vehicle.user_id else None
        if not user or not user.email:
            continue
        report = build_report(db, user, vehicle, start, end)
        with language_context(user.language):
            body = to_csv(report, user.language or "de")
            month_label = start.strftime("%m/%Y")
            mail = mailer.Mail(
                subject=translate("email.reimbursement.subject", month=month_label, vehicle=vehicle.name),
                heading=translate("email.reimbursement.heading", month=month_label),
                intro=[translate("email.reimbursement.intro")],
                rows=[
                    (translate("email.reimbursement.row_sessions"), str(len(report.rows))),
                    (translate("email.reimbursement.row_kwh"), f"{report.total_kwh:.2f} kWh"),
                    (translate("email.reimbursement.row_amount"), f"{report.total_amount:.2f} €"),
                ],
                note=(
                    translate("email.reimbursement.estimated_note", count=report.estimated_sessions)
                    if report.estimated_sessions else None
                ),
            )
        filename = f"lademonitor-erstattung-{key}.csv"
        mailer.send(
            db, to_address=user.email, mail=mail, kind="reimbursement_report",
            language=user.language, user_id=user.id,
            attachments=[(filename, body.encode("utf-8"), "text/csv")],
        )
        vehicle.reimbursement_last_mailed = key
        db.commit()


def run_due_notifications() -> None:
    """Einstiegspunkt des Scheduler-Tasks (siehe main.py). Faengt pro Bereich
    ab, damit ein Fehler in einem die uebrigen nicht mitreisst."""
    db = SessionLocal()
    try:
        if not mailer.is_sending_enabled(mailer.get_config(db)):
            return
        for step in (
            _run_key_expiry_warnings, _run_review_digests, _run_monthly_reports, _run_yearly_reviews,
            _run_reminders, _run_reimbursement_reports,
        ):
            try:
                step(db)
            except Exception:  # noqa: BLE001
                logger.exception("Benachrichtigungslauf %s fehlgeschlagen", step.__name__)
    finally:
        db.close()
