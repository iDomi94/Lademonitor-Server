from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import (
    COOKIE_NAME,
    generate_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from ..database import get_db
from ..i18n import LANGUAGE_COOKIE_MAX_AGE, LANGUAGE_COOKIE_NAME

router = APIRouter(prefix="/api/auth", tags=["auth"])

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


@router.post("/register", response_model=schemas.LoginResponse, status_code=201)
def register(payload: schemas.RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    username = payload.username.strip()
    if len(username) < 3:
        raise HTTPException(422, "Nutzername muss mindestens 3 Zeichen lang sein")
    if len(payload.password) < 8:
        raise HTTPException(422, "Passwort muss mindestens 8 Zeichen lang sein")

    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        raise HTTPException(409, "Nutzername bereits vergeben")

    is_first_user = db.query(models.User).count() == 0
    user = models.User(
        username=username,
        password_hash=hash_password(payload.password),
        is_admin=is_first_user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = generate_token()
    db.add(models.AuthToken(user_id=user.id, token=token))
    db.commit()

    _set_session_cookie(request, response, token)
    return schemas.LoginResponse(token=token, user=user)


@router.post("/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username.strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Nutzername oder Passwort falsch")

    token = generate_token()
    db.add(models.AuthToken(user_id=user.id, token=token))
    db.commit()

    _set_session_cookie(request, response, token)
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
        db.query(models.AuthToken).filter(models.AuthToken.token == token).delete()
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
