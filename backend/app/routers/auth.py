import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import mailer, models, schemas
from ..auth import (
    COOKIE_NAME,
    check_user_token,
    consume_user_token,
    create_user_token,
    generate_token,
    get_current_user,
    hash_password,
    hash_token,
    require_admin,
    verify_password,
)
from ..database import get_db
from ..i18n import LANGUAGE_COOKIE_MAX_AGE, LANGUAGE_COOKIE_NAME, language_context, translate

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Rate-Limits fuer die oeffentlichen, mailausloesenden Endpunkte. Ohne die
# liesse sich mit dem Passwort-vergessen-Formular ein fremdes Postfach fluten
# und das eigene SMTP-Kontingent verbrennen. Bewusst ueber vorhandene Tabellen
# gezaehlt statt mit einer zusaetzlichen Abhaengigkeit (slowapi o.ae.) - fuer
# eine Heimnetz-App reicht das. Das offene Rate-Limit auf /login und /register
# bleibt davon unberuehrt, das ist ein eigenes Thema (siehe CLAUDE.md).
RESET_MAX_PER_HOUR_PER_USER = 3
MAIL_MAX_PER_HOUR_GLOBAL = 20

MIN_PASSWORD_LENGTH = 8

logger = logging.getLogger(__name__)


def _email_taken(db: Session, email: str, exclude_user_id: str | None = None) -> bool:
    """Eine Adresse darf nur einem Konto gehoeren - sonst waere beim
    Zuruecksetzen nicht entscheidbar, welches gemeint ist. Der Unique-Index in
    der DB faengt das ohnehin ab; diese Pruefung existiert nur, damit daraus
    eine verstaendliche 409 statt eines 500 wird."""
    query = db.query(models.User).filter(func.lower(models.User.email) == email.lower())
    if exclude_user_id:
        query = query.filter(models.User.id != exclude_user_id)
    return query.first() is not None

# ~400 Tage - Chrome deckelt Cookie-Max-Age ohnehin dort. Serverseitig laufen
# Tokens nicht ab, das Cookie ist nur dafuer da, dass der Browser eingeloggt
# bleibt statt bei jedem Neustart neu zu fragen.
COOKIE_MAX_AGE = 60 * 60 * 24 * 400


