"""In-Memory-Rate-Limiter fuer oeffentliche Auth-Endpunkte (Login, Registrierung).

Bewusst kein Redis/slowapi: der Server laeuft als einzelner Python-Prozess pro
Container (siehe entrypoint.sh - genau ein uvicorn-Prozess neben Postgres),
ein Dict im Prozessspeicher reicht fuer den Heimnetz-/Kleininstanz-Massstab
dieser App. Bei einem Neustart ist der Zaehler leer - unkritisch, dafuer
muesste ein Angreifer den Container-Neustart selbst ausloesen koennen. Bei
mehreren Workern/Replikas wuerde dieser Ansatz nicht mehr greifen (jeder
Prozess zaehlt fuer sich) - fuer diese App nicht der Fall.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request

_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def client_ip(request: Request) -> str:
    """Client-IP hinter einem Reverse Proxy (z.B. Nginx Proxy Manager) ermitteln.

    Ohne das waere `request.client.host` bei jedem Nutzer identisch die
    Proxy-IP - der Rate-Limiter wuerde dann alle Nutzer hinter demselben
    Proxy in einen Topf werfen. `X-Forwarded-For` wird nur vom eigenen
    Reverse Proxy gesetzt, nicht vom Client selbst validiert - bei direktem
    Zugriff ohne Proxy (z.B. rohes `docker run` im Heimnetz ohne
    vorgeschalteten Nginx) koennte ein Client den Header faelschen und den
    Limiter damit umgehen. Fuer den dokumentierten Betrieb dieser App
    (Heimnetz oder eigener Reverse Proxy) ein akzeptabler Kompromiss - der
    Limiter ist eine zusaetzliche Schutzschicht, nicht die einzige
    Verteidigungslinie (Passwort-Hashing/Token bleiben die eigentliche
    Absicherung)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_rate_limited(key: str, max_attempts: int, window_seconds: int) -> bool:
    """Sliding-Window-Zaehler: max_attempts Aufrufe pro window_seconds und Key.

    Ein Aufruf, der das Limit bereits erreicht hat, wird NICHT mitgezaehlt -
    sonst wuerde ein Angreifer, der einfach weiter anfragt, das Fenster
    kuenstlich am Leben halten und nie wieder freikommen."""
    now = time.monotonic()
    with _lock:
        bucket = _attempts[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            return True
        bucket.append(now)
        return False
