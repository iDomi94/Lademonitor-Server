# Lademonitor – Projektkontext

Selbstgehostete Ladevorgang-Tracking-App für ein E-Auto (Škoda Enyaq via
MySkoda-Integration), inspiriert von Spritmonitor. Läuft komplett selbstgehostet
auf Unraid (Docker), kein Cloud-Dienst.

## Architektur-Entscheidung (wichtig für den Kontext)

Web-UI und iOS-App sind beide Clients gegen dieselbe REST-API, die
**Datenbank liegt zentral auf dem Server** (Unraid) - das bleibt unveraendert.

**Update 2026-08-16 (kippt einen Teil der urspruenglichen Entscheidung):**
Urspruenglich war die iOS-App bewusst **online-only** (kein lokales Speichern,
keine Sync-Schicht), weil der Nutzer ohnehin staendig mit Unraid+HA verbunden
ist und eine Sync-Schicht als unnoetige Komplexitaet galt. Das wurde bewusst
revidiert: die iOS-App bekommt jetzt einen **Local-Only-Modus** (SwiftData,
komplett ohne Server nutzbar) als Alternative zum bisherigen Server-Modus, mit
Moduswahl beim ersten Start statt erzwungenem Login. Grund: Nutzung auch ganz
ohne eigene Server-Infrastruktur ermoeglichen. Details/Architektur siehe
`ios/Lademonitor/CLAUDE.md` (falls vorhanden) bzw. die Local-Only-Implementierung
in `ios/Lademonitor/Repositories/` und `ios/Lademonitor/Models/LocalModels.swift`.
Der Server-Modus selbst bleibt online-only wie bisher; geplant (noch nicht
umgesetzt) ist ein Sync-Service, der beim Wechsel Local-Only -> Server lokale
Daten hochlaedt und im Server-Modus bei kurzzeitigem Verbindungsverlust
puffert.

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
    geocoding.py, backup.py, auth.py
  auth.py            - Passwort-Hashing, Token-Handling, Auth-Dependencies
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

## Authentifizierung (`auth.py`, `routers/auth.py`)

Ein Mechanismus fuer alle drei Clients (Web-UI, iOS-App, Home-Assistant-
`rest_command`): opake, zufaellige Tokens (`secrets.token_urlsafe(32)`) in
der DB-Tabelle `auth_tokens` (Modell `AuthToken`) statt JWT - einfacher zu
widerrufen (Logout = Zeile loeschen), keine Signatur-/Ablauf-Logik noetig.
Werden ausschliesslich als `Authorization: Bearer <token>`-Header ODER als
httponly-Cookie (`session_token`) akzeptiert (`auth.get_current_user`, per
`dependencies=[Depends(get_current_user)]` auf jeden bestehenden Router in
`main.py` angewendet - nicht einzeln pro Endpunkt, um die Routen selbst
unangetastet zu lassen). Tokens laufen **bewusst nicht ab** - Home Assistant
kann nicht interaktiv neu einloggen, ein ablaufendes Token wuerde die
Automation regelmaessig kaputt machen. Widerruf nur ueber Logout oder
Loeschen des Nutzers durch einen Admin (kaskadiert auf dessen Tokens).

Registrierung ist bewusst offen (jeder mit der URL kann sich ein Konto
anlegen) - der **erste jemals registrierte Nutzer wird automatisch Admin**
(`routers/auth.py::register`, prueft `User`-Tabelle auf Leerheit). Admins
sehen in den Einstellungen eine Nutzerverwaltung (`GET/DELETE
/api/auth/users`) und koennen Konten loeschen, aber sich nicht selbst
loeschen (Sperre gegen versehentliches Aussperren).

Passwoerter mit `bcrypt` gehasht (kein passlib, direkte Nutzung des
`bcrypt`-Pakets reicht). HTML-Seiten (`main.py::_page`) leiten bei
fehlendem/ungueltigem Cookie zu `/login` um (303), JSON-API-Endpunkte
antworten mit 401 (`auth.get_current_user` wirft `HTTPException`).