def _is_https(request: Request) -> bool:
    """Direkter Unraid-/Docker-/HA-Add-on-Zugriff laeuft per HTTP (kein
    Reverse-Proxy) - ein Secure-Cookie wuerde der Browser dann stillschweigend
    verwerfen und der Login-Cookie wuerde nie ankommen (sah wie ein
    401/Redirect-Loop nach erfolgreichem Login aus). Steht ein eigener
    Reverse-Proxy mit TLS davor (z.B. Nginx), setzt der ueblicherweise
    `X-Forwarded-Proto: https` - dann bleibt secure=True wie zuvor."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded_proto == "https" or request.url.scheme == "https"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        max_age=COOKIE_MAX_AGE,
    )


def _find_user_by_login(db: Session, identifier: str) -> models.User | None:
    """Anmeldung mit Nutzername ODER E-Mail-Adresse.

    Nutzername zuerst: er ist die eigentliche Identitaet und exakt eindeutig.
    Erst danach die Adresse, unabhaengig von Gross-/Kleinschreibung (der
    Unique-Index laeuft ebenfalls ueber lower(), siehe database.py) - sonst
    scheitert die Anmeldung an einem grossgeschriebenen Anfangsbuchstaben,
    den der Mail-Client vorschlaegt."""
    identifier = identifier.strip()
    if not identifier:
        return None
    user = db.query(models.User).filter(models.User.username == identifier).first()
    if user:
        return user
    return (
        db.query(models.User)
        .filter(func.lower(models.User.email) == identifier.lower())
        .first()
    )


def _issue_session(db: Session, request: Request, response: Response, user: models.User) -> str:
    """Neue Sitzung anlegen. Gespeichert wird nur der Hash - der Klartext
    verlaesst den Server einmal (Cookie bzw. Antwort) und existiert danach
    nirgends mehr (siehe models.AuthToken)."""
    token = generate_token()
    db.add(models.AuthToken(user_id=user.id, token_hash=hash_token(token)))
    db.commit()
    _set_session_cookie(request, response, token)
    return token


def _send_verification_mail(db: Session, user: models.User) -> None:
    """Bestaetigungslink an eine frisch eingetragene Adresse. Ohne Basis-Adresse
    gibt es keinen Link - dann bleibt die Adresse unbestaetigt und taugt fuer
    Benachrichtigungen, aber nicht zum Zuruecksetzen des Passworts."""
    config = mailer.get_config(db)
    if not user.email or not mailer.are_links_available(config):
        return
    raw = create_user_token(db, user, models.UserTokenPurpose.EMAIL_VERIFY, email=user.email)
    url = mailer.build_url(config, "verify-email", token=raw)
    with language_context(user.language):
        mail = mailer.Mail(
            subject=translate("email.verify.subject"),
            heading=translate("email.verify.heading"),
            intro=[
                translate("email.verify.intro", username=user.username),
                translate("email.verify.validity"),
            ],
            button=mailer.MailButton(translate("email.verify.button"), url),
            note=translate("email.verify.note"),
        )
    mailer.send(db, to_address=user.email, mail=mail, kind="email_verify",
                language=user.language, user_id=user.id, log_skipped=True)


def _notify_admins_of_registration(db: Session, new_user: models.User) -> None:
    """Die Registrierung ist bewusst offen - ohne diese Meldung faellt ein
    neues Konto nur auf, wenn jemand zufaellig in die Benutzerverwaltung
    schaut."""
    admins = (
        db.query(models.User)
        .filter(
            models.User.is_admin.is_(True),
            models.User.notify_new_registration.is_(True),
            models.User.email.isnot(None),
            models.User.id != new_user.id,
        )
        .all()
    )
    for admin in admins:
        with language_context(admin.language):
            mail = mailer.Mail(
                subject=translate("email.new_registration.subject"),
                heading=translate("email.new_registration.heading"),
                intro=[translate("email.new_registration.intro")],
                rows=[
                    (translate("email.new_registration.row_username"), new_user.username),
                    (translate("email.new_registration.row_email"), new_user.email or "–"),
                    (
                        translate("email.new_registration.row_created"),
                        new_user.created_at.strftime("%d.%m.%Y %H:%M"),
                    ),
                ],
                note=translate("email.new_registration.note"),
            )
        mailer.send(db, to_address=admin.email, mail=mail, kind="new_registration",
                    language=admin.language, user_id=admin.id)


@router.post("/register", response_model=schemas.LoginResponse, status_code=201)
def register(payload: schemas.RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    username = payload.username.strip()
    if len(username) < 3:
        raise HTTPException(422, "Nutzername muss mindestens 3 Zeichen lang sein")
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen lang sein")

    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        raise HTTPException(409, "Nutzername bereits vergeben")
    if payload.email and _email_taken(db, payload.email):
        raise HTTPException(409, "E-Mail-Adresse wird bereits verwendet")

    is_first_user = db.query(models.User).count() == 0
    user = models.User(
        username=username,
        password_hash=hash_password(payload.password),
        is_admin=is_first_user,
        email=payload.email,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = _issue_session(db, request, response, user)

    # Beides darf die Registrierung nicht scheitern lassen - ein kaputter
    # SMTP-Zugang ist kein Grund, jemandem das Konto zu verweigern. mailer.send
    # wirft von sich aus nicht, der Token-Teil koennte es theoretisch.
    try:
        _send_verification_mail(db, user)
        _notify_admins_of_registration(db, user)
    except Exception:  # noqa: BLE001
        logger.exception("Mailversand nach Registrierung fehlgeschlagen")

    return schemas.LoginResponse(token=token, user=user)


@router.post("/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = _find_user_by_login(db, payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Nutzername oder Passwort falsch")

    token = _issue_session(db, request, response, user)
    return schemas.LoginResponse(token=token, user=user)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth_header = request.headers.get("authorization")
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.cookies.get(COOKIE_NAME)

    if token:
        db.query(models.AuthToken).filter(
            models.AuthToken.token_hash == hash_token(token)
        ).delete()
        db.commit()
    response.delete_cookie(COOKIE_NAME)


@router.get("/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(get_current_user)):
    return user


@router.put("/language", response_model=schemas.UserOut)
def set_language(
    payload: schemas.LanguageUpdate,
    request: Request,
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persistiert die UI-Sprache auf dem Nutzer (Quelle der Wahrheit fuer
    eingeloggte Seiten) UND spiegelt sie in ein Cookie - Login-/Registrieren-
    Seite haben noch keinen Nutzer und lesen deshalb nur das Cookie (siehe
    main.py::_resolve_language). Nicht httponly, im Gegensatz zum
    Session-Cookie: enthaelt kein Geheimnis, es gibt also keinen Grund,
    clientseitigem JS den Zugriff zu verwehren."""
    user.language = payload.language
    db.commit()
    response.set_cookie(
        LANGUAGE_COOKIE_NAME,
        payload.language,
        samesite="lax",
        secure=_is_https(request),
        max_age=LANGUAGE_COOKIE_MAX_AGE,
    )
    return user


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), _: models.User = Depends(require_admin)):
    return db.query(models.User).order_by(models.User.created_at).all()


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "Du kannst dich nicht selbst löschen")
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "Nutzer nicht gefunden")
    db.delete(user)
    db.commit()


