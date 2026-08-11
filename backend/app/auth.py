"""Passwort-Hashing, Token-Erzeugung und Auth-Dependencies.

Ein Mechanismus fuer alle drei Clients: Browser (Web-UI) via httponly-Cookie,
iOS-App und Home-Assistant-rest_command via "Authorization: Bearer <token>".
Tokens sind opake, zufaellige Strings in der DB (siehe models.AuthToken),
kein JWT - einfacher zu widerrufen, keine Signatur-/Ablauf-Logik noetig.
"""

import secrets

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
        db.query(models.AuthToken).filter(models.AuthToken.token == token).first()
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