Cookie hat `secure=True` (seit die App ueber Nginx per HTTPS oeffentlich
erreichbar ist) - direkter Zugriff per `http://<lan-ip>:8111` funktioniert
fuers Web-UI-Login seitdem NICHT mehr (Browser sendet ein secure-Cookie nur
ueber HTTPS), nur noch ueber die echte Domain. Betrifft nur die
Browser-Cookie-Session, NICHT den Bearer-Token-Weg (iOS-App, Home-Assistant-
`rest_command`) - der funktioniert unabhaengig vom Schema weiter.

**iOS-App wurde bewusst NICHT angepasst** (siehe Hinweis unten zu
abweichenden iOS-Dateien) - fuer eine spaetere Anpassung: Login-Screen,
Token in Keychain speichern, `Authorization: Bearer <token>`-Header auf
allen Requests (`APIClient.swift`).

### Pro-Nutzer-Datentrennung

Bewusste Entscheidung (nicht der erste Entwurf!): **jeder Nutzer hat seinen
eigenen, komplett isolierten Datensatz** - keine geteilten Fahrzeuge/Ladeorte/
Anbieter/Sessions zwischen Konten, trotz offener Registrierung. Grund: Ohne
das koennte sich theoretisch jeder mit der URL registrieren und saehe sofort
die echten Ladeorte (inkl. GPS-Koordinaten von z.B. Zuhause) und das
Fahrverhalten des Admins - ein echtes Datenschutz-Thema, seit die App
oeffentlich per Nginx erreichbar ist.

Umsetzung: `user_id`-FK (nullable) auf `Vehicle`, `Provider`,
`ChargingLocation` UND `ChargingSession` (bei Sessions denormalisiert statt
nur ueber `vehicle.user_id` ableitbar, fuer einfache WHERE-Filter ohne Joins
in jedem Router). Jeder Router filtert Listen/Get/Update/Delete nach
`user_id == current_user.id` (404 statt 403 bei Fremdzugriff, um nicht zu
verraten dass eine ID existiert). `match_location()` (`routers/locations.py`)
und `resolve_location()`/`attach_consumption()` (`routers/sessions.py`)
bekommen dafuer explizit die `user_id` mitgegeben statt global zu suchen.

