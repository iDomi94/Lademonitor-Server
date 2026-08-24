# Lademonitor

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

Selbstgehostete Ladevorgang-Tracking-App für ein E-Auto (Škoda Enyaq via
MySkoda/Home-Assistant-Integration), inspiriert von Spritmonitor. Läuft
komplett selbstgehostet, kein Cloud-Dienst.

Zugehörige iOS-App (SwiftUI, reiner REST-Client gegen dieses Backend):
[Lademonitor-App](https://github.com/iDomi94/Lademonitor-App)

## Features

- **Home Assistant**: [Lademonitor-HA](https://github.com/iDomi94/Lademonitor-HA)
  (HACS-Integration) holt Statistik-Sensoren (Kosten/Verbrauch/km) in HA und
  überträgt automatisch erkannte Ladevorgänge per Service statt manuellem
  `rest_command`-Token-Handling – deutlich einfacher als der rohe API-Weg
  unten. Für HA OS/Supervised gibt es zusätzlich
  [Lademonitor-HA-Addon](https://github.com/iDomi94/Lademonitor-HA-Addon),
  um den Server direkt als Add-on statt separat (z.B. auf Unraid) zu betreiben.
- Ladevorgänge manuell erfassen oder automatisch per Home-Assistant-Automation
  pushen lassen (`POST /api/sessions/auto`)
- Verbrauchsberechnung (kWh/100km) pro Ladevorgang über eine priorisierte
  Fallback-Kette (Vollladungs-Intervalle als Goldstandard, SoC-Korrektur,
  naive Rechnung, Schätzung) – Details in `CLAUDE.md`
- Ladeorte per Adresssuche anlegen (Nominatim) oder automatisch per
  Offline-Reverse-Geocoding einen Anzeigetext zu GPS-Koordinaten ermitteln
- Spritmonitor-CSV-Import mit Vorschau und Duplikat-Erkennung
- Vollständiger Backup-Export/-Import als ZIP (alle Fahrzeuge, Anbieter,
  Ladeorte, Ladevorgänge), gedacht für Server-Neuaufsetzung
- Mehrbenutzerfähig: Registrierung, Login, jeder Nutzer hat einen eigenen,
  komplett isolierten Datensatz
- Server-rendertes Web-UI (kein separates Frontend-Build nötig), reine
  REST-API für Home Assistant und die
  [iOS-App](https://github.com/iDomi94/Lademonitor-App) (separates Repo)

## Start / Installation

Vier Deployment-Wege – je nachdem, wo/wie der Server laufen soll. Backend,
Web-UI und Postgres stecken in allen vier Varianten im selben Container
(außer Docker Compose, siehe unten) – kein separater DB-Container nötig.

### Unraid (Community Applications)

Am einfachsten für Unraid-Nutzer: in **Apps** nach "Lademonitor" suchen und
installieren. Das Template (`templates/lademonitor-server.xml`) ist
vorkonfiguriert (Port, `/config`-Pfad für die Daten) und zeigt auf das
öffentliche GHCR-Image, das per GitHub Actions automatisch gebaut wird
(siehe `.github/workflows/docker-publish.yml`). Über den Tag-Selector im
Container-Edit lässt sich statt `latest` (stabil) auch `beta` (jeweils
aktuellster `main`-Stand) wählen.

### Home Assistant Add-on

Für HA OS/Supervised, um den Server direkt in Home Assistant statt separat
zu betreiben: **Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories**,
dort `https://github.com/iDomi94/Lademonitor-HA-Addon` eintragen, dann
"Lademonitor" installieren und starten. Daten liegen im Supervisor-`/data`-
Pfad und sind damit Teil regulärer HA-Backups. Details im
[Add-on-Repo](https://github.com/iDomi94/Lademonitor-HA-Addon).

### Docker (Einzelcontainer)

Für jeden anderen Docker-Host (Synology, VPS, Unraid ohne CA, …), ein
Container statt Stack:
```bash
docker run -d -p 8111:8000 -v /pfad/zu/daten:/config ghcr.io/idomi94/lademonitor-server:latest
```
`/config` enthält die komplette Postgres-Datenbank – unbedingt in den
Backup-Plan aufnehmen.

### Docker Compose (lokale Entwicklung/Tests)

Einziger Weg mit zwei getrennten Containern (Backend + Postgres), gedacht
für Entwicklung/Tests statt Produktivbetrieb:
```bash
docker compose up -d --build
```
Web-UI danach unter `http://<host-ip>:8111`. Zugangsdaten für die interne
Postgres-Instanz lassen sich optional per `.env` überschreiben (siehe
`.env.example`) – der Compose-interne Standard ist unkritisch, da Postgres
nicht nach außen exponiert wird.

---

API-Dokumentation (Swagger) liegt unter `/docs`.

## Erste Schritte

1. Account registrieren (`/register`) – der erste registrierte Nutzer wird
   automatisch Admin.
2. Unter **Einstellungen** dein Fahrzeug anlegen (External ID z.B. `enyaq` –
   wird 1:1 in der Home-Assistant-Automation wiederverwendet).
3. Optional: Ladeanbieter mit bekannten Preisen und bekannte Ladeorte anlegen.
4. Unter **Import** eine Spritmonitor-CSV hochladen, oder unter
   **Ladevorgänge** manuell erfassen.
5. Auf dem **Dashboard** Kosten-/Verbrauchsstatistiken ansehen.

## Home-Assistant-Anbindung

> **Empfehlung:** Statt der rohen API unten die
> [Lademonitor-HA](https://github.com/iDomi94/Lademonitor-HA)-Integration
> (über HACS) nutzen – übernimmt Login/Token-Handling und bringt fertige
> Services für den automatischen Push mit, inkl. einer vollständigen
> Beispiel-Automation in deren README.

`POST /api/sessions/auto` nimmt automatisch erkannte Ladevorgänge entgegen
(erwartet einen `Authorization: Bearer <token>`-Header – Token per
`POST /api/auth/login` holen, siehe `CLAUDE.md` für ein Beispiel-Package
mit `rest_command`-Automation). Direkter API-Zugriff bleibt für andere
Integrationen/Skripte natürlich weiterhin möglich. Erwartetes JSON:

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

Die `external_session_id` verhindert Duplikate, falls die Automation mehrfach
feuert. Sessions über diesen Weg werden mit `source: automatic` und
`needs_review: true` markiert und, falls die Koordinaten zu einem bekannten
Ladeort passen, automatisch mit Anbieter/Preis vorbefüllt.

## Backup

Zwei Ebenen:
- **Eingebauter Export/Import** (Einstellungen → Daten-Backup): ZIP mit CSVs
  aller eigenen Daten, ideal für eine Server-Neuinstallation.
- **Rohes Volume-Backup**: Bei Compose liegt die DB unter `./data/postgres`,
  beim Einzelcontainer unter dem gemounteten `/config`-Pfad – für Unraid
  reicht ein regelmäßiger Ordner-Backup-Job (z.B. CA Backup/Restore Appdata).

## Sicherheitshinweis

Auth ist eingebaut (Registrierung/Login, pro Nutzer isolierte Daten), aber
es gibt bewusst noch **kein Rate-Limiting** auf Login/Registrierung – bei
öffentlicher Erreichbarkeit über einen Reverse Proxy also auf ein starkes
Passwort achten. Details und weitere bekannte Einschränkungen in `CLAUDE.md`.

## Weiterführende Doku

`CLAUDE.md` enthält die vollständige Architektur-Dokumentation (Datenmodell,
Auth-Design, Verbrauchsberechnung, Deployment-Details) – Startpunkt für
alles, was hier nicht Platz findet.
