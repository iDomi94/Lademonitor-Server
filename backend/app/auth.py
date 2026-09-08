"""Passwort-Hashing, Token-Erzeugung und Auth-Dependencies.

Ein Mechanismus fuer alle drei Clients: Browser (Web-UI) via httponly-Cookie,
iOS-App und Home-Assistant-rest_command via "Authorization: Bearer <token>".
Tokens sind opake, zufaellige Strings in der DB (siehe models.AuthToken),
kein JWT - einfacher zu widerrufen, keine Signatur-/Ablauf-Logik noetig.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import models
from .database import get_db

COOKIE_NAME = "session_token"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256-Hex eines Tokens - so liegen Auth-Tokens (models.AuthToken) und
    die einmaligen Mail-Tokens (models.UserToken) in der DB.

    Bewusst NICHT bcrypt wie beim Passwort: hier wird ueber Gleichheit
    nachgeschlagen, ein gesalzener Hash liesse sich gar nicht suchen. Noetig
    ist das auch nicht - der Token ist mit `token_urlsafe(32)` bereits 256 Bit
    Zufall, es gibt kein Woerterbuch und nichts zu erraten. bcrypt schuetzt
    Passwoerter davor, dass sie kurz und menschengemacht sind; dieses Problem
    existiert hier nicht.

    Der Nutzen: ein Auth-Token IST eine fertige Anmeldung. Im Klartext waere
    die Tabelle ein Generalschluessel fuer jedes Konto."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def get_user_from_request(request: Request, db: Session) -> models.User | None:
    token = _extract_token(request)
    if not token:
        return None
    auth_token = (
        db.query(models.AuthToken)
        .filter(models.AuthToken.token_hash == hash_token(token))
        .first()
    )
    return auth_token.user if auth_token else None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency fuer JSON-API-Endpunkte - 401 bei fehlendem/ungueltigem Token."""
    user = get_user_from_request(request, db)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if not user.is_admin:
        raise HTTPException(403, "Nur fuer Admins")
    return user


# ---------------------------------------------------------------------------
# Einmalige, per Mail verschickte Tokens (Passwort-Reset, Adressbestaetigung,
# Einladung) - siehe models.UserToken
# ---------------------------------------------------------------------------

# Kurz: wer den Link angefordert hat, klickt ihn gleich. Ein laenger gueltiger
# Reset-Token ist ein laenger offenes Fenster in ein fremdes Postfach.
PASSWORD_RESET_TTL = timedelta(hours=1)
# Grosszuegiger: die Bestaetigung ist keine Sicherheitsfunktion, sondern ein
# Tippfehlerschutz - sie darf auch am naechsten Tag noch klappen.
EMAIL_VERIFY_TTL = timedelta(days=1)
# Eine Einladung kann im Urlaub liegen bleiben.
INVITE_TTL = timedelta(days=7)

TOKEN_TTL = {
    models.UserTokenPurpose.PASSWORD_RESET: PASSWORD_RESET_TTL,
    models.UserTokenPurpose.EMAIL_VERIFY: EMAIL_VERIFY_TTL,
    models.UserTokenPurpose.INVITE: INVITE_TTL,
}


def create_user_token(
    db: Session,
    user: models.User,
    purpose: models.UserTokenPurpose,
    email: str | None = None,
) -> str:
    """Legt einen Einmal-Token an und gibt ihn im KLARTEXT zurueck - das ist
    der einzige Moment, in dem er existiert; gespeichert wird nur der Hash.

    Aeltere, noch offene Tokens desselben Zwecks werden verbraucht: sonst
    blieben nach mehrfachem Anfordern mehrere gueltige Links gleichzeitig
    offen, und der aelteste (z.B. aus einer weitergeleiteten Mail) waere
    genauso brauchbar wie der neueste."""
    now = datetime.utcnow()
    (
        db.query(models.UserToken)
        .filter(
            models.UserToken.user_id == user.id,
            models.UserToken.purpose == purpose,
            models.UserToken.used_at.is_(None),
        )
        .update({models.UserToken.used_at: now}, synchronize_session=False)
    )

    raw = generate_token()
    db.add(
        models.UserToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw),
            email=email,
            expires_at=now + TOKEN_TTL[purpose],
        )
    )
    db.commit()
    return raw


def check_user_token(
    db: Session, raw: str, purposes: tuple[models.UserTokenPurpose, ...]
) -> tuple[models.UserToken | None, str]:
    """Prueft einen Token, ohne ihn einzuloesen.

    Der Grund fuer die unterschiedlichen Ablehnungsgruende: "bereits
    verwendet" ist der mit Abstand haeufigste Fall (Mail-Client oeffnet Links
    zur Vorschau, Nutzer klickt danach nochmal) und verdient eine andere
    Auskunft als "ungueltig"."""
    row = (
        db.query(models.UserToken)
        .filter(models.UserToken.token_hash == hash_token(raw))
        .first()
    )
    if not row or row.purpose not in purposes:
        return None, "unknown"
    if row.used_at is not None:
        return None, "used"
    if row.expires_at < datetime.utcnow():
        return None, "expired"
    return row, "ok"


def consume_user_token(db: Session, row: models.UserToken) -> None:
    row.used_at = datetime.utcnow()
    db.commit()