`Vehicle.external_id` und `Provider.name` waren vorher GLOBAL eindeutig
(`unique=True`) - jetzt zusammengesetzter Unique-Constraint `(user_id, *)`,
damit zwei Nutzer beide ein Fahrzeug "enyaq" nennen koennen. Die Migration
dafuer in `database.py::run_light_migrations()` ist nicht trivial (Postgres
kennt kein `ADD CONSTRAINT IF NOT EXISTS`, und der alte
`Vehicle.external_id`-Constraint ist ein UNIQUE INDEX (`ix_vehicles_...`),
der alte `Provider.name`-Constraint dagegen ein table-level UNIQUE CONSTRAINT
(`providers_name_key`) - unterschiedliche DROP-Befehle noetig, empirisch mit
echtem Postgres verifiziert statt geraten.

**Backfill bestehender Daten:** Alle Zeilen ohne `user_id` (aus der Zeit vor
Multi-User) werden beim Migrationslauf automatisch dem **ersten jemals
registrierten Nutzer** zugeordnet (`SELECT id FROM users ORDER BY created_at
LIMIT 1`) - das ist zuverlaessig der Admin, der die "alten" Daten ohnehin
als seine eigenen ansieht. Auf einer komplett neuen Installation (noch kein
Nutzer registriert) ist das UPDATE ein No-Op.

**Wichtige Konsequenz fuer Home Assistant:** Der `rest_command`-Token muss
zu dem Nutzer gehoeren, der das jeweilige Fahrzeug besitzt - bei einer
bestehenden Installation ist das der **Admin** (der die Bestandsdaten geerbt
hat), NICHT ein separates, leeres HA-Konto. Ein dediziertes HA-Konto haette
kein eigenes Fahrzeug und `POST /api/sessions/auto` wuerde 404 liefern.

**Bekannte Einschraenkung:** `Provider`/`ChargingLocation` sind jetzt
vollstaendig pro Nutzer getrennt, nicht "haushaltsweit geteilt" - falls
mehrere Personen dasselbe Auto/denselben Ladeort nutzen sollen, muesste
jede Person ihre eigenen Ladeorte/Anbieter anlegen (kein Teilen-Mechanismus
vorhanden).

## Home-Assistant-Anbindung (aktiv, Nutzer betreibt eigene Automation)

**Update 2026-08-23:** Die weiter unten beschriebene rest_command/Token-per-
Hand-Loesung ist die urspruengliche, weiterhin funktionierende Variante -
empfohlen wird inzwischen aber die neue HACS-Integration
[Lademonitor-HA](https://github.com/iDomi94/Lademonitor-HA) (separates Repo):
Config Flow fragt Zugangsdaten einmalig ab, Token-Handling (inkl. Re-Login
bei Invalidierung) laeuft intern, Push passiert ueber den HA-Service
`lademonitor.push_charging_session` statt `rest_command`. Ausserdem bringt
sie Statistik-Sensoren (Kosten/Verbrauch/km) als Pull-Richtung mit, die es
vorher gar nicht gab. Ergaenzend dazu
[Lademonitor-HA-Addon](https://github.com/iDomi94/Lademonitor-HA-Addon) fuer
den Betrieb des Servers selbst als HA-Supervisor-Add-on (bundled Postgres,
Submodule auf dieses Repo, `/data` statt `/config`). Damit ist das weiter
unten erwaehnte "Phase 2 des HA-Themas" (Blueprint-Version) obsolet - die
Integration loest dasselbe Problem allgemeiner (funktioniert nicht nur mit
YAML-Blueprints, sondern mit jeder normalen Automation).

**Seit Einfuehrung der Authentifizierung braucht `rest_command` einen
`Authorization: Bearer <token>`-Header**, sonst antwortet der Endpunkt mit
401. Wegen der Pro-Nutzer-Datentrennung (siehe Abschnitt "Authentifizierung"
oben) MUSS das der Token des Nutzers sein, dem das Fahrzeug gehoert -
praktisch also der Admin-Account, nicht ein separates HA-Konto (fruehere,
inzwischen ueberholte Empfehlung - ein leeres HA-Konto haette kein Fahrzeug
und der Push wuerde 404 liefern). Einmalig per `POST /api/auth/login`
einloggen und das zurueckgegebene `token` statisch in den
`rest_command`-Header eintragen (Tokens laufen nicht ab).

Endpunkt `POST /api/sessions/auto` nimmt Pushes entgegen (Schema
`schemas.AutoSessionPush`: vehicle_external_id, external_session_id
[Duplikatschutz], start_time, end_time, charging_type, soc_start, soc_end,
odometer_km, latitude, longitude, energy_kwh optional).

Datenquelle: MySkoda-Integration in Home Assistant liefert
`sensor.skoda_enyaq_battery_percentage`, `sensor.skoda_enyaq_charging_state`
(`device_class: enum`, GESCHLOSSENE Werteliste, keine 6. "unplugged"-Option:
connect_cable [Ruhezustand/Standard, KEIN 6. Wert existiert dafür],
ready_for_charging, conserving, charging, charging_interrupted [alle vier
= "verbunden/aktiv"]. Session-Grenze ist der Übergang connect_cable ↔ einer
der vier aktiven Werte, NICHT "wert außerhalb einer Liste" - das war ein
falscher erster Ansatz, der nie ausgelöst hat, weil kein Wert je außerhalb
der 5 `options` liegt), `sensor.skoda_enyaq_mileage`,
`sensor.skoda_enyaq_charge_type` (liefert `ac`/`dc` kleingeschrieben -
`schemas.AutoSessionPush._normalize_charging_type` uppercased das vor der
Validierung gegen `models.ChargingType`; **war bis 2026-08-15 vergessen**,
automatisch importierte Sessions hatten dadurch bislang immer
`charging_type: null` und bekamen ueber `apply_provider_price()` faelschlich
immer den AC-Preis des Anbieters vorgeschlagen statt des DC-Preises).
**Faellt bei Ladeende oft schon auf `unknown` zurueck, bevor die Automation
feuert** (Timing-Problem, nicht Backend-seitig loesbar) - deshalb zwei
Absicherungen: 1) die HA-Automation merkt sich den Wert per
`input_text.enyaq_charge_type` beim Einstecken (wie SoC-Start/Startzeit)
statt ihn beim Ladeende live zu lesen, 2) `_normalize_charging_type` wirft
bei einem trotzdem ungueltigen Wert (z.B. `unknown`, falls der
`input_text`-Helper leer ist) KEINEN 422-Fehler, sondern faellt still auf
`None` zurueck - der Push soll nie komplett verworfen werden, nur weil die
Lade-Art fehlt, das faengt `needs_review` beim manuellen Nachtragen ab.
`device_tracker.skoda_enyaq_position` (GPS als
Attribute latitude/longitude, nur bei "Position teilen"-Einstellung im
Škoda-Account). Energie-kWh wird bewusst NICHT vom Auto übernommen (MySkoda
liefert das nicht zuverlässig) - stattdessen serverseitige Schätzung aus
SoC-Delta × Akkukapazität.

Umsetzung beim Nutzer: HA-Package (`packages/lademonitor.yaml`, NICHT in
`configuration.yaml` direkt, um die Hauptdatei sauber zu halten) mit
`input_text`/`input_number`-Helpern (SoC/Startzeit/Lade-Art merken beim
Einstecken - Werte, die beim Ladeende schon wieder unbekannt/zurueckgesetzt
sein koennen, MUESSEN so zwischengespeichert werden statt live gelesen zu
werden), `rest_command` (direkter HTTP-POST an `/api/sessions/auto`, kein
input_text-Umweg fuer den REST-Call selbst noetig, da eigener Server im
selben Netz läuft) und einer
YAML-Automation mit `id:` (bleibt trotzdem nur YAML-editierbar, da nicht in
`automations.yaml` - UI-Editor kann Packages nicht zurückschreiben). Die
urspruenglich hier als "Phase 2" geplante Blueprint-Version fuer
Wiederverwendbarkeit ist erledigt, nur anders geloest als angedacht: siehe
Update 2026-08-23 oben - der `lademonitor.push_charging_session`-Service aus
Lademonitor-HA ersetzt den `rest_command`-Aufruf, die restliche Automation
(Trigger + Helfer) bleibt konzeptionell gleich.

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

- **Kein Rate-Limiting auf `/api/auth/login`/`/register`** - seit die App
  oeffentlich per Nginx erreichbar ist, kein Schutz gegen automatisiertes
  Passwort-Raten oder Spam-Registrierungen. Bewusst zurueckgestellt (starkes
  Passwort des Nutzers als aktuelle Absicherung), waere ein separater,
  ueberschaubarer Zusatz (z.B. `slowapi` oder Nginx-seitig).
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
- `ac_share_pct`/`dc_share_pct` sind **kWh-gewichtet**, NICHT Anteil an der
  Vorgangs-ANZAHL (war urspruenglich anders, bewusst umgestellt - ein langer
  AC-Vorgang zaehlte sonst genauso viel wie eine kurze DC-Schnellladung).
  `ac_kwh`/`dc_kwh` liefern zusaetzlich die absoluten Werte fuer die
  Beschriftung des Web-UI-Balkens (kein reiner Prozent-Chart, sondern ein
  zweigeteilter horizontaler Balken/"Linear Gauge")
- `by_provider` (Liste `{provider_name, total_kwh, total_cost}`, absteigend
  nach kWh sortiert): Sessions ohne `provider_id` landen unter
  `"Ohne Anbieter"`, damit die Summen immer vollstaendig bleiben. Web-UI
  gruppiert selbst auf max. 5 Anbieter + "Andere"-Sammelposten und nutzt
  **dieselbe Farbzuordnung** in beiden Kuchendiagrammen (kWh/bezahlt), damit
  ein Anbieter in beiden Charts sofort wiedererkennbar ist ("Farbe folgt der
  Entitaet")
- `monthly[].avg_consumption_kwh_per_100km`: nutzt dieselbe Fallback-Kette
  wie einzelne Ladevorgaenge (`consumption.py`), aber **km-gewichtet** pro
  Monat gemittelt statt naiv pro Session - dafuer wurde `ConsumptionResult`
  um ein `km`-Feld erweitert (nur als Gewicht gedacht, nicht fuer die
  Anzeige). `None` wenn im Monat kein Vorgang einen berechenbaren Wert hat.
  Web-UI zeigt das als vertikales Saeulendiagramm, chronologisch (aeltester
  zuerst) - bewusst anders als die uebrigen Monats-Balkendiagramme, die
  neuester-zuerst zeigen, weil ein Zeitverlauf von links nach rechts gelesen
  werden soll

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

## Deployment-Varianten

Es gibt jetzt ZWEI parallele Deployment-Wege - beide bleiben bestehen, keiner
ersetzt den anderen:

1. **docker-compose.yml (Stack, 2 Container)** - bisheriger Weg fuer lokale
   Entwicklung/Tests: `backend/Dockerfile` (nur FastAPI) + separater
   `postgres:16-alpine`-Container. Nach Datei-Aenderungen im Backend:
   kompletten Ordner ersetzen, nicht nur einzelne Dateien (fuehrte in der
   Vergangenheit zu Versions-Mismatches zwischen Templates und Router-Code).
   Bei hartnaeckigen Cache-Problemen: `docker compose build --no-cache backend`.
   Hat bewusst kein `version:`-Feld mehr (obsolet in neueren
   Compose-Versionen, erzeugt sonst eine Warnung).
2. **Dockerfile (Root, Einzelcontainer)** - fuer Unraid gedacht, EIN
   Container statt Stack: Basis-Image `postgres:16` (nicht `python:3.12-slim`
   wie beim Compose-Weg!), Python wird per `apt-get` nachgeruestet, App laeuft
   in einem venv unter `/venv`. `entrypoint.sh` startet Postgres (delegiert an
   das offizielle `docker-entrypoint.sh`, laeuft im Hintergrund) und danach
   uvicorn im Vordergrund; `wait -n` sorgt dafuer, dass der GANZE Container
   stoppt, sobald einer der beiden Prozesse stirbt - Unraids Restart-Policy
   startet dann beides sauber neu, statt dass die App weiterlaeuft ohne
   funktionierende DB. Bewusst KEIN s6-overlay/Supervisor (Ein-Nutzer-
   Heimnetz-App, das waere unnoetige Komplexitaet). Postgres ist NUR via
   localhost im Container erreichbar, kein zweiter Port noetig - nur EIN
   Port-Mapping (App) + EIN Pfad-Mapping (`/config` = komplettes
   Postgres-Datenverzeichnis unter `/config/postgres`) fuer den Unraid-Nutzer.
   DB-Zugangsdaten sind fest im Dockerfile verdrahtet (nicht von aussen
   erreichbar, daher unkritisch).
   - `unraid-template.xml`: Community-Applications-Template. `<Repository>`
     zeigt aktuell auf einen lokalen Image-Tag (`lademonitor:latest`) - Nutzer
     muss vor der Nutzung selbst `docker build -t lademonitor:latest .` auf
     dem Unraid-Host ausfuehren. Sobald das Projekt auf GitHub/einer Registry
     veroeffentlicht ist (naechstes geplantes Thema), `<Repository>` auf das
     veroeffentlichte Image umstellen, dann entfaellt der manuelle Build-Schritt.
   - **Noch nicht auf echter Hardware getestet** (kein Docker in der
     Umgebung verfuegbar, in der das erstellt wurde) - beim ersten Test auf
     Unraid besonders pruefen: `reverse_geocoder`/`scipy`-Installation
     (`build-essential` ist als Sicherheitsnetz mit drin, falls kein
     vorgebautes Wheel fuer die Ziel-Architektur existiert), und ob `initdb`
     beim allerersten Start sauber durchlaeuft.
