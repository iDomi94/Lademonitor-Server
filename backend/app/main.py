import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .auth import get_current_user, get_user_from_request
from .changelog import CHANGELOG, VERSION
from .database import Base, engine, get_db, run_light_migrations
from .i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_COOKIE_NAME,
    SUPPORTED_LANGUAGES,
    set_current_language,
    translate,
    translations_for,
)
from .myskoda_poller import SCHEDULER_INTERVAL_SECONDS as MYSKODA_SCHEDULER_INTERVAL_SECONDS
from .myskoda_poller import run_due_polls
from .routers import auth, backup, geocoding, importer, locations, myskoda, providers, sessions, stats, vehicles, webdav_backup
from .webdav_backup import run_due_backups

Base.metadata.create_all(bind=engine)
run_light_migrations()

logger = logging.getLogger(__name__)

# Alle 15 Minuten pruefen statt fest verdrahtet auf die kleinste moegliche
# Frequenz (taeglich) zu takten - Nutzer koennen die Haeufigkeit nachtraeglich
# aendern, ein zu grobes Scheduler-Intervall wuerde das erst verspaetet greifen
# lassen. 15 Minuten sind fuer ein Heimnetz-Backup mehr als praezise genug.
WEBDAV_SCHEDULER_INTERVAL_SECONDS = 15 * 60


async def _webdav_scheduler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_due_backups)
        except Exception:
            logger.exception("WebDAV-Backup-Scheduler-Durchlauf fehlgeschlagen")
        await asyncio.sleep(WEBDAV_SCHEDULER_INTERVAL_SECONDS)


async def _myskoda_scheduler_loop() -> None:
    """Automatische Ladeerkennung ueber die MyŠkoda Public API.

    Deutlich feiner getaktet als das WebDAV-Backup (Minute statt Viertelstunde),
    weil das Abfrageintervall waehrend eines laufenden Ladevorgangs bei wenigen
    Minuten liegt. Welches Fahrzeug tatsaechlich faellig ist, entscheidet
    `run_due_polls()` anhand von `next_poll_at` - der kurze Takt hier erzeugt
    also keine zusaetzlichen API-Anfragen."""
    while True:
        try:
            await asyncio.to_thread(run_due_polls)
        except Exception:
            logger.exception("MyŠkoda-Poller-Durchlauf fehlgeschlagen")
        await asyncio.sleep(MYSKODA_SCHEDULER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_webdav_scheduler_loop()),
        asyncio.create_task(_myskoda_scheduler_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Lademonitor", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Verhindert, dass Browser die serverseitig gerenderten, login-status-
    abhaengigen HTML-Seiten cachen - sonst kann nach einem Update (neues
    Image) oder einem Login/Logout eine veraltete Seite aus dem Browser-Cache
    angezeigt werden, obwohl der Server laengst anders antworten wuerde
    (aeusserte sich bei einem Nutzer nach einem Rebuild als "Seite erst nach
    Cache-Leeren wieder erreichbar")."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(auth.router)
app.include_router(vehicles.router, dependencies=[Depends(get_current_user)])
app.include_router(providers.router, dependencies=[Depends(get_current_user)])
app.include_router(locations.router, dependencies=[Depends(get_current_user)])
app.include_router(sessions.router, dependencies=[Depends(get_current_user)])
app.include_router(stats.router, dependencies=[Depends(get_current_user)])
app.include_router(importer.router, dependencies=[Depends(get_current_user)])
app.include_router(geocoding.router, dependencies=[Depends(get_current_user)])
app.include_router(backup.router, dependencies=[Depends(get_current_user)])
app.include_router(webdav_backup.router, dependencies=[Depends(get_current_user)])
app.include_router(myskoda.router, dependencies=[Depends(get_current_user)])

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
# `t('key')` in jedem Template nutzbar, ohne sie einzeln in jeden Render-Kontext
# aufzunehmen - liest die aktuell aktive Sprache aus einer ContextVar
# (siehe i18n/__init__.py::set_current_language, wird unten pro Request gesetzt).
templates.env.globals["t"] = translate


def _resolve_language(request: Request, user: models.User | None) -> str:
    """Eingeloggt ist die DB-Spalte User.language die Quelle der Wahrheit;
    ausgeloggt (Login/Registrieren) gibt es noch keinen Nutzer, daher der
    Cookie-Fallback (wird beim Sprachwechsel in den Einstellungen mitgesetzt,
    siehe routers/auth.py::set_language) - ohne das wuerde die Login-Seite
    nach einem Logout auf Deutsch zurueckspringen, obwohl der Nutzer zuvor
    Englisch gewaehlt hatte. Zuletzt einfach Deutsch als Basis-/Default-Sprache."""
    if user is not None and user.language in SUPPORTED_LANGUAGES:
        return user.language
    cookie_lang = request.cookies.get(LANGUAGE_COOKIE_NAME)
    if cookie_lang in SUPPORTED_LANGUAGES:
        return cookie_lang
    return DEFAULT_LANGUAGE


def _page(request: Request, db: Session, template_name: str):
    """Rendert eine geschuetzte HTML-Seite, oder leitet bei fehlendem/ungueltigem
    Cookie zum Login um. Eigene (nicht-werfende) Variante von auth.get_current_user,
    da Seiten umleiten statt mit 401 antworten sollen."""
    user = get_user_from_request(request, db)
    if not user:
        return RedirectResponse("login", status_code=303)
    lang = set_current_language(_resolve_language(request, user))
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "version": VERSION,
            "changelog": CHANGELOG,
            "lang": lang,
            "js_translations": translations_for(lang, prefix="filter."),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "index.html")


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "sessions.html")


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "import.html")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "settings.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_user_from_request(request, db):
        return RedirectResponse(".", status_code=303)
    lang = set_current_language(_resolve_language(request, None))
    return templates.TemplateResponse("login.html", {"request": request, "lang": lang})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_user_from_request(request, db):
        return RedirectResponse(".", status_code=303)
    lang = set_current_language(_resolve_language(request, None))
    return templates.TemplateResponse("register.html", {"request": request, "lang": lang})


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}
