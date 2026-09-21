"""Einstellungen und Nachtrag fuer die Aussentemperatur vom Wetterdienst.

Warum das Ganze opt-in und pro Nutzer ist, steht in `weather.py`.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas, weather
from ..auth import get_current_user
from ..database import get_db

router = APIRouter(prefix="/api/weather", tags=["weather"])


def _to_out(user: models.User) -> schemas.WeatherSettingsOut:
    return schemas.WeatherSettingsOut(
        enabled=user.weather_autofill_enabled,
        api_url=user.weather_api_url,
        effective_api_url=user.weather_api_url or weather.DEFAULT_API_URL,
        coordinate_precision=weather.COORD_PRECISION,
    )


@router.get("/settings", response_model=schemas.WeatherSettingsOut)
def get_settings(user: models.User = Depends(get_current_user)):
    return _to_out(user)


@router.put("/settings", response_model=schemas.WeatherSettingsOut)
def set_settings(
    payload: schemas.WeatherSettingsUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    user.weather_autofill_enabled = payload.enabled
    user.weather_api_url = payload.api_url
    db.commit()
    db.refresh(user)
    return _to_out(user)


@router.post("/backfill", response_model=schemas.WeatherBackfillResult)
def backfill(
    dry_run: bool = Query(
        default=True,
        description=(
            "Vorgabe true: es wird abgefragt und gezeigt, was passieren wuerde, "
            "aber nichts geschrieben."
        ),
    ),
    overwrite: bool = Query(
        default=False,
        description="Auch bereits gesetzte Temperaturen ersetzen (Vorgabe: nein).",
    ),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Aussentemperatur fuer bestehende Ladevorgaenge nachtragen.

    Bewusst unabhaengig vom Schalter `weather_autofill_enabled`: das hier ist
    eine ausdrueckliche Einzelaktion des Nutzers, kein Hintergrundverhalten -
    und wer den Nachtrag einmal laufen lassen will, muss dafuer nicht die
    Dauer-Automatik einschalten. Der Aufruf selbst IST die Einwilligung.
    """
    report = weather.backfill_sessions(
        db, user, dry_run=dry_run, overwrite=overwrite
    )
    return schemas.WeatherBackfillResult(
        dry_run=dry_run,
        considered=report.considered,
        already_set=report.already_set,
        without_coordinates=report.without_coordinates,
        resolved=report.resolved,
        unresolved=report.unresolved,
        written=report.written,
        preview=[schemas.BackfillPreviewItem(**item) for item in report.preview],
    )
