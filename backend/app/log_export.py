"""ZIP-Export der Debug-Protokolle aller automatischen Quellen.

Die Rohdaten stehen in der Web-UI nur einzeln hinter je einem Link - wer
einen Ladevorgang nachvollziehen will, braucht aber meist alle Abrufe am
Stueck (Anlass: 59 einzeln heruntergeladene VW-Datensaetze, 19.10.2026).
Eine Datei je Eintrag mit Rohdaten, so wie sie der Einzel-Link liefert, und
dazu `protokoll.json` mit allen Eintraegen in zeitlicher Reihenfolge, damit
auch die Zeilen ohne Rohdaten (Fehler, angelegte Vorgaenge) dabei sind.

Enthaelt FIN und, je nach Quelle, GPS-Koordinaten aus den Rohantworten,
aber keine Zugangsdaten - die stehen nie im Protokoll.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any, Iterable

from fastapi.responses import Response

_OPTIONAL_FIELDS = ("charging_state", "soc_percent", "charge_power_kw")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "eintrag"


def build_zip(entries: Iterable[Any]) -> bytes:
    """`entries` aelteste zuerst. Gibt die fertige ZIP zurueck."""
    buffer = io.BytesIO()
    overview = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, entry in enumerate(entries, start=1):
            row = {
                "created_at": _iso(entry.created_at),
                "vehicle_id": entry.vehicle_id,
                "level": entry.level,
                "event": entry.event,
                "message": entry.message,
            }
            for field in _OPTIONAL_FIELDS:
                if hasattr(entry, field):
                    row[field] = getattr(entry, field)
            if hasattr(entry, "captured_at"):
                row["captured_at"] = _iso(entry.captured_at)
            if entry.payload:
                name = (
                    f"rohdaten/{number:04d}_{entry.created_at:%Y-%m-%dT%H-%M-%S}"
                    f"_{_safe(entry.event)}.json"
                )
                archive.writestr(name, entry.payload)
                row["payload_file"] = name
            overview.append(row)
        archive.writestr("protokoll.json", json.dumps(overview, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def zip_response(entries: Iterable[Any], prefix: str) -> Response:
    filename = f"{prefix}-protokoll-{datetime.utcnow():%Y-%m-%dT%H-%M-%S}.zip"
    return Response(
        content=build_zip(entries),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
