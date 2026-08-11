"""Geocoding in beide Richtungen.

reverse_geocode(): offline, kein Internetzugriff, Datensatz liegt im Package -
genutzt fuer den automatischen Ladeort-Anzeigetext (nur Stadt-/Ortsebene,
siehe CLAUDE.md).

forward_geocode(): bewusste Ausnahme von der "kein Cloud-Dienst"-Linie -
ruft die oeffentliche OpenStreetMap-Nominatim-API auf, weil eine offline
Adresssuche eine echte Strassendatenbank braeuchte und die Praezision hier
wichtig ist (Ladeort-Koordinaten steuern das radius-basierte Geo-Matching
gegen echte GPS-Punkte). Wird nur beim Anlegen/Bearbeiten eines Ladeorts
genutzt, also selten genug, dass Nominatims Nutzungsrichtlinie (max.
1 req/s) nie relevant wird.
"""

import httpx
import reverse_geocoder as rg

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "Lademonitor/1.0 (selfhosted charging tracker, no contact set)"


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Liefert einen Anzeigetext wie 'Leonberg, Baden-Württemberg' fuer Koordinaten."""
    try:
        result = rg.search((lat, lon))[0]
    except Exception:
        return None

    name = result.get("name")
    admin1 = result.get("admin1")
    if not name:
        return None
    if admin1 and admin1 != name:
        return f"{name}, {admin1}"
    return name


def forward_geocode(query: str, limit: int = 5) -> list[dict]:
    """Liefert Kandidaten (display_name/latitude/longitude) fuer eine Adress-
    oder Ortssuche. Leere Liste bei keinem Treffer oder Netzwerkfehler -
    Aufrufer faellt dann auf manuelle Koordinaten-Eingabe zurueck."""
    params = {"q": query, "format": "jsonv2", "limit": limit}
    headers = {"User-Agent": NOMINATIM_USER_AGENT}
    try:
        response = httpx.get(NOMINATIM_URL, params=params, headers=headers, timeout=5.0)
        response.raise_for_status()
        raw_results = response.json()
    except (httpx.HTTPError, ValueError):
        return []

    results = []
    for item in raw_results:
        try:
            results.append(
                {
                    "display_name": item["display_name"],
                    "latitude": float(item["lat"]),
                    "longitude": float(item["lon"]),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue
    return results
