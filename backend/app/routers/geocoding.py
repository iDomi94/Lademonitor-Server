from fastapi import APIRouter, Query

from .. import schemas
from ..geocode import forward_geocode

router = APIRouter(prefix="/api/geocode", tags=["geocode"])


@router.get("/forward", response_model=list[schemas.GeocodeResult])
def geocode_forward(query: str = Query(..., min_length=3)):
    """Proxy fuer die oeffentliche Nominatim-Adresssuche, genutzt beim
    Anlegen/Bearbeiten von Ladeorten. Siehe geocode.py fuer die Begruendung."""
    return forward_geocode(query)
