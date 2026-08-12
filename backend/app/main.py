from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .auth import get_current_user, get_user_from_request
from .database import Base, engine, get_db, run_light_migrations
from .routers import auth, backup, geocoding, importer, locations, providers, sessions, stats, vehicles

Base.metadata.create_all(bind=engine)
run_light_migrations()

app = FastAPI(title="Lademonitor")

app.include_router(auth.router)
app.include_router(vehicles.router, dependencies=[Depends(get_current_user)])
app.include_router(providers.router, dependencies=[Depends(get_current_user)])
app.include_router(locations.router, dependencies=[Depends(get_current_user)])
app.include_router(sessions.router, dependencies=[Depends(get_current_user)])
app.include_router(stats.router, dependencies=[Depends(get_current_user)])
app.include_router(importer.router, dependencies=[Depends(get_current_user)])
app.include_router(geocoding.router, dependencies=[Depends(get_current_user)])
app.include_router(backup.router, dependencies=[Depends(get_current_user)])

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _page(request: Request, db: Session, template_name: str):
    """Rendert eine geschuetzte HTML-Seite, oder leitet bei fehlendem/ungueltigem
    Cookie zum Login um. Eigene (nicht-werfende) Variante von auth.get_current_user,
    da Seiten umleiten statt mit 401 antworten sollen."""
    user = get_user_from_request(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(template_name, {"request": request, "user": user})


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
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_user_from_request(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}
