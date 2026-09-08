"""Mailversand: SMTP-Zugang, Rendern der Vorlagen, Versandprotokoll.

Bewusst die Standardbibliothek (`smtplib` + `email.message.EmailMessage`) statt
eines Pakets - keine neue Abhaengigkeit fuer etwas, das Python seit jeher
mitbringt.

Alles hier ist synchron wie der Rest der App (sync SQLAlchemy, sync httpx).
Aufrufe aus Hintergrund-Tasks laufen ueber `asyncio.to_thread()`, damit ein
haengender Mailserver den Event-Loop nicht blockiert - dieselbe Bauweise wie
beim WebDAV-Backup und beim MyŠkoda-Poller.

Warum EINE Vorlage fuer alle Mails: siehe templates/emails/layout.html.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from . import models
from .i18n import language_context, translate

logger = logging.getLogger(__name__)

# Wie beim MyŠkoda-Protokoll gedeckelt, damit eine dauerhaft laufende
# Installation die Tabelle nicht unbegrenzt fuellt.
LOG_MAX_ENTRIES = 200

# Ein haengender Mailserver darf einen Request nicht ewig festhalten. 20 s sind
# grosszuegig fuer SMTP im Heimnetz wie fuer einen oeffentlichen Anbieter.
SMTP_TIMEOUT_SECONDS = 20

# Eigene Jinja-Umgebung statt der aus main.py: die wird dort erst beim Start der
# App gebaut, und ein Import aus main.py waere zirkulaer. Pfad relativ zu dieser
# Datei, nicht zum Arbeitsverzeichnis - der Scheduler laeuft im selben Prozess,
# aber Hintergrund-Code sollte nicht von cwd abhaengen.
_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    # Autoescaping NUR fuer die HTML-Fassung. Global eingeschaltet landen in
    # der Textfassung HTML-Entities: aus Anfuehrungszeichen wird &#34;, aus
    # einem Ampersand &amp; - in einer reinen Textmail schlicht kaputt.
    autoescape=select_autoescape(enabled_extensions=("html",), default=False),
)
_env.globals["t"] = translate


@dataclass
class MailButton:
    label: str
    url: str


@dataclass
class Mail:
    """Inhaltsmodell einer Mail - unabhaengig von HTML/Text."""
    subject: str
    heading: str
    intro: list[str] = field(default_factory=list)
    button: MailButton | None = None
    rows: list[tuple[str, str]] = field(default_factory=list)
    note: str | None = None


def get_config(db: Session) -> models.SmtpConfig:
    """Genau eine Zeile fuer die ganze Installation - siehe models.SmtpConfig.
    Wird beim ersten Zugriff leer angelegt, damit die Einstellungsseite nicht
    zwischen "noch nie gespeichert" und "gespeichert" unterscheiden muss."""
    config = db.query(models.SmtpConfig).first()
    if config is None:
        config = models.SmtpConfig()
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def is_sending_enabled(config: models.SmtpConfig | None) -> bool:
    return bool(config and config.enabled and config.host and config.from_address)


def are_links_available(config: models.SmtpConfig | None) -> bool:
    """Passwort-Reset, Adressbestaetigung und Einladung brauchen einen
    absoluten Link - ohne konfigurierte Basis-Adresse gibt es keinen (und aus
    dem Request darf sie nicht kommen, siehe models.SmtpConfig.base_url)."""
    return is_sending_enabled(config) and bool(config and config.base_url)


def build_url(config: models.SmtpConfig, path: str, **params: str) -> str:
    """Absolute Adresse fuer einen Link in einer Mail. `path` ist derselbe
    relative Pfad, den auch die Web-UI benutzt (z.B. "reset-password")."""
    base = config.base_url.rstrip("/")
    query = f"?{urlencode(params, quote_via=quote)}" if params else ""
    return f"{base}/{path.lstrip('/')}{query}"


def render(mail: Mail, language: str | None) -> tuple[str, str]:
    """(HTML, Text) in der Sprache des Empfaengers."""
    with language_context(language):
        html = _env.get_template("emails/layout.html").render(mail=mail)
        text = _env.get_template("emails/layout.txt").render(mail=mail)
    return html, text


def _log(
    db: Session,
    *,
    to_address: str,
    subject: str,
    kind: str,
    status: str,
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    db.add(
        models.EmailLogEntry(
            user_id=user_id,
            to_address=to_address,
            subject=subject,
            kind=kind,
            status=status,
            error=error[:2000] if error else None,
        )
    )
    db.commit()

    # Aelteste Zeilen ueber der Obergrenze entfernen.
    total = db.query(models.EmailLogEntry).count()
    if total > LOG_MAX_ENTRIES:
        stale = (
            db.query(models.EmailLogEntry.id)
            .order_by(models.EmailLogEntry.created_at.desc())
            .offset(LOG_MAX_ENTRIES)
            .all()
        )
        db.query(models.EmailLogEntry).filter(
            models.EmailLogEntry.id.in_([row.id for row in stale])
        ).delete(synchronize_session=False)
        db.commit()


def _deliver(config: models.SmtpConfig, message: EmailMessage) -> None:
    """Reiner SMTP-Teil, wirft bei Fehlern weiter."""
    context = ssl.create_default_context()
    if config.security == models.SmtpSecurity.SSL:
        server = smtplib.SMTP_SSL(
            config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    else:
        server = smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS)
    try:
        server.ehlo()
        if config.security == models.SmtpSecurity.STARTTLS:
            server.starttls(context=context)
            server.ehlo()
        # Ohne Nutzernamen kein AUTH - ein Relay im eigenen Netz verlangt oft
        # keine Anmeldung und antwortet auf AUTH mit einem Fehler.
        if config.username:
            server.login(config.username, config.password or "")
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - beim Aufraeumen egal
            pass


def send(
    db: Session,
    *,
    to_address: str | None,
    mail: Mail,
    kind: str,
    language: str | None = None,
    user_id: str | None = None,
    log_skipped: bool = False,
) -> bool:
    """Verschickt eine Mail und protokolliert das Ergebnis.

    Wirft NIE - ein fehlgeschlagener Versand darf weder einen Request noch
    einen Scheduler-Durchlauf abbrechen (dieselbe Linie wie
    `myskoda_poller.poll_vehicle()`). Das Ergebnis steht im Protokoll.

    `log_skipped` nur fuer vom Nutzer ausgeloeste Vorgaenge: dass eine
    automatische Benachrichtigung mangels Adresse unterbleibt, ist der
    Normalfall und wuerde das Protokoll fluten. Dass eine angeforderte
    Reset-Mail nicht rausging, ist dagegen genau die Zeile, die man spaeter
    sucht.
    """
    config = get_config(db)

    if not is_sending_enabled(config):
        if log_skipped:
            _log(db, to_address=to_address or "-", subject=mail.subject, kind=kind,
                 status="skipped", error="SMTP nicht eingerichtet oder deaktiviert",
                 user_id=user_id)
        return False

    if not to_address:
        if log_skipped:
            _log(db, to_address="-", subject=mail.subject, kind=kind, status="skipped",
                 error="Keine E-Mail-Adresse hinterlegt", user_id=user_id)
        return False

    html, text = render(mail, language)

    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = formataddr((config.from_name or "Lademonitor", config.from_address))
    message["To"] = to_address
    # Reiner Text zuerst, HTML als bevorzugte Alternative - genau die
    # Reihenfolge erwartet multipart/alternative.
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        _deliver(config, message)
    except Exception as exc:  # noqa: BLE001 - jeder SMTP-Fehler landet im Protokoll
        logger.warning("Mailversand an %s fehlgeschlagen: %s", to_address, exc)
        _log(db, to_address=to_address, subject=mail.subject, kind=kind,
             status="failed", error=str(exc), user_id=user_id)
        return False

    _log(db, to_address=to_address, subject=mail.subject, kind=kind, status="sent",
         user_id=user_id)
    return True


def send_test(db: Session, *, to_address: str, language: str | None, user_id: str) -> tuple[bool, str | None]:
    """Testmail plus Statusvermerk an der Konfiguration - dieselbe Doppelrolle
    wie "Jetzt sichern" beim WebDAV-Backup: der einzige ehrliche Test ist ein
    echter Versand."""
    config = get_config(db)
    with language_context(language):
        mail = Mail(
            subject=translate("email.test.subject"),
            heading=translate("email.test.heading"),
            intro=[translate("email.test.intro")],
            rows=[
                (translate("email.test.row_host"), f"{config.host}:{config.port}"),
                (translate("email.test.row_security"), config.security.value),
                (translate("email.test.row_from"), config.from_address),
            ],
        )
    ok = send(db, to_address=to_address, mail=mail, kind="test", language=language,
              user_id=user_id, log_skipped=True)

    last = (
        db.query(models.EmailLogEntry)
        .filter(models.EmailLogEntry.kind == "test")
        .order_by(models.EmailLogEntry.created_at.desc())
        .first()
    )
    config.last_test_at = datetime.utcnow()
    config.last_status = "ok" if ok else "error"
    config.last_error = None if ok else (last.error if last else "Unbekannter Fehler")
    db.commit()
    return ok, config.last_error
