# Lademonitor – Projektkontext

Selbstgehostete Ladevorgang-Tracking-App für ein E-Auto (Škoda Enyaq via
MySkoda-Integration), inspiriert von Spritmonitor. Läuft komplett selbstgehostet
auf Unraid (Docker), kein Cloud-Dienst.

## Architektur-Entscheidung (wichtig für den Kontext)

Es gab einen bewussten Architektur-Wechsel während der Planung: statt Daten
lokal auf dem iPhone zu speichern, ist die **Datenbank zentral auf dem Server**
(Unraid). Web-UI und iOS-App sind beides reine Clients gegen dieselbe REST-API.
Grund: Nutzer hat bereits eine Selfhosting-Infrastruktur (Unraid + Home
Assistant) und wollte Laptop-Zugriff parallel zur App, ohne eine Sync-Schicht
zwischen App und Server bauen zu müssen. Die App ist deshalb **online-only**
(funktioniert nicht ohne Verbindung zum Server) - das ist Absicht, kein Bug.

## Tech-Stack

- **Backend:** FastAPI (Python) + PostgreSQL + SQLAlchemy, Docker auf Unraid
- **Web-UI:** Server-rendered Jinja2, KEIN Chart.js/externe CDN-Libs mehr
  (wurden entfernt, weil Client keinen CDN-Zugriff hatte) - Charts sind
  selbstgebaute SVG-Balkendiagramme in reinem JS
- **iOS-App:** SwiftUI, reiner REST-Client (kein SwiftData/CoreData), async/await
- **Deployment:** `docker compose up -d --build` im Projekt-Root auf Unraid,
  Compose-Projektname/Stack-Name beim Nutzer: "Lademonitor"

## Projektstruktur

```
backend/app/
  models.py          - SQLAlchemy: Vehicle, Provider, ChargingLocation, ChargingSession
  schemas.py          - Pydantic Request/Response-Schemas
  routers/
    vehicles.py, providers.py, locations.py, sessions.py, stats.py, importer.py,
    geocoding.py, backup.py
  templates/          - Jinja2 Web-UI (index=Dashboard, sessions, import, settings)
  static/style.css
ios/Lademonitor/
  Models/Models.swift          - Swift-Pendant zu schemas.py
  Networking/APIClient.swift, AppSettings.swift
  Views/                       - Dashboard, SessionsList, AddEditSession, Settings, ContentView
```

## Datenmodell-Kernpunkte

- **ChargingSession** ist die zentrale Entität: vehicle_id, provider_id,
  location_id (alle FK), start_time, soc_start/soc_end, energy_kwh
  (kann geschätzt sein via `energy_is_estimated`, aus SoC-Delta × Akkukapazität),
  odometer_km, price_total/price_per_kwh, latitude/longitude, source
  (manual/automatic/import), needs_review (Flag für automatisch erkannte,
  noch zu prüfende Einträge)
- **Provider** hat ein "Preis-Gedächtnis": `last_price_ac_per_kwh` /
  `last_price_dc_per_kwh` werden bei jedem Speichern eines Sessions mit
  Preisangabe aktualisiert - beim nächsten Anlegen wird der Preis
  automatisch vorgeschlagen (Web-UI + iOS-App beide implementiert)
- **ChargingLocation** hat lat/lon + Radius + `default_provider_id` -
  Geo-Matching (Haversine-Formel in `routers/locations.py`) ordnet
  automatisch erkannte Sessions einem bekannten Ladeort zu
