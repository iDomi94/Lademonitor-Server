# Lademonitor

**Sprache:** Deutsch | [English](README.en.md)

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
- **MyŠkoda Public API**: alternativ erkennt der Server Ladevorgänge selbst,
  indem er die offizielle Škoda-API direkt abfragt – ganz ohne Home Assistant.
  Nur API-Key und FIN eintragen, siehe
  [Automatische Ladeerkennung](#automatische-ladeerkennung-über-die-myškoda-public-api)
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

Zugriff (Ingress-Sidebar-Button oder Direktport `8111`) funktioniert dabei
nur lokal im Heimnetz – für externen Zugriff (z.B. die iOS-App unterwegs)
brauchst du zusätzlich einen VPN-Zugang zu deinem Netz oder einen eigenen
Reverse-Proxy/Nginx-Vhost auf Port 8111 (Details dazu im
[Add-on-Repo](https://github.com/iDomi94/Lademonitor-HA-Addon#netzwerkzugriff-lokal-vs-extern)).

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

## Automatische Ladeerkennung über die MyŠkoda Public API

Zweite, von Home Assistant unabhängige Quelle für automatisch erfasste
Ladevorgänge – gedacht für alle, die kein Home Assistant betreiben. Der
Server fragt die
[offizielle MyŠkoda Public API](https://public.api.connect.skoda-auto.cz/docs)
selbst ab, erkennt Beginn und Ende eines Ladevorgangs aus den Zustands-
übergängen und legt ihn wie einen HA-Push mit `source: automatic` und
`needs_review: true` an.

**Einrichtung** (Einstellungen → Automatische Ladeerkennung):

1. API-Key in der MyŠkoda-App erzeugen: [go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)
   (Link auf dem Handy öffnen). Der Key ist an die beim Erzeugen ausgewählten
   Fahrzeuge gebunden und läuft nach einiger Zeit ab.
2. Fahrzeug auswählen, API-Key und FIN (17 Zeichen) eintragen, speichern.
3. "Verbindung testen" zeigt die geparste Antwort der API – das ist gleichzeitig
   die Kontrolle, welche Felder das eigene Fahrzeug überhaupt liefert.
4. "Automatisch abfragen" aktivieren.

**Wichtig zu wissen:**

- **Nur einen der beiden Wege pro Fahrzeug aktivieren.** Der HA-Push und diese
  Erkennung wissen nichts voneinander und würden denselben Ladevorgang doppelt
  anlegen.
- **Rate-Limit: 20 Anfragen pro Stunde und API-Key**, geteilt über alle
  Fahrzeuge und alle Clients. Die Standardintervalle (20 min im Leerlauf,
  5 min während eines Ladevorgangs) bleiben mit Reserve darunter. Läuft
  parallel die
  [MySkoda-PublicAPI-Integration in Home Assistant](https://github.com/iDomi94/homeassistant-myskoda-public-api)
  mit demselben Key, teilen sich beide dieses Kontingent – dann besser einen
  zweiten Key erzeugen.
- **Genauigkeit:** Die API kennt keinen Push und keinen Energiezähler. Der
  Ladebeginn wird erst beim nächsten Abruf bemerkt, der Anfangs-SoC ist also
  eher zu hoch und die Energie eher zu niedrig – bei AC-Laden vernachlässigbar,
  bei DC-Schnellladen deutlich spürbar. Jeder erkannte Vorgang bekommt deshalb
  eine Notiz mit den Werten davor und danach.
- **Debug-Protokoll:** Unter derselben Sektion; protokolliert jede Abfrage und
  jede Erkennungsentscheidung inklusive der vollständigen Rohantworten und
  lässt sich als JSON herunterladen. Da das Antwortverhalten der API (noch in
  der Beta) während eines echten Ladevorgangs kaum dokumentiert ist, ist das
  der Weg, eine nicht erkannte Ladung nachträglich aufzuklären.
- Der API-Key liegt im Klartext in der Datenbank (wie alle Zugangsdaten dieser
  App), wird aber bewusst **nicht** in die Backup-ZIP exportiert.

Vorerst nur über die Web-UI konfigurierbar; die Endpunkte (`/api/myskoda/...`)
sind trotzdem regulärer Teil der REST-API und in `/docs` dokumentiert.

## Backup

Drei Ebenen:
- **Eingebauter Export/Import** (Einstellungen → Daten-Backup): ZIP mit CSVs
  aller eigenen Daten, ideal für eine Server-Neuinstallation.
- **Automatisches WebDAV-Backup** (Einstellungen → Automatisches
  WebDAV-Backup): lädt genau diese ZIP in konfigurierbarer Häufigkeit
  (täglich/wöchentlich/monatlich) automatisch auf einen WebDAV-Server hoch
  (z.B. Nextcloud) und räumt dort selbst hochgeladene Backups nach der
  eingestellten Aufbewahrungsfrist wieder auf. Pro Nutzer konfigurierbar,
  läuft im Hintergrund ohne Cron/externen Scheduler.
- **Rohes Volume-Backup**: Bei Compose liegt die DB unter `./data/postgres`,
  beim Einzelcontainer unter dem gemounteten `/config`-Pfad – für Unraid
  reicht ein regelmäßiger Ordner-Backup-Job (z.B. CA Backup/Restore Appdata).

## Sicherheitshinweis

Auth ist eingebaut (Registrierung/Login, pro Nutzer isolierte Daten), aber
es gibt bewusst noch **kein Rate-Limiting** auf Login/Registrierung – bei
öffentlicher Erreichbarkeit über einen Reverse Proxy also auf ein starkes
Passwort achten. Details und weitere bekannte Einschränkungen in `CLAUDE.md`.

## Nach einem Update nicht erreichbar?

Falls der Server nach einem Rebuild/Update über deinen eigenen Reverse
Proxy (z.B. Nginx vor Unraid) kurzzeitig nicht erreichbar ist und erst
nach Leeren des Browser-Caches wieder geht, sind typischerweise zwei
Ursachen möglich:

- **Browser-Cache**: Der Browser zeigt noch eine vor dem Update geladene
  Seite (altes HTML/JS) an, die nicht mehr zum neuen Server passt. Seit
  v0.9.1 senden die HTML-Seiten `Cache-Control: no-store`, damit Browser
  sie grundsätzlich nicht mehr zwischenspeichern – ein Hard-Refresh
  (Strg/Cmd+Shift+R) sollte das Problem seitdem nicht mehr auslösen.
- **Reverse Proxy/Docker-Netzwerk**: Ein Container-Rebuild vergibt meist
  eine neue interne Docker-IP. Falls dein Nginx den Zielserver einmalig
  auflöst und die IP danach nicht neu ermittelt (üblich bei einem
  statischen `upstream`-Eintrag ohne dynamische DNS-Auflösung), zeigt er
  nach einem Rebuild auf die alte, nicht mehr existierende IP – hilft dann
  ein Neuladen/Neustart des Reverse-Proxy-Containers, nicht das Leeren des
  Browser-Caches, ist das die wahrscheinlichere Ursache.

Ein `secure`-Cookie-Problem (siehe `CLAUDE.md`) ist es bei einem bereits
laufenden HTTPS-Setup **nicht** – das betrifft nur den direkten
HTTP-Zugriff ohne eigenen Reverse Proxy.

## Weiterführende Doku

`CLAUDE.md` enthält die vollständige Architektur-Dokumentation (Datenmodell,
Auth-Design, Verbrauchsberechnung, Deployment-Details) – Startpunkt für
alles, was hier nicht Platz findet.
