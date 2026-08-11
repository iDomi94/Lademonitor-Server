from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import models
from .database import Base, engine, run_light_migrations
from .routers import backup, geocoding, importer, locations, providers, sessions, stats, vehicles

Base.metadata.create_all(bind=engine)
run_light_migrations()

app = FastAPI(title="Charging Tracker")

app.include_router(vehicles.router)
app.include_router(providers.router)
app.include_router(locations.router)
app.include_router(sessions.router)
app.include_router(stats.router)
app.include_router(importer.router)
app.include_router(geocoding.router)
app.include_router(backup.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request):
    return templates.TemplateResponse("sessions.html", {"request": request})


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return templates.TemplateResponse("import.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/health")
def health():
    return {"status": "ok"}