# ---------------------------------------------------------------------------
# Eigenes Konto: Passwort, E-Mail-Adresse, Benachrichtigungen
# ---------------------------------------------------------------------------

@router.put("/password", status_code=204)
def change_own_password(
    payload: schemas.PasswordChange,
    request: Request,
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Eigenes Passwort aendern. Gab es bisher ueberhaupt nicht - das Passwort
    liess sich nach der Registrierung nie mehr wechseln.

    Danach werden alle anderen Sitzungen beendet und eine frische ausgestellt:
    ein Passwortwechsel erfolgt haeufig genau deshalb, weil eine fremde Sitzung
    im Verdacht steht. Achtung, dieselbe Konsequenz wie beim Zuruecksetzen -
    der Home-Assistant-Token und die iOS-Anmeldung muessen neu eingetragen
    werden."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(403, "Aktuelles Passwort ist falsch")
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen lang sein")

    user.password_hash = hash_password(payload.new_password)
    db.query(models.AuthToken).filter(models.AuthToken.user_id == user.id).delete()
    # Offene Reset-Links entwerten: wer sein Passwort gerade selbst gesetzt
    # hat, will nicht, dass ein aelterer Link es wieder ueberschreiben kann.
    db.query(models.UserToken).filter(
        models.UserToken.user_id == user.id,
        models.UserToken.purpose == models.UserTokenPurpose.PASSWORD_RESET,
        models.UserToken.used_at.is_(None),
    ).update({models.UserToken.used_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()

    _issue_session(db, request, response, user)


@router.put("/email", response_model=schemas.UserOut)
def set_own_email(
    payload: schemas.EmailUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(403, "Aktuelles Passwort ist falsch")
    if payload.email and _email_taken(db, payload.email, exclude_user_id=user.id):
        raise HTTPException(409, "E-Mail-Adresse wird bereits verwendet")

    changed = (user.email or "").lower() != (payload.email or "").lower()
    user.email = payload.email
    if changed:
        # Eine geaenderte Adresse ist wieder unbestaetigt - sonst wuerde die
        # Bestaetigung der ALTEN Adresse fuer die neue weitergelten.
        user.email_verified_at = None
    db.commit()
    db.refresh(user)

    if changed and user.email:
        _send_verification_mail(db, user)
    return user


@router.post("/email/verify/resend", status_code=204)
def resend_verification(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.email:
        raise HTTPException(400, "Keine E-Mail-Adresse hinterlegt")
    if user.email_verified_at:
        raise HTTPException(400, "Adresse ist bereits bestätigt")
    _send_verification_mail(db, user)


@router.put("/notifications", response_model=schemas.UserOut)
def set_notifications(
    payload: schemas.NotificationSettingsUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user.notify_backup_failed = payload.notify_backup_failed
    user.notify_myskoda_error = payload.notify_myskoda_error
    user.notify_monthly_report = payload.notify_monthly_report
    user.notify_new_registration = payload.notify_new_registration
    user.review_digest = payload.review_digest
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Oeffentlich: Passwort zuruecksetzen, Adresse bestaetigen
# ---------------------------------------------------------------------------

def _reset_rate_limited(db: Session, user: models.User) -> bool:
    since = datetime.utcnow() - timedelta(hours=1)
    per_user = (
        db.query(models.UserToken)
        .filter(
            models.UserToken.user_id == user.id,
            models.UserToken.purpose == models.UserTokenPurpose.PASSWORD_RESET,
            models.UserToken.created_at >= since,
        )
        .count()
    )
    if per_user >= RESET_MAX_PER_HOUR_PER_USER:
        return True
    total_mails = (
        db.query(models.EmailLogEntry)
        .filter(
            models.EmailLogEntry.created_at >= since,
            models.EmailLogEntry.status == "sent",
        )
        .count()
    )
    return total_mails >= MAIL_MAX_PER_HOUR_GLOBAL


@router.post("/password-reset/request", status_code=204)
def request_password_reset(
    payload: schemas.PasswordResetRequest, db: Session = Depends(get_db)
):
    """Antwortet IMMER mit 204 - auch bei unbekanntem Konto, fehlender oder
    unbestaetigter Adresse, ausgeschaltetem SMTP oder erreichtem Rate-Limit.

    Jede Unterscheidung waere ein Orakel: wer die Antworten vergleicht,
    erfaehrt sonst, welche Nutzernamen und Adressen es auf diesem Server gibt.
    Was tatsaechlich passiert ist, steht im Versandprotokoll - sichtbar fuer
    Admins, nicht fuer den anonymen Aufrufer."""
    user = _find_user_by_login(db, payload.identifier)
    if not user:
        return
    config = mailer.get_config(db)
    if not mailer.are_links_available(config) or not user.email:
        return
    # Nur eine BESTAETIGTE Adresse darf ein Passwort zuruecksetzen: bei einem
    # Tippfehler haette sonst ein Fremder einen gueltigen Token fuer dieses
    # Konto in der Hand.
    if not user.email_verified_at:
        return
    if _reset_rate_limited(db, user):
        logger.warning("Reset-Anfrage fuer %s wegen Rate-Limit verworfen", user.username)
        return

    raw = create_user_token(db, user, models.UserTokenPurpose.PASSWORD_RESET)
    url = mailer.build_url(config, "reset-password", token=raw)
    with language_context(user.language):
        mail = mailer.Mail(
            subject=translate("email.reset.subject"),
            heading=translate("email.reset.heading"),
            intro=[
                translate("email.reset.intro", username=user.username),
                translate("email.reset.validity"),
            ],
            button=mailer.MailButton(translate("email.reset.button"), url),
            note=translate("email.reset.note"),
        )
    mailer.send(db, to_address=user.email, mail=mail, kind="password_reset",
                language=user.language, user_id=user.id, log_skipped=True)


@router.get("/password-reset/check", response_model=schemas.TokenCheckResult)
def check_reset_token(token: str, db: Session = Depends(get_db)):
    """Zustand eines Links, bevor der Nutzer ein Passwort eintippt - sonst
    erfaehrt er erst nach dem Absenden, dass der Link abgelaufen war."""
    row, reason = check_user_token(
        db, token,
        (models.UserTokenPurpose.PASSWORD_RESET, models.UserTokenPurpose.INVITE),
    )
    if not row:
        return schemas.TokenCheckResult(valid=False, reason=reason)
    return schemas.TokenCheckResult(
        valid=True, reason="ok", username=row.user.username, purpose=row.purpose.value
    )


@router.post("/password-reset/confirm", status_code=204)
def confirm_password_reset(
    payload: schemas.PasswordResetConfirm, db: Session = Depends(get_db)
):
    """Loest einen Reset- ODER Einladungs-Token ein - in beiden Faellen setzt
    der Nutzer ein Passwort, der Ablauf ist derselbe."""
    row, reason = check_user_token(
        db, payload.token,
        (models.UserTokenPurpose.PASSWORD_RESET, models.UserTokenPurpose.INVITE),
    )
    if not row:
        raise HTTPException(400, f"Link ungültig ({reason})")
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen lang sein")

    user = row.user
    user.password_hash = hash_password(payload.new_password)

    # Eine Einladung beweist nebenbei den Zugriff auf das Postfach - damit ist
    # die Adresse bestaetigt, ohne dass noch eine zweite Mail noetig waere.
    if row.purpose == models.UserTokenPurpose.INVITE and user.email:
        user.email_verified_at = datetime.utcnow()

    # ALLE Sitzungen beenden. Waere das Konto uebernommen worden, liefe die
    # Sitzung des Angreifers sonst unveraendert weiter. Konsequenz in genau
    # dieser App: der Home-Assistant-rest_command-Token und die iOS-Anmeldung
    # sterben mit und muessen neu eingetragen werden - darauf weisen Mail und
    # Bestaetigungsseite ausdruecklich hin.
    db.query(models.AuthToken).filter(models.AuthToken.user_id == user.id).delete()
    consume_user_token(db, row)


@router.post("/email/verify/confirm", status_code=204)
def confirm_email(payload: schemas.TokenOnly, db: Session = Depends(get_db)):
    row, reason = check_user_token(db, payload.token, (models.UserTokenPurpose.EMAIL_VERIFY,))
    if not row:
        raise HTTPException(400, f"Link ungültig ({reason})")
    user = row.user
    # Die im Token vermerkte Adresse muss noch die aktuelle sein - sonst wuerde
    # ein alter Link eine inzwischen geaenderte Adresse als geprueft markieren.
    if not user.email or (row.email or "").lower() != user.email.lower():
        consume_user_token(db, row)
        raise HTTPException(400, "Link gehört zu einer anderen E-Mail-Adresse")
    user.email_verified_at = datetime.utcnow()
    consume_user_token(db, row)


# ---------------------------------------------------------------------------
# Admin: Konten anlegen, Adressen pflegen
# ---------------------------------------------------------------------------

def _send_invite(db: Session, user: models.User) -> bool:
    config = mailer.get_config(db)
    if not mailer.are_links_available(config) or not user.email:
        return False
    raw = create_user_token(db, user, models.UserTokenPurpose.INVITE, email=user.email)
    url = mailer.build_url(config, "reset-password", token=raw)
    with language_context(user.language):
        mail = mailer.Mail(
            subject=translate("email.invite.subject"),
            heading=translate("email.invite.heading"),
            intro=[
                translate("email.invite.intro", username=user.username),
                translate("email.invite.validity"),
            ],
            button=mailer.MailButton(translate("email.invite.button"), url),
            note=translate("email.invite.note"),
        )
    return mailer.send(db, to_address=user.email, mail=mail, kind="invite",
                       language=user.language, user_id=user.id, log_skipped=True)


@router.post("/users", response_model=schemas.UserOut, status_code=201)
def create_user(
    payload: schemas.AdminUserCreate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Konto anlegen und einladen - der Nutzer setzt sein Passwort selbst ueber
    den Link. Bewusst kein vom Admin vergebenes Startpasswort: das muesste
    ueber einen anderen Kanal uebermittelt werden und waere danach einem
    Zweiten bekannt.

    Braucht denselben Aufbau wie ein Reset, weshalb es hier statt in einem
    eigenen Endpunkt sitzt."""
    username = payload.username.strip()
    if len(username) < 3:
        raise HTTPException(422, "Nutzername muss mindestens 3 Zeichen lang sein")
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(409, "Nutzername bereits vergeben")
    if _email_taken(db, payload.email):
        raise HTTPException(409, "E-Mail-Adresse wird bereits verwendet")
    if not mailer.are_links_available(mailer.get_config(db)):
        raise HTTPException(
            400,
            "E-Mail-Versand ist nicht eingerichtet - ohne Einladungslink kann "
            "der Nutzer kein Passwort setzen.",
        )

    user = models.User(
        username=username,
        # Ein zufaelliger Hash-Platzhalter: das Konto ist bis zum Einloesen der
        # Einladung nicht anmeldbar, und es gibt kein Passwort, das jemand
        # erraten koennte. Ein leerer String waere ein gueltiger bcrypt-Input
        # und damit ein anmeldbares Konto mit leerem Passwort.
        password_hash=hash_password(generate_token()),
        is_admin=payload.is_admin,
        email=payload.email,
        language=admin.language,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    _send_invite(db, user)
    return user


@router.put("/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: str,
    payload: schemas.AdminUserUpdate,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "Nutzer nicht gefunden")
    if payload.email and _email_taken(db, payload.email, exclude_user_id=user.id):
        raise HTTPException(409, "E-Mail-Adresse wird bereits verwendet")

    changed = (user.email or "").lower() != (payload.email or "").lower()
    user.email = payload.email
    if changed:
        # Vom Admin eingetragen heisst nicht bestaetigt - der Nutzer muss den
        # Zugriff auf das Postfach selbst nachweisen, sonst waere ein Vertipper
        # des Admins ein Reset-Recht fuer einen Fremden.
        user.email_verified_at = None
    db.commit()
    db.refresh(user)

    if changed and user.email:
        _send_verification_mail(db, user)
    return user


@router.post("/users/{user_id}/invite", status_code=204)
def resend_invite(
    user_id: str,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_admin),
):
    """Einladung erneut schicken - z.B. wenn die erste im Spam gelandet oder
    der Link abgelaufen ist."""
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "Nutzer nicht gefunden")
    if not user.email:
        raise HTTPException(400, "Nutzer hat keine E-Mail-Adresse")
    if not _send_invite(db, user):
        raise HTTPException(400, "Einladung konnte nicht verschickt werden - siehe E-Mail-Protokoll")
