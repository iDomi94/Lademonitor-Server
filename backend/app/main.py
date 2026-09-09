import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import crypto, models
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
from .mailer import are_links_available, get_config as get_smtp_config
from .myskoda_poller import SCHEDULER_INTERVAL_SECONDS as MYSKODA_SCHEDULER_INTERVAL_SECONDS
from .myskoda_poller import run_due_polls
from .notifications import SCHEDULER_INTERVAL_SECONDS as NOTIFICATION_SCHEDULER_INTERVAL_SECONDS
from .notifications import run_due_notifications
from .routers import (
    auth,
    backup,
    email,
    geocoding,
    importer,
    locations,
    myskoda,
    providers,
    sessions,
    stats,
    vehicles,
    webdav_backup,
)
from .webdav_backup import run_due_backups

# Feld-Verschluesselung ist opt-in (siehe crypto.py) - hier wird nur das
# FORMAT eines GESETZTEN Schluessels vor jedem DB-Zugriff geprueft, nicht
# seine Anwesenheit. run_light_migrations() unten prueft zusaetzlich, ob
# trotz fehlenden Schluessels bereits verschluesselte Bestandsdaten
# existieren (verweigert dann den Start, statt kaputte Werte auszuliefern).
crypto.check_configured()

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


async def _notification_scheduler_loop() -> None:
    """Zeitgesteuerte Benachrichtigungen (Ablaufwarnung des MyŠkoda-API-Keys,
    Sammelmeldung zu pruefender Ladevorgaenge, Monatsbericht).

    Derselbe Takt wie beim WebDAV-Backup: die feinste Faelligkeit ist "einmal
    taeglich", haeufiger nachzusehen bringt nichts. Was tatsaechlich faellig
    ist, entscheidet `run_due_notifications()` anhand von Zeitstempeln in der
    DB - der Takt hier verschickt fuer sich genommen gar nichts."""
    while True:
        try:
            await asyncio.to_thread(run_due_notifications)
        except Exception:
            logger.exception("Benachrichtigungs-Scheduler-Durchlauf fehlgeschlagen")
        await asyncio.sleep(NOTIFICATION_SCHEDULER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_webdav_scheduler_loop()),
        asyncio.create_task(_myskoda_scheduler_loop()),
        asyncio.create_task(_notification_scheduler_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Lademonitor", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Verhindert, dass Browser veraltete Seiten oder Assets aus dem Cache
    zeigen, obwohl der Server laengst anders antworten wuerde.

    HTML: `no-store`. Die Seiten sind login-status-abhaengig, nach einem
    Login/Logout oder einem Update (neues Image) darf da nichts Altes mehr
    kommen (aeusserte sich bei einem Nutzer nach einem Rebuild als "Seite erst
    nach Cache-Leeren wieder erreichbar").

    Statische Dateien: `no-cache`, also "vor Benutzung nachfragen" - NICHT
    `no-store`. `StaticFiles` liefert ETag und Last-Modified, aber von sich aus
    keinen `Cache-Control`-Header; ohne den wenden Browser heuristisches Caching
    an und halten eine Datei ohne jede Rueckfrage fuer frisch. Genau das ist
    2026-09-06 passiert: nach dem Responsive-Update kam auf dem Handy das neue
    HTML mit der ALTEN style.css an - der Menue-Knopf war da, hatte aber keine
    Regeln, die Ladevorgangs-Karten waren voellig ungestylt. Mit `no-cache`
    fragt der Browser jedes Mal kurz nach und bekommt in aller Regel ein
    leeres 304 zurueck, also praktisch dieselbe Ersparnis ohne das Risiko.
    Zusaetzlich haengt an style.css/filter.js ein `?v=`-Parameter mit der
    App-Version (siehe base.html), damit ein Update auch bei einem
    zwischengeschalteten Proxy-Cache garantiert durchschlaegt."""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
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
# email.router prueft pro Endpunkt selbst auf Admin (require_admin), braucht
# hier also nur die allgemeine Anmeldepflicht wie die uebrigen Router.
app.include_router(email.router, dependencies=[Depends(get_current_user)])

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


def _page(request: Request, db: Session, template_name: str, **extra):
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
            "encryption_enabled": crypto.is_enabled(),
            "lang": lang,
            "js_translations": translations_for(lang, prefix="filter."),
            **extra,
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
    # Die Einstellungen zeigen bei den Benachrichtigungen an, ob ueberhaupt ein
    # SMTP-Zugang eingerichtet ist - sonst schaltet man dort Haken an, von denen
    # nie etwas ankommt. Nur auf dieser einen Seite abgefragt, nicht in _page()
    # fuer alle.
    smtp = get_smtp_config(db)
    return _page(
        request, db, "settings.html",
        smtp_ready=smtp.enabled and bool(smtp.host and smtp.from_address),
        smtp_links_ready=are_links_available(smtp),
    )


# Unterseiten der Einstellungen (Backup, API/Debug). Die Pfade liegen bewusst
# auf oberster Ebene und NICHT unter /settings/...: alle Links und
# fetch()-Aufrufe der Templates sind relativ, damit sie unter dem
# Home-Assistant-Ingress-Unterpfad genauso aufloesen wie am Domain-Root - das
# funktioniert nur, solange jede Seite genau eine Ebene unter der Basis liegt
# (siehe den Kommentar zur Ingress-Umstellung in CLAUDE.md). "/backup" kollidiert
# nicht mit dem Backup-Router, der unter "/api/backup" haengt.
@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "settings_backup.html")


@app.get("/api-debug", response_class=HTMLResponse)
def api_debug_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "settings_api.html")


@app.get("/email", response_class=HTMLResponse)
def email_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "settings_email.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_user_from_request(request, db):
        return RedirectResponse(".", status_code=303)
    lang = set_current_language(_resolve_language(request, None))
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "lang": lang,
            "version": VERSION,
            # Der Link fuehrt sonst in eine Sackgasse: ohne eingerichteten
            # SMTP-Zugang und ohne konfigurierte Basis-Adresse kann gar keine
            # Reset-Mail entstehen (siehe models.SmtpConfig.base_url).
            "reset_available": are_links_available(get_smtp_config(db)),
        },
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_user_from_request(request, db):
        return RedirectResponse(".", status_code=303)
    lang = set_current_language(_resolve_language(request, None))
    return templates.TemplateResponse(
        "register.html", {"request": request, "lang": lang, "version": VERSION}
    )


def _public_page(request: Request, db: Session, template_name: str, **extra):
    """Seiten, die ohne Anmeldung erreichbar sein MUESSEN - wer sein Passwort
    vergessen hat, kann sich per Definition nicht anmelden. Analog zu
    /login und /register, aber mit der Moeglichkeit, zusaetzliche Werte in den
    Kontext zu geben (z.B. den Token aus der Adresszeile)."""
    lang = set_current_language(_resolve_language(request, None))
    return templates.TemplateResponse(
        template_name,
        {"request": request, "lang": lang, "version": VERSION, **extra},
    )


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, db: Session = Depends(get_db)):
    return _public_page(request, db, "forgot_password.html")


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, db: Session = Depends(get_db)):
    return _public_page(request, db, "reset_password.html")


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(request: Request, db: Session = Depends(get_db)):
    return _public_page(request, db, "verify_email.html")


def _privacy_controller() -> dict[str, str | None]:
    """Kontaktdaten des datenschutzrechtlich Verantwortlichen dieser
    Installation - bewusst NICHT im Code, sondern optional per Umgebungs-
    variable gesetzt (siehe .env.example): der Betreiber einer selbst-
    gehosteten Instanz ist der Verantwortliche, nicht die Autorin/der Autor
    der Software. Ungesetzt zeigt privacy.html einen deutlichen Hinweis
    statt stillschweigend falsche/fehlende Angaben zu verstecken."""
    return {
        "name": os.getenv("PRIVACY_CONTROLLER_NAME"),
        "address": os.getenv("PRIVACY_CONTROLLER_ADDRESS"),
        "email": os.getenv("PRIVACY_CONTROLLER_EMAIL"),
    }


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, db: Session = Depends(get_db)):
    """Oeffentlich erreichbar wie /login - eine Datenschutzerklaerung, die
    erst nach einer Anmeldung zu lesen ist, waere fuer die Registrierung und
    fuer die App-Store-Privacy-URL wertlos."""
    controller = _privacy_controller()
    return _public_page(
        request, db, "privacy.html",
        controller=controller,
        controller_configured=bool(controller["name"] and controller["email"]),
    )


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION, "field_encryption": crypto.is_enabled()}
