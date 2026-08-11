# Charging Tracker

Selbstgehostete Web-App zum Tracken von Ladevorgängen (E-Auto), inkl. Statistiken,
Spritmonitor-Import und Vorbereitung für die Home-Assistant-Anbindung.

## Start (Unraid / beliebiger Docker-Host)

```bash
docker compose up -d --build
```

Danach ist die Web-UI erreichbar unter `http://<unraid-ip>:8000`.

Die API-Dokumentation (Swagger) liegt automatisch unter `http://<unraid-ip>:8000/docs` –
praktisch zum Testen einzelner Endpunkte, auch schon für die spätere App-Anbindung.

## Erste Schritte

1. Unter **Einstellungen** dein Fahrzeug anlegen (External ID z.B. `enyaq` – die
   brauchen wir später 1:1 in der Home-Assistant-Automation wieder).
2. Optional: Ladeanbieter mit bekannten Preisen anlegen.
3. Optional: Bekannte Ladeorte (Koordinaten) mit Standard-Anbieter verknüpfen.
4. Unter **Import** deine Spritmonitor-CSV-Exportdatei hochladen und die Vorschau prüfen,
   bevor final importiert wird.
5. Unter **Ladevorgänge** manuell Sessions erfassen.
6. Auf dem **Dashboard** siehst du Kosten/Verbrauch-Statistiken.

## Wichtiger Hinweis zur Spritmonitor-Spaltenerkennung

Spritmonitor-CSV-Exporte können je nach Spracheinstellung/Fahrzeugtyp leicht
unterschiedliche Spaltennamen haben. Die Spalten-Erkennung liegt in
`backend/app/routers/importer.py` in `COLUMN_ALIASES` – falls beim ersten Import
die Vorschau leer bleibt oder Fehler wirft, bitte die tatsächlichen Spaltennamen
deiner Export-Datei dort ergänzen.

## Home-Assistant-Anbindung (Phase 3, vorbereitet)

Der Endpunkt `POST /api/sessions/auto` ist bereits fertig für den Empfang von
automatisch erkannten Ladevorgängen. Erwartetes JSON:

```json
{
  "vehicle_external_id": "enyaq",
  "external_session_id": "20260809_1432",
  "start_time": "2026-08-09T14:05:00",
  "end_time": "2026-08-09T14:32:00",
  "soc_start": 42,
  "soc_end": 89,
  "odometer_km": 48213,
  "latitude": 52.5200,
  "longitude": 13.4050
}
```

Die `external_session_id` verhindert Duplikate, falls die Automation mehrfach feuert.
Sessions, die über diesen Weg reinkommen, werden automatisch mit
`source: automatic` und `needs_review: true` markiert, und – falls die
Koordinaten zu einem bekannten Ladeort passen – direkt mit Anbieter und
zuletzt gezahltem Preis vorbefüllt.

## Sicherheitshinweis für den produktiven Betrieb

Die Standard-Zugangsdaten in `docker-compose.yml` (`charging`/`charging`) sind nur
für den lokalen Start gedacht. Falls der Server über eine Portweiterleitung/
Reverse Proxy von außen erreichbar sein soll, unbedingt:
- Postgres-Passwort ändern
- Einen Reverse Proxy mit HTTPS + Basic-Auth oder besser eine Authentifizierung
  in der FastAPI-App selbst ergänzen (aktuell ist noch **keine** Auth eingebaut –
  das sollten wir vor dem Prod-Einsatz noch nachrüsten)

## Backup

Die komplette Datenbank liegt unter `./data/postgres`. Für Unraid reicht ein
regelmäßiger Ordner-Backup-Job (z.B. via CA Backup / Restore Appdata Plugin) auf
diesen Pfad.

## Nächste Schritte (siehe Gesamtplan)

- Phase 2: iOS-App als reiner REST-Client gegen dieses Backend
- Phase 3: Home-Assistant-Automation, die `POST /api/sessions/auto` aufruft
- Phase 4: Auth/Absicherung, falls von außerhalb des Heimnetzes zugegriffen wird