- **`ChargingSession.geocoded_place`**: Fallback-Ortstext (z.B. "Leonberg,
  Baden-Württemberg"), wird serverseitig per Offline-Reverse-Geocoding
  (`geocode.py`, Package `reverse_geocoder`) gesetzt, wenn Koordinaten
  vorhanden sind, aber **kein** bekannter `ChargingLocation`-Eintrag matcht.
  Bewusst offline/kein externer API-Call, um bei der "kein Cloud-Dienst"-Linie
  zu bleiben - dafür nur Orts-/Stadtebene, keine Adress-/POI-Genauigkeit.
  Web-UI zeigt Priorität: `location.name` > `geocoded_place` > Notiz-Parsing.
- **Adresssuche beim Anlegen von Ladeorten** (`GET /api/geocode/forward`,
  `geocode.py::forward_geocode`): bewusste EINZIGE Ausnahme von der "kein
  Cloud-Dienst"-Linie - ruft die oeffentliche OSM-Nominatim-API auf, weil
  Ladeort-Koordinaten praezise sein muessen (steuern das radius-basierte
  Geo-Matching gegen echte GPS-Punkte) und eine offline Adresssuche eine
  vollstaendige Strassendatenbank braeuchte. Nur bei Nutzeraktion beim
  Anlegen/Bearbeiten eines Ladeorts, nicht im laufenden Betrieb - daher
  unproblematisch bzgl. Nominatims Rate-Limit (max. 1 req/s). Web-UI
  (`settings.html`) zeigt die Kandidaten zur Bestätigung an statt den
  ersten Treffer blind zu übernehmen (Koordinatenfelder bleiben danach
  weiter manuell editierbar).

## Home-Assistant-Anbindung (aktiv, Nutzer betreibt eigene Automation)

Endpunkt `POST /api/sessions/auto` nimmt Pushes entgegen (Schema
`schemas.AutoSessionPush`: vehicle_external_id, external_session_id
[Duplikatschutz], start_time, end_time, soc_start, soc_end, odometer_km,
latitude, longitude, energy_kwh optional).

Datenquelle: MySkoda-Integration in Home Assistant liefert
`sensor.skoda_enyaq_battery_percentage`, `sensor.skoda_enyaq_charging_state`
(`device_class: enum`, GESCHLOSSENE Werteliste, keine 6. "unplugged"-Option:
connect_cable [Ruhezustand/Standard, KEIN 6. Wert existiert dafür],
ready_for_charging, conserving, charging, charging_interrupted [alle vier
= "verbunden/aktiv"]. Session-Grenze ist der Übergang connect_cable ↔ einer
der vier aktiven Werte, NICHT "wert außerhalb einer Liste" - das war ein
falscher erster Ansatz, der nie ausgelöst hat, weil kein Wert je außerhalb
der 5 `options` liegt), `sensor.skoda_enyaq_mileage`,
`device_tracker.skoda_enyaq_position` (GPS als
Attribute latitude/longitude, nur bei "Position teilen"-Einstellung im
Škoda-Account). Energie-kWh wird bewusst NICHT vom Auto übernommen (MySkoda
liefert das nicht zuverlässig) - stattdessen serverseitige Schätzung aus
SoC-Delta × Akkukapazität.

Umsetzung beim Nutzer: HA-Package (`packages/lademonitor.yaml`, NICHT in
`configuration.yaml` direkt, um die Hauptdatei sauber zu halten) mit
`input_text`/`input_number`-Helpern (SoC/Startzeit merken beim Einstecken),
`rest_command` (direkter HTTP-POST an `/api/sessions/auto`, kein
input_text-Umweg nötig, da eigener Server im selben Netz läuft) und einer
YAML-Automation mit `id:` (bleibt trotzdem nur YAML-editierbar, da nicht in
`automations.yaml` - UI-Editor kann Packages nicht zurückschreiben). Noch
offen: Blueprint-Version für Wiederverwendbarkeit mit anderen Fahrzeugen/
Integrationen (Phase 2 des HA-Themas).

## Verbrauchsberechnung pro Ladevorgang (`consumption.py`)

Da das Fahrzeug fast nie vollgeladen wird, gibt es keinen festen
Referenzpunkt für kWh/100km - daher eine priorisierte Fallback-Kette statt
einer festen Formel. Wird bei jedem Abruf frisch berechnet (NICHT in der DB
gespeichert), damit nachträgliche Korrekturen an SoC/Kilometerstand/kWh sich
automatisch auswirken. Ergebnis landet in `SessionOut.consumption_kwh_per_100km`
+ `consumption_method` (nur Response, nicht in Create/Update-Schemas als Input).

Grundformel (Einzelvorgang N mit chronologischem Vorgänger N-1 desselben
Fahrzeugs): `verbrauchte_energie = geladene_kWh(N) − Akkukapazität ×
(SoC_Ende(N) − SoC_Ende(N-1)) / 100`, `verbrauch = verbrauchte_energie /
(odometer_km(N) − odometer_km(N-1)) × 100`. `geladene_kWh` ist die primäre,
vertrauenswürdige Basis - der SoC-Term ist nur eine Korrektur, bestimmt NICHT
die Größenordnung.

Fallback-Kette (Priorität von oben nach unten, `consumption_method`-Wert):
1. `full_charge_interval` - Vorgang liegt zwischen zwei aufeinanderfolgenden
   Vollladungen (SoC_Ende=100). Goldstandard: Summe aller geladenen kWh im
   Intervall ÷ (odometer_km bei 2. Vollladung − odometer_km bei 1.
   Vollladung) × 100 ist die exakte Summe für das gesamte Intervall. Gilt für
   ALLE Vorgänge im Intervall, nicht nur die Vollladung selbst. Innerhalb des
   Intervalls wird NICHT mehr derselbe Durchschnittswert an alle Vorgänge
   verteilt, sondern individuell kalibriert (`consumption.py::_compute_interval`):
   pro Vorgang eine SoC-korrigierte Einzelschätzung (Formel wie Methode 2),
   die Abweichung zur bekannten Intervall-Summe wird km-gewichtet auf alle
   Vorgänge im Intervall verteilt, sodass die Einzelwerte wieder exakt zur
   bekannten Summe aufsummieren. Bei vollständiger SoC-Kette ist die
   Abweichung mathematisch immer exakt 0 (Teleskopsumme) - die Kalibrierung
   greift nur sichtbar, wenn einzelne Vorgänge im Intervall keine SoC-Werte
   haben. Fehlt bei einem Vorgang im Intervall der Kilometerstand (Korrektur
   nicht möglich), fällt das gesamte Intervall sicher auf den einheitlichen
   Durchschnittswert zurück.
2. `soc_corrected` - `energy_kwh` ist gemessen (`energy_is_estimated=false`),
   SoC_Ende(N) und SoC_Ende(N-1) sowie die Akkukapazität des Fahrzeugs sind
   bekannt → Korrekturformel oben.
3. `naive` - nur `energy_kwh` (gemessen) bekannt, keine SoC-Korrektur möglich
   (SoC oder Akkukapazität fehlt) → einfache kWh/km-Rechnung ohne Korrektur.
4. `estimated_energy` - `energy_kwh` ist selbst nur geschätzt
   (`energy_is_estimated=true`) → gleiche Rechnung wie 2/3, aber eigener
   Method-Tag, weil sich Schätzfehler potenzieren können.
5. `unavailable` - kein Vorgänger, `odometer_km` fehlt bei N/N-1, `odometer_km`
   nicht aufsteigend, oder `energy_kwh` fehlt → `consumption_kwh_per_100km: null`.

Web-UI (`sessions.html`) zeigt Wert + Icon je Methode (🎯 full_charge_interval,
✓ soc_corrected, ~ naive/estimated_energy, – unavailable) mit Tooltip.
**iOS-App wurde bewusst NICHT angepasst** (Dateien können vom hiesigen Stand
abweichen, siehe unten) - für eine spätere Anpassung sind
`consumption_kwh_per_100km: Double?` und `consumption_method: String?` in
`SessionOut`/`Models.swift` relevant, plus optional dieselbe
Icon/Tooltip-Logik in der SessionsList-View.

## Bekannte offene Punkte / TODOs

- **Keine Authentifizierung** im Backend - für Heimnetz okay, muss vor
  Zugriff von außerhalb (auch via VPN grundsätzlich vertretbar, aber
  idealerweise zusätzlich absichern) nachgerüstet werden
- **AC/DC fehlt bei Spritmonitor-Importen** - Spritmonitor-CSV-Export hat
  keine AC/DC-Spalte. Noch nicht umgesetzt: Muster-Erkennung anhand
  Ladeort-Name (z.B. "HPC"/"Ionity" → DC vorschlagen)
- **Spritmonitor-Spaltennamen** (`COLUMN_ALIASES` in `importer.py`) sind
  gegen die reale Export-Datei des Nutzers verifiziert (deutsche Exports:
  Datum, Km-Stand, Spritmenge, Kosten, Tankstelle, Ladezustand). Bei anderen
  Sprachen/Fahrzeugtypen ggf. weitere Aliase nötig
- **iOS-App: Fahrzeug kann beim Bearbeiten nicht gewechselt werden** (by
  design, da `SessionUpdate`-Schema kein vehicle_id-Feld hat)
- **Xcode-Beta-Umgebung des Nutzers:** macOS 27 Beta + Xcode 27 Beta
  (Erstbeta, Stand Aug 2026). Es gab einen `dyld_shared_cache_extract_dylibs`
  Bug beim Installieren auf echtem Gerät - gelöst durch Löschen von
  `~/Library/Developer/Xcode/iOS DeviceSupport/` und Neu-Pairing. Nutzer hat
  KEIN kostenpflichtiges Apple Developer Program - Distribution/Archive-Export
  funktioniert deshalb nicht, nur direktes Xcode-Run-Install
- **iOS-App-Dateien können vom Stand hier abweichen** - Nutzer hat bereits
  eigenständig über die "Claude in Xcode"-Integration Änderungen vorgenommen
  (u.a. Dashboard kWh-Chart ergänzt, Ladevorgänge-Zeilen komplett antippbar
  gemacht). Vor Änderungen am iOS-Code immer erst den aktuellen Datei-Stand
  einlesen, nicht blind überschreiben.

## Statistik-Endpunkt-Konventionen (`/api/stats/summary`)

- `avg_price_per_kwh` = gewichteter Durchschnitt (Gesamtkosten ÷ Gesamt-kWh),
  bewusst NICHT der einfache Mittelwert der Einzelpreise
- `price_per_100km` und `avg_consumption_kwh_per_100km` werden aus der
  Differenz von erstem/letztem bekanntem Kilometerstand berechnet (nicht aus
  allen Sessions einzeln aufsummiert, um Messfehler zu glätten)
- `monthly` Liste ist **absteigend sortiert** (neuester Monat zuerst)
- `total_km_driven` = Differenz erster/letzter bekannter Kilometerstand

## Backup-Export/-Import (`routers/backup.py`)

Reiner Backup/Restore-Mechanismus (z.B. Server-Neuaufsetzung), bewusst KEIN
flexibles Datenaustauschformat - unterscheidet sich vom Spritmonitor-Importer
(`importer.py`, eigenes CSV-Format mit Spaltenerkennung).

- `GET /api/backup/export`: liefert eine ZIP mit `README.txt` +
  `vehicles.csv`/`providers.csv`/`locations.csv`/`sessions.csv` (feste
  Dateinamen). Foreign Keys bleiben als Original-UUIDs erhalten (robust fuer
  den Reimport); zusaetzlich gibt es rein lesbare Spalten wie `vehicle_name`,
  `provider_name`, `location_name`, `default_provider_name` (werden beim
  Import ignoriert, koennen veraltet sein).
- `POST /api/backup/import`: erwartet exakt die vom Export erzeugte
  ZIP-Struktur (alle vier CSVs muessen vorhanden sein). Import-Reihenfolge
  wegen FKs: Vehicles → Providers → Locations → Sessions. Datensaetze werden
  per Original-ID wiederhergestellt; bereits vorhandene IDs werden
  uebersprungen (kein Ueberschreiben) - dadurch ist der Import idempotent
  und gefahrlos mehrfach ausfuehrbar.
- Web-UI: neue Sektion "Daten-Backup" unten in `settings.html`.
- Kein flexibles Multi-File-Upload mit Datei-Erkennung (bewusste
  Design-Entscheidung) - falls spaeter einzelne Tabellen unabhaengig
  im-/exportiert werden sollen, braeuchte es echte Spaltenerkennung wie beim
  Spritmonitor-Importer.

## Deployment-Hinweise

- Nach Datei-Änderungen im Backend: kompletten Ordner ersetzen, nicht nur
  einzelne Dateien (führte in der Vergangenheit zu Versions-Mismatches
  zwischen Templates und Router-Code)
- Bei hartnäckigen Docker-Cache-Problemen: `docker compose build --no-cache backend`
- `docker-compose.yml` hat bewusst kein `version:`-Feld mehr (obsolet in
  neueren Compose-Versionen, erzeugt sonst eine Warnung)
