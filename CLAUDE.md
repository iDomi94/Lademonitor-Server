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
**Nachtrag:** der dort als "geplant" notierte Sync-Service existiert inzwischen
in BEIDEN Apps (`Repositories/SyncService.swift` bzw.
`data/repo/SyncService.kt`) - bidirektional, Push vor Pull in
FK-Reihenfolge, Konfliktregel "lokal dirty gewinnt", und der Wechsel
Local-Only -> Server ist dort kein Sonderfall, sondern der erste normale
Durchlauf (jede neue lokale Zeile startet `isDirty=true`/`serverId=nil`).
Seit v0.22.0 kommen serverseitige Loeschungen ueber Grabsteine mit an - siehe
Abschnitt "Sync: Grabsteine fuer geloeschte Datensaetze".

## Tech-Stack

- **Backend:** FastAPI (Python) + PostgreSQL + SQLAlchemy, Docker auf Unraid
- **Web-UI:** Server-rendered Jinja2, KEIN Chart.js/externe CDN-Libs mehr
  (wurden entfernt, weil Client keinen CDN-Zugriff hatte) - Charts sind
  selbstgebaute SVG-Balkendiagramme in reinem JS
- **iOS-App:** SwiftUI + SwiftData, async/await (eigenes Repo
  [Lademonitor-App](https://github.com/iDomi94/Lademonitor-App)). **Nicht mehr
  "reiner REST-Client"** - seit dem Local-Only-Modus (siehe
  Architektur-Entscheidung oben) liegt ein vollstaendiger lokaler Speicher
  darunter, der Server-Modus synchronisiert bidirektional dagegen
  (`Repositories/SyncService.swift`).
- **Android-App:** Kotlin + Jetpack Compose + Room, 1:1-Portierung der iOS-App
  (eigenes Repo
  [Lademonitor-Android](https://github.com/iDomi94/Lademonitor-Android)) -
  gleiche zwei Modi, gleiche Sync-Logik, gleiche Verbrauchs-Fallback-Kette.
- **Tests:** `backend/tests/` (pytest gegen SQLite-in-memory), CI in
  `.github/workflows/tests.yml`. Siehe eigenen Abschnitt "Tests" weiter unten -
  insbesondere, was dort bewusst NICHT abgedeckt ist.
- **Deployment:** `docker compose up -d --build` im Projekt-Root auf Unraid,
  Compose-Projektname/Stack-Name beim Nutzer: "Lademonitor"

## Projektstruktur

```
backend/app/
  models.py          - SQLAlchemy: Vehicle, Provider, ChargingLocation, ChargingSession,
                          DeletedRecord (Grabsteine, siehe Abschnitt "Sync")
  schemas.py          - Pydantic Request/Response-Schemas
  routers/
    vehicles.py, providers.py, locations.py, sessions.py, stats.py, importer.py,
    geocoding.py, backup.py, auth.py, webdav_backup.py, myskoda.py, email.py,
    sync.py
  auth.py            - Passwort-Hashing, Token-Handling, Auth-Dependencies
  sync.py            - record_deletion(): Grabstein fuer eine geloeschte Zeile
  myskoda.py         - Client fuer die offizielle MyŠkoda Public API (sync httpx)
  myskoda_poller.py  - Zustandsmaschine der automatischen Ladeerkennung + Debug-Log
  mailer.py          - SMTP-Versand, Mail-Vorlagen, Versandprotokoll
  notifications.py   - Benachrichtigungen (ereignisgetrieben + zeitgesteuert)
  templates/          - Jinja2 Web-UI (index=Dashboard, sessions, map, settings +
                          die Unterseiten import, settings_backup, settings_api,
                          settings_email; auth_base.html fuer die Seiten ohne
                          Anmeldung; emails/ fuer die Mail-Vorlagen)
  static/style.css, static/filter.js, static/ui.js, static/sw.js,
  static/vendor/leaflet/  - Leaflet 1.9.4 lokal (siehe Abschnitt "Kartenansicht")
backend/tests/       - pytest (siehe Abschnitt "Tests")
```

Die Clients liegen in eigenen Repos, nicht mehr unter `ios/` in diesem Repo:
`Lademonitor-App` (iOS), `Lademonitor-Android`, `Lademonitor-HA` (HACS-
Integration) und `Lademonitor-HA-Addon` (Server als HA-Add-on).

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
**Seit v0.14.0 liegt dort nur noch der SHA-256-Hash** (`token_hash`), nicht
mehr der Token selbst - Begruendung und Migration siehe den E-Mail-Abschnitt
weiter unten. Ein Reset oder eine eigene Passwortaenderung loescht seitdem ALLE
Tokens des Nutzers; HA und iOS muessen sich danach neu anmelden.
Werden ausschliesslich als `Authorization: Bearer <token>`-Header ODER als
httponly-Cookie (`session_token`) akzeptiert (`auth.get_current_user`, per
`dependencies=[Depends(get_current_user)]` auf jeden bestehenden Router in
`main.py` angewendet - nicht einzeln pro Endpunkt, um die Routen selbst
unangetastet zu lassen). Tokens laufen **bewusst nicht ab** - Home Assistant
kann nicht interaktiv neu einloggen, ein ablaufendes Token wuerde die
Automation regelmaessig kaputt machen. Widerruf nur ueber Logout oder
Loeschen des Nutzers durch einen Admin (kaskadiert auf dessen Tokens).

Angemeldet wird mit **Nutzername ODER E-Mail-Adresse** (seit v0.14.0, das
API-Feld heisst der Kompatibilitaet wegen weiterhin `username`).

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

**Update 2026-08-24 (Bugfix, gefunden beim ersten echten CA-Test):** Cookie
hatte `secure=True` fest verdrahtet (urspruenglich fuer den eigenen
Nginx-HTTPS-Reverse-Proxy des Nutzers gesetzt) - das brach den Login fuer
JEDEN direkten HTTP-Zugriff (Unraid-CA-Standardfall `http://<ip>:8111`,
Docker, HA-Add-on): der Login-POST antwortete 200 OK, aber der Browser
verwirft ein Secure-Cookie ueber reines HTTP still, also kam nie ein Cookie
an - sah aus wie ein Redirect-Loop zurueck zu `/login` nach augenscheinlich
erfolgreichem Login. Jetzt dynamisch in `routers/auth.py::_is_https()`:
`secure=True` nur wenn `request.url.scheme == "https"` ODER ein
vorgeschalteter Reverse-Proxy `X-Forwarded-Proto: https` sendet (uebliches
Nginx-Verhalten) - damit funktioniert sowohl der direkte HTTP-Zugriff als
auch das eigene HTTPS-Setup des Nutzers weiter. Betrifft nur die
Browser-Cookie-Session, NICHT den Bearer-Token-Weg (iOS-App, Home-Assistant-
`rest_command`) - der funktioniert unabhaengig vom Schema weiter.

**iOS-App wurde bewusst NICHT angepasst** (siehe Hinweis unten zu
abweichenden iOS-Dateien) - fuer eine spaetere Anpassung: Login-Screen,
Token in Keychain speichern, `Authorization: Bearer <token>`-Header auf
allen Requests (`APIClient.swift`).

**Update 2026-08-25 (Web-UI ingress-faehig gemacht, v0.9.1):** Alle
serverseitig gerenderten Seiten/Redirects/`fetch()`-Aufrufe in
`templates/` und `main.py::_page()` nutzen jetzt relative statt absolute
Pfade - noetig, damit die App auch unter dem Home-Assistant-Ingress-
Unterpfad (`/api/hassio_ingress/<token>/...`) funktioniert, nicht nur am
Domain-Root. Ein Redirect auf einen absoluten Pfad wie `/login` fuehrte
dort auf die HA-Wurzel statt in den Add-on-Bereich (404). Da alle Seiten/
Endpunkte genau eine Ebene unter der jeweiligen Basis liegen, loesen
relative Pfade unter Ingress UND am bisherigen Domain-Root (Unraid+Nginx)
gleichermassen korrekt auf. Zusaetzlich neue Middleware
(`main.py::_no_cache_html`) setzt `Cache-Control: no-store` auf alle
HTML-Antworten - ein Nutzer sah nach einem Rebuild ueber seinen eigenen
Nginx-Reverse-Proxy die App erst nach manuellem Leeren des Browser-Caches
wieder erreichbar, plausibelste Erklaerung war eine vom Browser
zwischengespeicherte, veraltete Seite (kein Cache-Control-Header vorhanden
gewesen).

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

## MyŠkoda Public API (`myskoda.py`, `myskoda_poller.py`, `routers/myskoda.py`)

**Neu 2026-08-31.** Zweite Quelle fuer `source=AUTOMATIC`-Ladevorgaenge,
bewusst PARALLEL zum HA-Push oben und nicht als Ersatz: wer Home Assistant
betreibt, faehrt mit dem Push weiter besser (Push statt Polling, kein
Rate-Limit); der Server-Weg existiert fuer alle ohne HA. **Beide Quellen
wissen nichts voneinander** - pro Fahrzeug darf nur eine aktiv sein, sonst
entstehen Dubletten (die `external_session_id`-Praefixe unterscheiden sich:
HA liefert eine eigene, der Poller nutzt `myskoda-<Vehicle.id>-<Startzeit>` -
**Update 2026-09-09:** urspruenglich `myskoda-<FIN>-<Startzeit>`, auf die
interne Fahrzeug-UUID umgestellt, als die VIN im Zuge der DSGVO-Pruefung
verschluesselt wurde (siehe Abschnitt "Verschluesselung personenbezogener
Daten") - `external_session_id` braucht fuer den Dublettencheck einen
exakten SQL-Gleichheitsvergleich und kann deshalb selbst nicht verschluesselt
werden, hier waere die VIN sonst trotz verschluesselter `vin`-Spalte wieder
im Klartext gelandet. `Vehicle.id` ist ebenso pro Fahrzeug eindeutig, aber
kein personenbezogenes Datum).

Vorerst nur ueber die Web-UI konfigurierbar (Einstellungen), die Endpunkte
sind aber regulaerer Teil der REST-API, damit die iOS-App das ohne
Serveraenderung uebernehmen kann.

### Zwei Eigenschaften der API bestimmen das ganze Design

1. **Rate-Limit 20 Anfragen/Stunde PRO API-KEY**, geteilt ueber alle Fahrzeuge
   und alle Clients. Deshalb adaptives Polling: `poll_interval_idle_minutes`
   (Standard 20 -> 3/h) und `poll_interval_active_minutes` (Standard 5 ->
   12/h), Untergrenze im Schema 3 Minuten. Die `RateLimit-*`-Header werden
   ausgewertet; bei <= `RATE_LIMIT_RESERVE` Restanfragen wird bis zur Freigabe
   pausiert. Laeuft parallel die HA-Integration
   (`homeassistant-myskoda-public-api`) mit demselben Key, teilen sich beide
   das Kontingent - dann zweiten Key erzeugen.
2. **Reines Polling, kein Push, kein Historie-Endpunkt.** Es gibt genau einen
   lesenden Aufruf (`GET /api/v1/vehicles/{vin}`); ein "Ladevorgang" entsteht
   erst dadurch, dass der Server die Zustandsuebergaenge zusammensetzt - genau
   das, was sonst die HA-Automation macht. Die Antwort ist ein
   zwischengespeicherter Cloud-Stand mit eigenem `carCapturedTimestamp`, der
   bei schlafendem Fahrzeug Stunden bis Tage alt sein kann (im echten
   Test-Fixture der HA-Integration liegen `charging` und `odometer` einen Tag
   auseinander). Deshalb stammen ALLE Zeitstempel aus `carCapturedTimestamp`,
   nicht aus der lokalen Uhr.

### Schnitt eines Ladevorgangs

Deckungsgleich mit dem HA-Blueprint, damit beide Quellen dasselbe unter
"Ladevorgang" verstehen: Beginn = Uebergang "nicht verbunden"
(`CONNECT_CABLE`) -> "verbunden" (`CHARGING`, `CONSERVING`,
`READY_FOR_CHARGING`, `CHARGING_INTERRUPTED`), Ende = umgekehrt.
`DISCHARGING` (V2H/V2G) zaehlt als "nicht verbunden" und erzeugt keinen
Vorgang. Liefert eine Antwort gar keinen Ladezustand, wird bewusst NICHTS
entschieden (ein laufender Vorgang wuerde sonst faelschlich beendet).

### Bekannte Ungenauigkeiten (bewusst so, nicht uebersehen)

- **Der Ladebeginn wird erst beim naechsten Abruf bemerkt.** Seit 2026-09-06
  wird er zurueckdatiert, siehe eigenen Abschnitt unten - vorher war
  `soc_start` immer der SoC beim ERSTEN Abruf mit steckendem Kabel.
- `soc_end` ist `max()` ueber alle Beobachtungen waehrend des Steckens, nicht
  der Wert beim Ausstecken - sonst wuerde eine Vorklimatisierung aus der
  Batterie oder ein Stueck Fahrt vor dem naechsten Abruf den Wert druecken.
- **Ein schlafendes Fahrzeug kann einen ganzen Vorgang verstecken.** Dagegen
  `detect_missed_sessions`: steigt der SoC zwischen zwei Abrufen um
  >= `missed_session_min_soc_delta` (Standard 5), ohne dass je ein
  "verbunden"-Zustand gesehen wurde, wird nachtraeglich ein Vorgang angelegt
  (Start/Ende = die beiden Abrufe, Lade-Art unbekannt). Schwelle nicht zu
  klein waehlen - Rekuperation hebt den SoC auf langer Gefaellestrecke
  ebenfalls um ein paar Prozentpunkte.
- **Kabel steckte, aber es wurde nie geladen** (Zielladestand schon erreicht):
  SoC-Delta <= 0 UND nie Leistung > 0 -> kein Ladevorgang, nur ein
  `session_discarded`-Logeintrag.
- Es gibt **keinen Energiezaehler** in der API. `energy_kwh` bleibt wie beim
  HA-Weg die serverseitige Schaetzung aus SoC-Delta x Akkukapazitaet.

### Rueckdatierung des Ladebeginns (2026-09-06, kippt eine fruehere Entscheidung)

Urspruenglich war `soc_start` immer der SoC beim ersten Abruf mit steckendem
Kabel, mit der Begruendung "der SoC des vorherigen Abrufs waere falsch in die
andere Richtung, wenn das Fahrzeug zwischendurch gefahren ist". Das war nur zur
Haelfte richtig und ist bewusst revidiert - **Fahren senkt den SoC**. Solange
nicht geladen wird, ist der SoC monoton fallend, also gilt immer
`soc_echt <= min(open_soc_before, open_soc_start)`: der vorherige Wert kann nie
UNTER dem echten Startwert liegen, schlimmstenfalls (lange Fahrt dazwischen)
ist er zu wenig korrigiert. Der einzige Fall, in dem er wirklich in die falsche
Richtung zeigt, ist `open_soc_before > open_soc_start` - und der ist trivial
erkennbar.

Ausloeser war die im TODO oben geforderte Auswertung des Debug-Logs nach den
ersten echten DC-Ladevorgaengen (05./06.09.2026): erfasst wurden 30 %->77 % und
64 %->80 %, tatsaechlich waren es 8 %->77 % und 48 %->80 % - rund 17 bzw.
12 kWh, die ueber `estimate_energy_kwh` direkt in Energie, Kosten und
Verbrauchsstatistik gefehlt haben. Der jeweils letzte Abruf davor stand
punktgenau auf 8 % bzw. 48 %; beim zweiten sogar, obwohl dazwischen noch 5 km
gefahren wurden (der 15 min alte Wert enthielt die Fahrt schon). Genau deshalb
wird die Fahrstrecke (`odometer`) NICHT zusaetzlich abgezogen - das wuerde
ueberkorrigieren.

`_backdated_start()` in `myskoda_poller.py` setzt `soc_start` deshalb auf
`open_soc_before`, unter zwei Waechtern (ohne sie waere ein unbemerkter
Ladevorgang zwischen den Abrufen - fahren, woanders laden, heimkommen,
einstecken - als Zuwachs DIESES Vorgangs gezaehlt worden):

1. **Alter** (`backdate_max_gap_minutes`, 0 = automatisch das Doppelte des
   Leerlaufintervalls): ist der Abruf davor aelter, kann zu viel passiert sein.
2. **Physik**: aus `open_max_power_kw` und `vehicle.battery_capacity_kwh` ergibt
   sich die Obergrenze dessen, was in der Luecke geladen worden sein KANN.
   Ohne hinterlegte Akkukapazitaet greift dieser Waechter nicht.

Die **Startzeit** wird aus derselben Physik mitgezogen (die Zeit, die das
nachgetragene SoC-Delta bei der beobachteten Leistung braucht, hoechstens bis
zum vorherigen Abruf) - sonst wird die aus Energie und Dauer abgeleitete
Durchschnittsleistung unphysikalisch: 53 kWh in den gemessenen 19 min waeren
168 kW avg bei 134 kW Peak. Mit Korrektur: 122 kW avg bei 26 min.

Abschaltbar per `backdate_session_start`. Die Rohwerte beider Abrufe stehen
weiterhin in `notes`, die angewandte Korrektur dort und als
`session_backdated`-Zeile im Debug-Log; die Vorgaenge bleiben `needs_review`.

### Zustand liegt in der DB, nicht im Prozess

`MySkodaConfig` ist Konfiguration UND Zustandsspeicher: die `open_*`-Spalten
halten den laufenden, noch nicht abgeschlossenen Vorgang. Grund: ein
Container-Neustart mitten im Laden ist der Normalfall (Update, Host-Reboot)
und wuerde sonst den halben Vorgang verlieren. Dieselbe Ueberlegung wie in
`session_store.py` der Lademonitor-HA-Integration.

### Ablauf und Fehlerbehandlung

Scheduler-Task in `main.py` (`_myskoda_scheduler_loop`, Takt 60 s, ueber
`asyncio.to_thread` wie beim WebDAV-Backup - alles hier ist synchron).
Der kurze Takt erzeugt keine zusaetzlichen API-Anfragen: faellig ist ein
Fahrzeug nur, wenn `next_poll_at` erreicht ist. `poll_vehicle()` wirft
bewusst nie weiter, sondern schreibt `last_status`/`last_error`:
`auth_error` -> 60 min Backoff (ein abgelaufener Key wird erst wieder gueltig,
wenn jemand in der App einen neuen erzeugt; HA hat dafuer einen Reauth-Dialog,
der Server nur die Anzeige in den Einstellungen), `rate_limited` ->
`Retry-After` bzw. 15 min, sonstige Fehler -> 15 min.

`POST .../test` und `POST .../poll` sind bewusst getrennt: der Test macht
einen Abruf OHNE Zustandsmaschine (kann also nie versehentlich einen
Ladevorgang anlegen oder beenden) und gibt die geparste Zusammenfassung
zurueck; `poll` ist die vollwertige Abfrage inkl. Auswertung und funktioniert
auch bei deaktivierter Automatik.

### Debug-Log (`MySkodaLogEntry`)

Existiert, weil das Antwortverhalten der API waehrend eines echten
Ladevorgangs noch nicht bekannt ist. Kleine Zusammenfassungsspalten
(`charging_state`, `soc_percent`, `charge_power_kw`, `captured_at`) machen die
Tabelle in der Web-UI ohne Aufklappen lesbar, `payload` haelt optional die
komplette Rohantwort (separater Endpunkt, damit die Listenansicht leicht
bleibt). Begrenzt auf `LOG_MAX_ENTRIES` (500) Zeilen pro Fahrzeug, JSON-Export
zum Weiterreichen.

### Secrets

`api_key` liegt in der DB wie alle uebrigen Zugangsdaten dieser App
(Auth-Tokens, WebDAV-Passwort) - optional verschluesselt seit 2026-09-09
(`FIELD_ENCRYPTION_KEY`, siehe Abschnitt "Verschluesselung personenbezogener
Daten"), sonst Klartext; kein Secrets-Vault vorhanden, Postgres ist nur
containerlokal erreichbar. **Er wird aber NICHT in die Backup-ZIP exportiert**
(siehe `routers/backup.py`), weil die ZIP typischerweise auf fremdem Speicher
(WebDAV/Nextcloud) landet. Nach einer Neuinstallation muss der Key also neu
eingetragen werden - das steht auch in der README innerhalb der ZIP.

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

- **Rate-Limiting auf `/api/auth/login`/`/register`** - seit die App
  oeffentlich per Nginx erreichbar ist, war das offen (automatisiertes
  Passwort-Raten/Spam-Registrierungen technisch nicht gedrosselt). **Erledigt
  seit 2026-09-14** (Anlass: `lademonitor.cloud`-Launch): `rate_limit.py`
  haelt einen simplen In-Memory-Sliding-Window-Zaehler pro Client-IP (kein
  Redis/slowapi noetig, ein Prozess pro Container reicht) -
  `/login` 20 Versuche/5 min, `/register` 5/Stunde, beide 429 bei
  Ueberschreitung. Client-IP kommt aus `X-Forwarded-For` (gesetzt vom eigenen
  Reverse Proxy), Fallback `request.client.host` ohne Proxy - bei direktem
  Zugriff OHNE eigenen Reverse Proxy davor liesse sich der Header faelschen
  und der Limiter umgehen (Heimnetz-Fall, dort ist das Risiko gering; siehe
  Docstring in `rate_limit.py`). Bleibt eine zusaetzliche Schutzschicht, nicht
  der Ersatz fuer starke, einzigartige Passwoerter (weiterhin kein
  Kontosperren nach N Fehlversuchen o.ae.). **Der Passwort-vergessen-Endpunkt
  hat seit v0.14.0 ein eigenes, getrenntes Limit** (3 pro Konto und Stunde
  plus globale Obergrenze), weil er zusaetzlich Mails ausloest.
- **Nutzername war case-sensitiv eindeutig, Login ignorierte Gross-/
  Kleinschreibung aber schon vorher bei der E-Mail-Adresse.** Inkonsistenz:
  Anmeldung mit Nutzername war weiterhin exakt (`==`), zwei Konten "Domi"/
  "domi" waeren gleichzeitig registrierbar gewesen. **Erledigt seit
  2026-09-14** (gleicher Anlass): Login-Lookup UND Registrierungs-/
  Admin-Anlage-Check laufen jetzt einheitlich ueber `func.lower()`
  (`_username_taken()`, neu neben `_email_taken()`), der DB-Unique-Index
  wechselt von der Rohspalte auf `lower(username)`
  (`uq_users_username_lower`, `database.py`) - gleiches Muster wie
  `uq_users_email_lower`. Migration droppt den alten
  `ix_users_username`-Index (von `unique=True, index=True` auf der Spalte).
- **Auth-Tokens ueberleben einen Passwort-Reset nicht** (siehe E-Mail-Abschnitt):
  danach muessen der Home-Assistant-Token und die iOS-Anmeldung neu eingetragen
  werden. Die saubere Loesung waeren typisierte, benannte API-Tokens
  (`kind: session | api`) mit Widerruf pro Eintrag - noch nicht umgesetzt.
- **Verschluesselung der gespeicherten Zugangsdaten** (SMTP-, WebDAV-Passwort,
  MyŠkoda-Key) waere nur mit einem Schluessel ausserhalb von `/config` sinnvoll,
  also ueber eine Env-Variable im Unraid-Template. Bewusst zurueckgestellt,
  siehe Begruendung im E-Mail-Abschnitt. **Erledigt seit 2026-09-09** (noch am
  selben Tag wie die GPS-/Notizen-Verschluesselung): alle drei Secrets nutzen
  jetzt denselben `FIELD_ENCRYPTION_KEY`-Mechanismus (siehe Abschnitt
  "Verschluesselung personenbezogener Daten") - unproblematisch, weil keines
  davon per SQL gefiltert oder als Unique-Constraint gebraucht wird.
- **Feld-Verschluesselung deckt noch nicht alles ab:** `Vehicle.external_id`,
  `Provider.name`, `User.email` und `User.username` sind mangels
  Blind-Index-Loesung noch Klartext (haengen an SQL-Gleichheitsvergleichen
  und/oder DB-Unique-Constraints, siehe Abschnitt "Verschluesselung
  personenbezogener Daten" fuer die Details je Feld), und der eingebaute
  CSV-Export/das WebDAV-Backup liefern alle verschluesselten Felder
  weiterhin bewusst unverschluesselt (menschenlesbares Format).
- **MyŠkoda-Poller: erledigt bis auf die Rueckdatierung selbst.** Das
  Debug-Log ist inzwischen ueber je einen echten AC- und zwei DC-Ladevorgaenge
  ausgewertet (Stand 06.09.2026, 355 Zeilen) - Zustandsfolge, Nachlauf von
  `carCapturedTimestamp` und `chargeType` verhalten sich wie angenommen. Der
  verschluckte SoC-Anteil am Ladebeginn war mit 22 bzw. 16 Prozentpunkten
  deutlich groesser als befuerchtet; daraus entstand die Rueckdatierung (siehe
  Abschnitt oben). **Offen:** die Rueckdatierung selbst ist bisher nur gegen
  die drei Vorgaenge aus dem Log verifiziert, nicht im Live-Betrieb - der
  naechste echte DC-Vorgang sollte daraufhin geprueft werden, insbesondere ob
  der Physik-Waechter bei einer laenger als erwartet dauernden Luecke greift.
- **Kein Schutz gegen doppelte Erfassung, wenn HA-Push und MyŠkoda-Poller
  gleichzeitig fuer dasselbe Fahrzeug laufen** - bewusst nicht geloest
  (unterschiedliche `external_session_id`-Schemata, ein Abgleich ueber
  Zeitfenster waere Rateraetsel). Dokumentiert in README und Web-UI.
- **AC/DC fehlt bei Spritmonitor-Importen** - Spritmonitor-CSV-Export hat
  keine AC/DC-Spalte. Noch nicht umgesetzt: Muster-Erkennung anhand
  Ladeort-Name (z.B. "HPC"/"Ionity" → DC vorschlagen)
- **Spritmonitor-Spaltennamen** (`COLUMN_ALIASES` in `importer.py`) sind
  gegen die reale Export-Datei des Nutzers verifiziert (deutsche Exports:
  Datum, Km-Stand, Spritmenge, Kosten, Tankstelle, Ladezustand). Bei anderen
  Sprachen/Fahrzeugtypen ggf. weitere Aliase nötig
- **iOS-App: Fahrzeug kann beim Bearbeiten nicht gewechselt werden** (by
  design, da `SessionUpdate`-Schema kein vehicle_id-Feld hat)
- **Ladevorgangs-Liste war auf 200 Eintraege gedeckelt** - `GET /api/sessions`
  hatte `limit=200` als Default und kein Client schickte je einen Wert.
  **Erledigt seit 2026-09-20 (v0.22.0)**, siehe Abschnitt "Paginierung von
  `GET /api/sessions`".
- **Serverseitige Loeschungen erreichten die Apps nie** ("Geisterzeilen").
  **Erledigt seit 2026-09-20 (v0.22.0)** ueber Grabsteine, siehe Abschnitt
  "Sync: Grabsteine fuer geloeschte Datensaetze". **Noch offen davon:** ein
  inkrementeller Delta-Pull auch fuer AENDERUNGEN - haengt daran, dass die
  Verbrauchsberechnung ueber die Kette laeuft und eine Aenderung die Werte der
  Nachbarn mitbewegt, ohne deren `updated_at` anzufassen (Begruendung im selben
  Abschnitt).
- **Keine automatisierten Tests** in keinem der fuenf Repos. **Teilweise
  erledigt seit 2026-09-20 (v0.22.0)**: pytest im Server-Repo, siehe Abschnitt
  "Tests". **Noch offen:** die Migrationen (`run_light_migrations()`, bewusst
  ausgeklammert - Postgres-eigenes SQL), der MyŠkoda-Poller als Ganzes
  (bisher nur `_backdated_start()`), die Leaflet-Logik der Kartenseite, sowie
  Tests in den vier uebrigen Repos (iOS/Android/HA/HA-Addon haben keine).
- **Web-Oberflaeche war nicht installierbar**, obwohl der Changelog von
  "PWA-Installation" sprach - Manifest und Service Worker fehlten schlicht.
  **Erledigt seit 2026-09-20 (v0.22.0)**, siehe Abschnitt "PWA".
- **`CLAUDE.md` lief dem Code hinterher** (Tech-Stack nannte die iOS-App noch
  einen "reinen REST-Client", die Android-App kam gar nicht vor, die
  TODO-Liste endete bei v0.14 bei damals v0.21.0). **Aufgeraeumt 2026-09-20.**
  Diese Datei ist die einzige Wissensquelle fuer das Projekt - sie
  mitzupflegen gehoert zu jedem groesseren Schritt, nicht in einen spaeteren
  Aufraeumlauf.
- **Aussentemperatur: noch keine Werte in Bestandsdaten.** Die Auswertung
  (siehe Abschnitt "Verbrauch nach Aussentemperatur") beginnt bei null und
  wird erst ueber die Monate belastbar - fuer alte Vorgaenge laesst sich der
  Wert nicht nachtraeglich ermitteln, ein geratener waere schlimmer als
  keiner. **Offen ausserdem:** ob die MyŠkoda Public API die Temperatur
  ueberhaupt liefert, ist an einer echten Antwort nicht nachgewiesen (die
  Auswertung klopft mehrere plausible Stellen ab) - beim naechsten Poll ins
  Debug-Protokoll sehen. **Erledigt seit 2026-09-21:** beide Apps kennen das
  Feld inzwischen (erfassen, anzeigen, synchronisieren - auch im
  Local-Only-Modus; Android brauchte dafuer die Room-Migration 1->2). Dass ein
  App-Speichern OHNE das Feld den Wert nicht loescht, haengt allein an
  `exclude_unset` in `update_session()` und ist in
  `tests/test_temperature.py` festgehalten. **Ebenfalls erledigt seit 2026-09-21:** die
  Auswertung selbst (Streudiagramm, Klassenmittel, Trend, Jahreszeiten) gibt es
  jetzt auch in beiden Apps - aber **nur im Server-Modus**, als fertige Antwort
  von `GET /api/stats/temperature`. Sie lokal nachzurechnen hiesse,
  `temperature.py` ein drittes Mal nachzubauen (wie schon bei
  `LocalConsumptionCalculator`), mit der Aussicht, dass App und Web frueher oder
  spaeter andere Zahlen zeigen. Im Local-Only-Modus bleibt der Abschnitt daher
  weg, ebenso wenn der Abruf scheitert (aelterer Server ohne den Endpunkt) -
  das Dashboard ist deshalb nicht fehlgeschlagen. **Weiterhin offen:** dieselbe
  Auswertung im Local-Only-Modus.
- **Bestandsdaten koennen ihre Temperatur seit 2026-09-21 (v0.24.0) nachtraeglich
  bekommen** - siehe Abschnitt "Aussentemperatur vom Wetterdienst". Damit ist der
  Satz weiter oben ("fuer alte Vorgaenge laesst sich der Wert nicht nachtraeglich
  ermitteln") ueberholt, allerdings nur fuer Vorgaenge MIT Koordinaten: ein
  Spritmonitor-Import ohne Ladeort bleibt ohne Wert. **Erledigt am selben Tag:** beide Apps
  zeigen die Herkunft (`outside_temp_source`) inzwischen in der Detailansicht an
  (iOS als zweite Zeile unter dem Wert, Android als Zusatz dahinter wie das
  "(geschaetzt)" bei den kWh). Das Feld ist in beiden Apps **nur lesend** - es
  fehlt bewusst im Payload, weil der Server MANUAL aus einer echten
  Wertaenderung ableitet: schickte eine App den alten Wert einfach zurueck,
  bliebe eine von Hand korrigierte Temperatur faelschlich als "vom
  Wetterdienst" stehen. Im Local-Only-Modus setzen die Apps die Quelle selbst,
  nach derselben Regel. Android brauchte dafuer die Room-Migration 2->3.
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

## Web-UI: Responsive-Verhalten (`static/style.css`, ab 2026-09-06)

Bis dahin gab es **keine einzige Media Query** - die Oberflaeche war rein fuer
den Desktop gebaut und auf dem Handy praktisch unbenutzbar (Navigationsleiste
zu breit, die zwoelfspaltige Ladevorgangs-Tabelle weit ausserhalb des
Viewports). Ein Breakpoint bei **720 px**, mehr nicht - bewusst kein
Framework und kein zweiter Breakpoint, die App hat eine Handvoll Seiten.
(Aufbau und Navigation dieser Seiten haben sich mit v0.13.0 geaendert - siehe
den Abschnitt "Web-UI: Seitenaufbau und Navigation" weiter unten.)

**Navigation:** Die Links liegen in einem `.navlinks`-Container mit
`display: contents`. Auf dem Desktop ist das ein No-op - die `.topnav` bleibt
exakt dieselbe flache Flexbox wie vorher; mobil wird der Container sichtbar
und traegt die aufgeklappte Liste unter der Leiste. Umgeschaltet wird nur eine
Klasse (`.topnav.open`), was sichtbar ist, entscheidet die Media Query.

**Ladevorgaenge - Tabelle ODER Karten, nie beides.** Auf schmalen Schirmen
rendert `renderSessions()` (sessions.html) Karten statt Tabellenzeilen,
entschieden ueber `matchMedia('(max-width: 720px)')` und bei dessen
`change`-Event neu. Bewusst NICHT beide Varianten rendern und eine per CSS
verstecken - das baute jeden Ladevorgang doppelt auf. Beide Darstellungen
ziehen ihre Anzeigewerte aus derselben `sessionView()`-Funktion, damit sie
nicht auseinanderlaufen.

Der Kartenaufbau folgt bewusst der Zeile in der iOS-App
(`SessionsListView.swift::SessionRow`), damit App und Web sich gleich anfuehlen:
links Datum + AC/DC-Badge + Hinweis-Symbol, darunter Fahrzeug/Anbieter/Ort,
SoC + Kilometerstand, Verbrauch mit Methoden-Symbol; rechts kWh, Gesamtpreis,
Preis je kWh. Ein Tipp auf die Karte oeffnet den Bearbeiten-Modus - wie in der
App, wo der Nutzer die Zeilen selbst komplett antippbar gemacht hat. Loeschen
liegt dort hinter einer Wischgeste; im Web ist es ein zurueckhaltender
Ghost-Button am Rand (`event.stopPropagation()`), nicht der flaechig rote
Knopf der Tabelle - der waere sonst das auffaelligste Element der Liste.
`needs_review` zeigt sich als orange linke Kante statt als flaechige
Einfaerbung wie in der Tabelle (die schluckt auf kleinen Schirmen die
Sekundaertexte).

**Aufklappbare Bloecke** sind durchgaengig natives `<details>` (kein eigenes
JS): `details.help` fuer die langen Erklaertexte, `details.panel` fuer ganze
Abschnitte. Zwei Muster:

- `details.panel.plain` uebernimmt eine bestehende Abschnittsueberschrift als
  `<summary>` - in den Einstellungen tragen die `<h2 id="*-form-heading">`
  ohnehin schon den Wechsel zwischen "Fahrzeug anlegen" und "Fahrzeug
  bearbeiten", sie wurden nur zu `<summary>`. Jede `startEdit*()`-Funktion
  setzt `panel.open = true`, sonst scrollt das Bearbeiten ins Leere (seit
  v0.13.0 liegt dieses Panel geschachtelt in einem Abschnitts-Panel, es
  muessen also beide geoeffnet werden - siehe `openForEdit()`).
- Das MyŠkoda-Debug-Protokoll (seit v0.13.0 auf der Unterseite `api-debug`)
  laedt seine 200 Zeilen erst beim Aufklappen (`toggle`-Listener); zugeklappt
  entfaellt der API-Aufruf ganz.

Der Ausgangszustand des Ladevorgangs-Formulars haengt an der Breite
(`setupFormPanel()`, nur beim Laden) - wer danach selbst auf- oder zuklappt,
soll das beim Drehen des Geraets nicht wieder verlieren.

**Tabellen** stehen jetzt in `div.tablewrap` mit `overflow-x: auto`. Vorher
schoben die breiten (Ladeorte mit Koordinaten, das Protokoll) die ganze Seite
seitlich aus dem Bild.

**Statische Dateien MUESSEN einen `Cache-Control`-Header haben** (2026-09-06,
teuer gelernt): `StaticFiles` liefert ETag und `Last-Modified`, aber von sich
aus keinen `Cache-Control` - ohne den wenden Browser heuristisches Caching an
und halten eine Datei ohne jede Rueckfrage fuer frisch. Beim Responsive-Update
kam auf dem Handy des Nutzers deshalb das neue HTML mit der ALTEN `style.css`
an: der Menue-Knopf war im Markup da, hatte aber keine Regeln, die
Ladevorgangs-Karten waren voellig ungestylt. Das sah nach einem Fehler im
neuen CSS aus, war aber reine Auslieferung. `main.py::_no_cache_html` setzt
jetzt zusaetzlich `no-cache` auf alles unter `/static/` (mit ETag praktisch
immer ein leeres 304), und `base.html`/`login.html`/`register.html` haengen
`?v={{ version }}` an `style.css` und `filter.js`. **Bei jeder Aenderung an
einer statischen Datei also die Version in `changelog.py` mitziehen** - sonst
bleibt die Adresse gleich und ein Proxy-Cache dazwischen kann weiter die alte
Datei liefern.

Messbar: Einstellungen auf 375 px von 4322 px auf 2568 px Seitenhoehe, keine
horizontale Ueberlaeufe mehr auf irgendeiner Seite in beiden Sprachen.

## Web-UI: Seitenaufbau und Navigation (ab 2026-09-08, v0.13.0)

**Hauptleiste: Dashboard, Ladevorgaenge, Einstellungen** (seit v0.20.0
zusaetzlich **Karte**, siehe eigenen Abschnitt weiter unten). Der Import ist
von dort verschwunden - er wird einmal beim Umstieg von Spritmonitor gebraucht
und belegte dauerhaft einen von vier Plaetzen.

**Die Einstellungen sind eine Uebersichtsseite**, kein Fliesstext mehr. Jeder
Bereich ist ein `details.panel`, das **Liste UND Anlege-Formular** enthaelt
(vorher klappte nur das Formular, die Tabelle stand immer sichtbar darueber -
allein die drei Tabellen fuellten die halbe Seite, auch wenn man nur die
Sprache umstellen wollte). Innen liegt das Formular als zweites, geschachteltes
`details.panel.plain`. Jede `startEdit*()`-Funktion muss deshalb **beide**
Ebenen oeffnen (`openForEdit()`), sonst scrollt das Bearbeiten ins Leere. Der
Zaehler im `<summary>` (`.count`) zeigt zugeklappt, wie viele Eintraege
drinstehen. Messbar: Seitenhoehe von 2136 px auf 604 px (Desktop) bzw. von
2774 px auf 783 px (375 px breit).

**Drei Unterseiten** statt weiterer Klappabschnitte, weil jede fuer sich schon
eine Seite ist (Vorschau-Tabelle, drei Formulare, Debug-Protokoll):
`import` (Spritmonitor), `backup` (Daten-Backup + Backup-Import +
WebDAV-Backup, `settings_backup.html`) und `api-debug`
(MyŠkoda-Konfiguration + Debug-Protokoll, `settings_api.html`). Einstieg ueber
`.subpage`-Kacheln in den Einstellungen, Rueckweg ueber einen `.backlink` oben
auf jeder Unterseite.

**Die Pfade liegen bewusst auf oberster Ebene und NICHT unter `/settings/...`**
- alle Links und `fetch()`-Aufrufe sind relativ (siehe Ingress-Abschnitt oben),
das loest nur richtig auf, solange jede Seite genau EINE Ebene unter der Basis
liegt. `/backup` kollidiert nicht mit dem Backup-Router, der unter
`/api/backup` haengt.

**Aktionsspalten sind Icon-Knoepfe** (`static/ui.js`: `editDeleteButtons()`,
`iconButton()`), Beschriftung nur noch als `title`/`aria-label`. Grund: die
beschrifteten Knoepfe "Bearbeiten"/"Löschen" waren mit ~200 px die breiteste
Spalte der zwoelfspaltigen Ladevorgangs-Tabelle; die lief dadurch bei JEDER
Fensterbreite um ~130 px aus dem Container heraus, weil `.container` fest auf
1100 px begrenzt ist. Zweiter Teil der Loesung: die Ladevorgangs-Seite darf
ueber den neuen `container_class`-Block in `base.html` bis 1500 px breit werden
(`.container.wide`) - nur diese eine Seite, auf Dashboard und Einstellungen
liest sich schmaler besser. Seitdem ab 1100 px Fensterbreite kein horizontaler
Ueberlauf mehr. Inline-SVG statt Emoji, weil Emoji je nach Plattform in Groesse
und Farbe auseinanderlaufen; `currentColor` laesst die Icons der Textfarbe des
Knopfes folgen.

## Kartenansicht (`templates/map.html`, ab 2026-09-15, v0.20.0)

Vierter Punkt der Hauptleiste, Gegenstueck zum Karten-Tab der iOS-App
(`Views/MapOverviewView.swift` im Repo `iDomi94/Lademonitor-App`). Die Seite
ist bewusst eine **Nachbildung** dieser Ansicht, nicht eine eigene Idee -
Marker-Rollen, Cluster-Schwelle und das Verhalten beim Antippen sind aus dem
Swift-Code uebernommen, damit App und Web sich gleich anfuehlen.

**Leaflet liegt lokal** unter `static/vendor/leaflet/` (Version 1.9.4, JS +
CSS + `images/`), NICHT an einem CDN: der Rest der Oberflaeche kommt seit der
Chart.js-Entfernung ohne externe Skriptquellen aus (ein Client hatte keinen
CDN-Zugriff), und eine Karte ohne Bedienlogik waere derselbe Fehlerfall. Nur
die **Kacheln** muessen naturgemaess von `tile.openstreetmap.org` kommen -
damit ist die Karte die zweite Ausnahme von der "kein Cloud-Dienst"-Linie
neben der Nominatim-Adresssuche, allerdings eine, die nur der Browser macht
(der Server ruft nichts auf). Dafuer ein Hinweis unter der Karte und ein
eigener Punkt in der Datenschutzerklaerung: OpenStreetMap erfaehrt IP und
Kartenausschnitt, also mittelbar, wo geladen wird.

**Was gezeigt wird:** Ladeorte als blauer Pin plus ihr `radius_m` als Kreis
(macht sichtbar, wie nah ein Vorgang liegen muss, damit `match_location()`
greift), Ladevorgaenge mit Koordinaten als graue Punkte bzw. Zahlen-Cluster,
`needs_review` orange. Der Zeitraumfilter (`filter.js`) wirkt nur auf die
Ladevorgaenge - Ladeorte sind Stammdaten. Die Sessions werden mit
`limit=1000` geladen statt der 200 der Listenansicht: eine Karte mit Luecken
waere schwer als solche zu erkennen.

**Clustering** ist dieselbe Single-Linkage-Rechnung wie in der App (Schwelle
= 6 % der sichtbaren Kantenlaenge, neu gebildet bei jedem `moveend`) - keine
Clustering-Bibliothek, das waere die schwerere Loesung fuer dasselbe
Ergebnis. Klick auf ein Cluster zoomt hinein, solange sich die Punkte
auftrennen lassen, sonst oeffnet eine Liste.

**Ob sich etwas auftrennen laesst, entscheidet sich in BILDSCHIRMPIXELN bei
maximalem Zoom** (`handleClusterClick`), nicht an einer festen Entfernung:
die iOS-Vorlage nimmt dafuer ~11 m (`sameSpotEpsilon = 0.0001`), und genau
das war im Web unbrauchbar - die GPS-Punkte mehrerer Ladevorgaenge an
derselben Wallbox streuen um einige zehn Meter, liegen also fast immer
DARUEBER. Folge: es kam praktisch nie eine Liste, man zoomte bis zum Anschlag
und stand dann vor einem Cluster, das sich nicht mehr aufloeste. Jetzt:
Liste, sobald die aeussersten Punkte auch bei `map.getMaxZoom()` naeher als
60 px beieinanderlaegen (zwei Marker sind je ~30 px breit) ODER der Zoom
bereits am Anschlag ist.

**Zwei Fallen, beide im Browser aufgefallen und behoben:**

1. `.modal` hatte `z-index: 100`, Leaflet vergibt seinen Ebenen und
   Bedienelementen bis 1000 - jeder Dialog lag unsichtbar HINTER den
   Kartenkacheln. Der Wert steht jetzt auf 1100 (gilt auch fuer das
   Changelog-Modal, dort folgenlos).
2. Ladeort-Marker und Session-Cluster liegen an einem bekannten Ladeort
   zwangslaeufig auf derselben Koordinate, der obere verdeckte den unteren
   komplett (inkl. Klickflaeche). Deshalb haengt der Ladeort als Pin mit
   Spitze UEBER dem Punkt (`iconAnchor: [13, 34]` plus CSS-Spitze in
   `.pin-location::after`), die Ladevorgaenge sitzen mittig darauf.

**Ein Klick auf einen Ladeort zeigt zuerst dessen Ladevorgaenge**
(`openLocationSessions`), das Bearbeiten liegt als Knopf unter der Liste.
Anders herum (direkt ins Formular, wie die iOS-Vorlage es macht) war der
haeufigere Fall - nachsehen, was man dort geladen hat - ueberhaupt nicht
erreichbar. `sessionsAtLocation()` nimmt neben `location_id` auch Vorgaenge
mit, die im Radius liegen und noch KEINEM Ort zugeordnet sind (genau die, die
das Geo-Matching vergeben wuerde); wer schon an einem ANDEREN Ort haengt,
bleibt aussen vor, sonst taucht derselbe Vorgang an zwei Orten auf. Dieselbe
Liste (`openSessionList`) dient dem nicht auftrennbaren Cluster.

**Bearbeitet wird nicht doppelt:** das Ladeort-Formular steht auf der Karte
selbst (dieselben Felder wie in den Einstellungen), ein
Ladevorgang dagegen fuehrt ueber die Vorschau nach `sessions#edit=<id>` - die
Ladevorgaenge-Seite hat das vollstaendige Formular bereits, eine zweite Kopie
davon wuerde frueher oder spaeter auseinanderlaufen. `openFromHash()` dort
laedt notfalls mit `limit=1000` nach, falls der Eintrag ausserhalb der ersten
200 liegt. Die Vorschau bietet ausserdem "Bestaetigen" (`needs_review` weg,
ohne das Formular zu oeffnen) - wie die Detailansicht in der App.

## E-Mail (`mailer.py`, `notifications.py`, `routers/email.py`, ab 2026-09-08, v0.14.0)

### SMTP-Konfiguration ist GLOBAL, nicht pro Nutzer

Bewusste Abweichung vom WebDAV-Muster: dort sichert jeder Nutzer seine eigenen
Daten auf sein eigenes Ziel, hier verschickt der Server Mails im Namen der
Anwendung. Entscheidend ist der Passwort-vergessen-Fall - **in dem Moment ist
niemand angemeldet**, eine am Nutzer haengende Konfiguration waere also gar
nicht erreichbar. Genau eine Zeile in `smtp_configs`, nur fuer Admins
(`require_admin` pro Endpunkt in `routers/email.py`).

Versand ueber die **Standardbibliothek** (`smtplib` + `email.message`), keine
neue Abhaengigkeit. Synchron wie alles hier, Scheduler-Aufrufe ueber
`asyncio.to_thread`.

### `base_url` wird konfiguriert und NIEMALS aus dem Request abgeleitet

Ein Reset-Link braucht eine absolute Adresse, die App kennt ihre eigene aber
nicht (Unraid-IP, eigener Nginx, HA-Ingress - deshalb ist die ganze UI auf
relative Pfade gebaut). Der naheliegende Weg, den `Host`-Header zu nehmen, ist
ein Sicherheitsloch: der Header kommt vom Client, wer ihn beim
Passwort-vergessen-POST faelscht, laesst dem Opfer eine Mail mit einem Link auf
die eigene Domain zustellen (Host-Header-Poisoning). Ausserdem gibt es bei
geplanten Mails ueberhaupt keinen Request. Ist `base_url` leer, sind alle
Funktionen mit Link (Reset, Bestaetigung, Einladung) aus - sichtbar in den
Einstellungen und am fehlenden "Passwort vergessen"-Link auf der Login-Seite.

### Warum das SMTP-Passwort im Klartext liegt

Es geht nicht anders: SMTP-AUTH **uebertraegt** das Passwort, aus einem Hash
liesse es sich nicht zurueckgewinnen (beim Nutzer-Login wird dagegen nur
verglichen - deshalb dort bcrypt). Gleiches gilt fuer das WebDAV-Passwort und
den MyŠkoda-API-Key. Der wirksame Schutz ist nicht Verstecken, sondern den Wert
begrenzen: die Einstellungen empfehlen ein app-spezifisches Passwort bzw. ein
eigenes Absender-Konto (einzeln widerrufbar, kein Zugriff aufs Postfach).
Zusaetzlich: nie ueber die API zurueckgeben (`has_password`), nicht in die
Backup-ZIP.

Eine Verschluesselung at rest waere nur mit einem Schluessel AUSSERHALB von
`/config` sinnvoll (Env-Variable), weil `PGDATA` unter `/config/postgres` liegt
und das CA-Template die Nutzer ausdruecklich auffordert, `/config` ins Backup
zu nehmen - Schluessel und DB wuerden sonst immer gemeinsam abfliessen.

**Update 2026-09-09: umgesetzt.** Genau dieser Mechanismus
(`FIELD_ENCRYPTION_KEY`, siehe Abschnitt "Verschluesselung personenbezogener
Daten") deckt inzwischen auch SMTP-/WebDAV-Passwort und MyŠkoda-API-Key ab -
optional, wie der Rest der Feld-Verschluesselung. Am Grund, warum das
Passwort ueberhaupt im Klartext VORLIEGEN muss (bevor es ggf. verschluesselt
gespeichert wird), aendert das nichts: SMTP-AUTH braucht das Passwort selbst,
nicht nur einen Hash-Vergleich.

### Wo Hashing sehr wohl richtig ist

`auth_tokens.token` lag bis v0.14.0 im **Klartext** - ein Auth-Token IST eine
fertige Anmeldung, wer die Tabelle lesen konnte, war sofort jeder Nutzer (und
das wiegt schwerer als das SMTP-Passwort, das nur Mailversand erlaubt). Jetzt
`token_hash` mit SHA-256. Bewusst **nicht bcrypt**: der Lookup geht ueber
Gleichheit, ein gesalzener, absichtlich langsamer Hash liesse sich gar nicht
nachschlagen - und noetig ist er nicht, weil `secrets.token_urlsafe(32)` bereits
256 Bit Zufall sind. bcrypt schuetzt Passwoerter davor, kurz und
menschengemacht zu sein; dieses Problem existiert hier nicht. Dieselbe
Behandlung fuer `user_tokens` (Reset/Bestaetigung/Einladung).

Die Migration hasht Bestandszeilen und entfernt danach die Klartextspalte -
**niemand wird ausgeloggt**, Cookie, iOS-App und HA-Header gelten weiter
(empirisch gegen echtes Postgres verifiziert).

### Passwort-Reset

`POST /api/auth/password-reset/request` antwortet **immer** 204 - auch bei
unbekanntem Konto, fehlender/unbestaetigter Adresse, ausgeschaltetem SMTP oder
Rate-Limit. Jede Unterscheidung waere ein Verzeichnis aller Nutzernamen und
Adressen. Was wirklich passiert ist, steht im Versandprotokoll (nur fuer
Admins). Rate-Limit: 3 Anfragen pro Konto und Stunde plus eine globale
Obergrenze, ueber vorhandene Tabellen gezaehlt statt mit `slowapi` o.ae. Das
offene Limit auf `/login` und `/register` bleibt davon unberuehrt.

Nur eine **bestaetigte** Adresse darf zuruecksetzen - sonst haette ein
Tippfehler einem Fremden einen gueltigen Token fuer ein fremdes Konto in die
Hand gegeben.

**Beim Einloesen werden ALLE `auth_tokens` des Nutzers geloescht** (ebenso beim
eigenen Passwortwechsel). Waere das Konto uebernommen worden, liefe die fremde
Sitzung sonst weiter. Konsequenz in genau dieser App: der
HA-`rest_command`-Token stirbt mit - darauf weisen Mail, Bestaetigungsseite und
Einstellungen ausdruecklich hin.

**Seit 0.14.1 gibt `PUT /api/auth/password` den neuen Token zurueck** (200 mit
`{token, user}` statt 204). Die frische Sitzung wurde vorher nur als Cookie
ausgestellt, was allein der Web-Oberflaeche half; ein Bearer-Client hatte seinen
Token gerade selbst entwertet, bekam keinen neuen und musste sich mit dem neuen
Passwort ein zweites Mal anmelden, obwohl die Sitzung serverseitig schon
existierte. Beim ZURUECKSETZEN bleibt es bewusst bei 204 ohne Token: dort ist
der Aufrufer per Definition nicht angemeldet, ein Token in der Antwort wuerde
den Reset-Link zu einem vollwertigen Anmeldeweg machen. Die saubere Alternative
waeren typisierte, benannte API-Tokens (`kind: session | api`), die einen Reset
ueberleben - eine eigene Baustelle, bewusst nicht hier mit reingezogen.

Reset- und Bestaetigungsseite loesen **nichts per GET aus**: der Link oeffnet
eine Seite, gesetzt wird per POST. Mail-Scanner rufen Links beim Vorschauen
automatisch ab und wuerden den Token sonst verbrauchen (deshalb unterscheidet
`check_user_token` auch "bereits verwendet" von "ungueltig" - das ist der
haeufigste Supportfall).

### Anmeldung mit Nutzername ODER E-Mail

`_find_user_by_login()`: Nutzername exakt zuerst (das ist die Identitaet), dann
die Adresse ueber `lower()` - der Unique-Index laeuft ebenfalls ueber
`lower(email)`, sonst scheitert die Anmeldung an einem grossgeschriebenen
Anfangsbuchstaben aus der Autovervollstaendigung. Das API-Feld heisst weiterhin
`username`, damit iOS-App und HA-Login unveraendert funktionieren.

### Mail-Vorlagen: EINE statt neun

`templates/emails/layout.html` + `.txt`; jede Mail liefert nur ein kleines
Modell (`mailer.Mail`: Ueberschrift, Absaetze, optionaler Knopf, Wertetabelle,
Schlusshinweis). Neun eigene Vorlagen mit je HTML- und Textfassung waeren
achtzehn Dateien, die auseinanderlaufen. Immer **beide** Fassungen
(`multipart/alternative`) - reiner HTML-Versand erhoeht die Spam-Bewertung
deutlich.

**Autoescaping nur fuer `.html`** (`select_autoescape(enabled_extensions=("html",))`):
global eingeschaltet landen in der Textfassung HTML-Entities, aus einem
Anfuehrungszeichen wird `&#34;`. Genau das ist im Test aufgefallen.

Mails gehen in der Sprache des **Empfaengers** raus, nicht des Ausloesers -
dafuer `i18n.language_context()`, ein Kontextmanager, der die ContextVar
danach zurueckstellt (ohne das wuerde ein Versand mitten in einem Request die
Sprache fuer den Rest der Antwort umstellen).

### Benachrichtigungen (`notifications.py`)

Zwei Ausloeserarten:

* **Ereignisgetrieben** aus `webdav_backup.py` und `myskoda_poller.py` - beide
  merken sich `previous_status` VOR dem Ueberschreiben und melden nur den
  **Uebergang** nach "kaputt". Sonst kaeme bei einem dauerhaft falschen
  Passwort taeglich dieselbe Mail. Beim MyŠkoda-Poller ausserdem nur
  `auth_error`: ein einzelner Netzwerkfehler heilt beim naechsten Abruf von
  selbst, `rate_limited` ist normal - beides waere reines Rauschen.
* **Zeitgesteuert** ueber `run_due_notifications()` im Scheduler (Takt 15 min,
  wie das WebDAV-Backup): Ablaufwarnung des API-Keys (14/7/1 Tage),
  Prüf-Sammelmeldung (pro Nutzer aus/taeglich/woechentlich), Monatsbericht.

Die Merkerspalten (`last_failure_notified_at`, `api_key_expiry_notified_days`,
`last_review_digest_at`, `last_monthly_report_at`) sind nicht optional: ohne sie
ginge dieselbe Mail viertelstuendlich erneut raus. Auch wenn nichts zu melden
ist, wird der Zeitstempel fortgeschrieben.

Der Monatsbericht ruft `routers/stats.py::stats_summary()` als gewoehnliche
Funktion auf (Depends-Parameter explizit uebergeben), damit Mail und Dashboard
nicht unterschiedliche Zahlen zeigen koennen.

### Web-UI

Neue Unterseite `/email` (Admin: SMTP, Testmail, Protokoll) als vierte Kachel.
Neue Klappabschnitte "Mein Konto" (Adresse, Bestaetigungsstatus, **eigenes
Passwort aendern** - gab es vorher gar nicht) und "Benachrichtigungen" in den
Einstellungen. Benutzerverwaltung um E-Mail-Spalte, Bearbeiten und Einladen
erweitert. Oeffentliche Seiten `/forgot-password`, `/reset-password`,
`/verify-email` - wie alle Seiten genau eine Ebene unter der Basis (Ingress).

## Verschluesselung personenbezogener Daten (`crypto.py`, ab 2026-09-09)

Ausloeser: der Nutzer will den Server oeffentlich (Nginx-Reverse-Proxy statt
nur Heimnetz) erreichbar machen und wollte dafuer DSGVO-konform absichern,
idealerweise so, dass er selbst als Betreiber keinen Zugriff auf die
personenbezogenen Daten anderer/eigener Nutzer hat. Echtes Zero-Knowledge
(clientseitige Verschluesselung, Schluessel verlaesst nie das Nutzergeraet)
wurde bewusst VERWORFEN, weil es die Kern-Architektur der App sprengen wuerde:
Geo-Matching (`match_location`), die komplette Statistik-Aggregation
(`stats.py`, `consumption.py`), das server-seitige Offline-Reverse-Geocoding
und vor allem der Home-Assistant-Push sowie der MyŠkoda-Poller (beide senden
rohes JSON direkt an die API, ohne verschluesselnden Client dazwischen)
brauchen Klartext-Zugriff auf dem Server. Umgesetzt ist stattdessen
**Verschluesselung at rest gegen Diebstahl von DB-Dump/Backup/Datentraeger**
(z.B. wenn der Hoster/VPS-Anbieter oder wer auch immer ein Backup abgreift) -
**ausdruecklich NICHT** ein Schutz davor, dass der Betreiber (wer den
laufenden Server-Prozess kontrolliert) die Daten technisch einsehen koennte,
denn der Schluessel liegt im Server-Environment und der Server entschluesselt
noch waehrend jedes Requests transparent. Diese Grenze steht auch als
Docstring in `crypto.py`, damit sie nicht in Vergessenheit geraet.

**Umfang (bewusst NICHT alles auf einmal):** verschluesselt sind die
GPS-Koordinaten (`ChargingSession.latitude/longitude`,
`ChargingLocation.latitude/longitude` - der schaerfste Fall ist ein Ladeort
namens "Zuhause"), `ChargingSession.notes` und `.geocoded_place`,
`ChargingLocation.name` und `Vehicle.name` (reine Anzeigelabels), sowie die
drei bisher als Klartext-Secrets dokumentierten Zugangsdaten
`SmtpConfig.password`, `WebdavBackupConfig.password` und
`MySkodaConfig.api_key` (**Update 2026-09-09, spaeter am selben Tag:**
urspruenglich als eigener, spaeterer Schritt vorgesehen - inzwischen erledigt,
da keine SQL-Filterung/Unique-Constraint auf diesen Feldern liegt, also
keinen Blind-Index braucht, genau wie die GPS-Spalten).

**NICHT verschluesselt, bewusst, wegen SQL-Gleichheitsvergleich/
Unique-Constraint:** `Vehicle.external_id` (HA-Push in `routers/sessions.py`
UND Dublettencheck in `routers/vehicles.py` fragen exakt per SQL danach ab,
zusaetzlich eindeutig pro Nutzer), `Provider.name` (Dublettencheck in
`routers/providers.py::create_provider` sowie eindeutig pro Nutzer, siehe
Abschnitt "Pro-Nutzer-Datentrennung" weiter oben), `User.email` (haengt am
funktionalen `lower(email)`-Unique-Index fuer den Login/Reset-Lookup, siehe
Abschnitt "Authentifizierung") und `User.username` (Login-Lookup, global
eindeutig). Fernet ist NICHT deterministisch (Zufalls-IV) - zwei
Verschluesselungen desselben Klartexts ergeben unterschiedlichen Ciphertext,
also wuerden sowohl ein `WHERE spalte = :wert`-Vergleich als auch ein
DB-Unique-Constraint auf der verschluesselten Spalte einfach aufhoeren zu
funktionieren (der Login wuerde bei jedem Versuch fehlschlagen bzw. der
HA-Push jedes Fahrzeug mit 404 quittieren). Eine Loesung dafuer waere ein
zusaetzlicher, DETERMINISTISCHER Blind-Index (z.B. HMAC-SHA256 mit einem aus
`FIELD_ENCRYPTION_KEY` abgeleiteten Schluessel, in einer eigenen Spalte
`*_lookup_hash`, Unique-Constraint/WHERE-Abfragen laufen dann ueber diese
Hash-Spalte statt ueber den Klartext) - eigenstaendige, groessere
Baustelle, noch nicht umgesetzt.

**`ChargingLocation.name`/`Provider.name` und SQL `ORDER BY`:** beide Listen
waren vorher per SQL `ORDER BY name` sortiert - Ciphertext sortiert sich
nicht alphabetisch. `Provider.name` bleibt unverschluesselt (siehe oben),
`routers/providers.py::list_providers` sortiert also weiterhin per SQL.
`ChargingLocation.name` ist jetzt verschluesselt, `routers/locations.py::
list_locations` sortiert deshalb seither in Python nach dem Laden
(`sorted(locations, key=lambda loc: loc.name)`) - bei der ueblichen
Groessenordnung (Ladeorte eines einzelnen Nutzers) unkritisch.

**Update 2026-09-09, DSGVO-Vollpruefung (dritte Runde am selben Tag):** Auf
Nutzeranfrage alle verbliebenen Klartextfelder systematisch durchgegangen -
zwei Kategorien gefunden, die beim zweiten Schritt (Secrets) uebersehen
wurden, plus eine bewusst wieder verworfene dritte:

- **Uebersehen, jetzt nachgezogen:** `Provider.notes` (Freitext, gleiches
  Risiko wie `ChargingSession.notes`), `WebdavBackupConfig.url`/`.username`
  (Cloud-Adressen wie Nextcloud enthalten oft den eigenen Kontonamen im Pfad,
  z.B. `.../files/<username>/...`), `MySkodaConfig.vin` (die
  Fahrzeug-Identifizierungsnummer - ein weltweit eindeutiger, ueber
  Zulassungs-/Versicherungsdaten auf eine Person rueckfuehrbarer
  Identifikator), `MySkodaConfig.open_latitude/open_longitude` (dieselbe
  GPS-Position wie `ChargingSession.latitude/longitude`, nur als
  Zwischenspeicher fuer den noch laufenden Vorgang - eine Inkonsistenz: der
  fertige Datensatz war geschuetzt, die Kopie im Zwischenzustand nicht) und
  `MySkodaLogEntry.payload` (komplette Rohantwort der MyŠkoda-API inkl. GPS/
  VIN/SoC, Standard `log_raw_payload=True`, bis zu `LOG_MAX_ENTRIES` Zeilen
  pro Fahrzeug - der mit Abstand groesste zusammenhaengende Bestand an
  Rohdaten in der App und vermutlich die groesste Einzel-Angriffsflaeche vor
  dieser Aenderung).

  **Versteckte Nebenwirkung beim VIN gefunden und mitkorrigiert:**
  `myskoda_poller.py::_create_session()` baute die
  `external_session_id` als `f"myskoda-{config.vin}-{start_time}"` - waere
  die VIN trotz verschluesselter `vin`-Spalte ueber dieses zweite, fuer den
  Dublettencheck zwingend unverschluesselte Feld wieder im Klartext gelandet
  (dieselbe Kategorie wie `Vehicle.external_id`/`Provider.name` oben - ein
  exakter SQL-Gleichheitsvergleich braucht Klartext bzw. deterministischen
  Wert). Umgestellt auf `f"myskoda-{vehicle.id}-{start_time}"` -
  `Vehicle.id` ist ebenso pro Fahrzeug eindeutig, aber die interne UUID ist
  kein personenbezogenes Datum. Format vorher `myskoda-<FIN>-<Startzeit>`,
  siehe Abschnitt "MyŠkoda Public API" weiter oben. Rein additive Aenderung
  fuer neu angelegte Sessions - Bestandszeilen mit dem alten Format bleiben
  unangetastet und kollidieren nicht mit dem neuen Schema.

- **Bewusst NICHT verschluesselt, obwohl technisch moeglich gewesen waere:**
  `UserToken.email` (bei `EMAIL_VERIFY`) und `EmailLogEntry.to_address` -
  beide sind zum Zeitpunkt des Schreibens IMMER eine exakte Kopie eines
  bereits existierenden `User.email`-Werts (verifiziert im Code:
  `routers/auth.py` setzt `user.email = payload.email` bzw. legt den
  `User`-Datensatz mit `email=payload.email` an, JEWEILS bevor der
  zugehoerige Token/die Mail erzeugt wird - `UserToken.email`/
  `EmailLogEntry.to_address` sind also nie ein GEGENUEBER `User.email`
  unabhaengiger Wert). Da `User.email` selbst aus den oben genannten
  Login-/Reset-Lookup-Gruenden zwingend Klartext bleiben muss, wuerde das
  Verschluesseln der beiden Kopien keinen zusaetzlichen Schutz bringen - die
  Adresse waere ueber `users.email` ohnehin trivial einsehbar. Reiner
  Mehraufwand ohne Sicherheitsgewinn, deshalb bewusst ausgelassen.

**Mechanik:** `crypto.py` haelt Fernet (`cryptography`-Paket, AES-128-CBC +
HMAC, inkl. Zeitstempel und Authentifizierung) als `EncryptedString`/
`EncryptedFloat` (`TypeDecorator`, impl `Text`) - Router/Schemas/ORM-Code
sehen weiterhin normale Python-`str`/`float`, in der DB liegt nur Ciphertext.
Dadurch war praktisch **kein Code ausserhalb von `models.py` zu aendern**:
`match_location()` (Haversine) rechnet schon in Python nach dem ORM-Load,
`backup.py` liest/schreibt ausschliesslich ueber die ORM-Objekte - beides
transparent weiter funktionsfaehig.

**Schluessel:** `FIELD_ENCRYPTION_KEY`, ausschliesslich Umgebungsvariable,
NIEMALS in der DB oder unter `/config` - genau das waere sonst im selben
Backup wie die verschluesselten Daten und der Schutz waere wirkungslos.

**Update 2026-09-09 (kippt einen Teil der urspruenglichen Entscheidung, noch
am selben Tag): Verschluesselung ist bewusst OPT-IN, keine Pflicht.**
Urspruenglich verweigerte die App den Start komplett ohne gesetzten
Schluessel (`crypto.require_key()`) - das wurde noch am selben Tag revidiert,
weil ein grosser Teil der Nutzer den Server rein im eigenen Heimnetz betreibt
und dort weder das DSGVO-Thema noch den Aufwand (Schluessel generieren,
sicher verwahren, bei Verlust sind die Felder futsch) braucht. Jetzt: fehlt
`FIELD_ENCRYPTION_KEY` komplett, verhalten sich `EncryptedString`/
`EncryptedFloat` (`crypto.py`) wie ganz normale String/Float-Spalten -
`crypto.is_enabled()` haelt diesen Zustand fest, `encrypt_str()`/
`decrypt_str()` verzweigen intern darauf (No-Op-Passthrough ohne Schluessel).
`main.py` ruft dafuer nur noch `crypto.check_configured()` auf - validiert
das FORMAT eines GESETZTEN Schluessels (verweigert den Start bei einem
kaputten Wert), verlangt aber keine Anwesenheit mehr.

**Einmal aktiviert, bleibt es aktiviert** - genau das ist der Haken am
Opt-in: wird der Schluessel NACH bereits erfolgter Verschluesselung wieder
entfernt, waeren die betroffenen Werte ohne ihn nicht mehr lesbar. Dagegen
prueft `database.py::_check_no_orphaned_ciphertext()` bei JEDEM
Migrationslauf (nicht nur beim ersten): findet sich in einer der sechs
betroffenen Spalten ein Wert mit dem Fernet-Praefix `gAAAAA` (siehe
`crypto.FERNET_PREFIX`, rein String-basiert per `LIKE`, damit der Check auch
OHNE Schluessel funktioniert) WAEHREND kein Schluessel gesetzt ist, verweigert
die App den Start mit einer klaren Fehlermeldung, statt kaputte/falsche Werte
durch API oder Web-UI auszuliefern. Web-UI zeigt den aktuellen Zustand als
Badge unten in den Einstellungen (🔒/🔓, `encryption_enabled` im
Template-Kontext von `main.py::_page()`), `/health` liefert ihn zusaetzlich
als `field_encryption` im JSON.

**Migration Bestandsdaten** (`database.py::run_light_migrations()`): laeuft
wie alle anderen leichten Migrationen bei jedem Container-Start und ist
idempotent, in zwei getrennten Schritten. **Schritt 1** (immer, unabhaengig
vom Schluessel): fuer die GPS-Spalten (bisher `DOUBLE PRECISION`) wird der
Spaltentyp per Umbenennungs-Trick (neue VARCHAR-Spalte, Python-seitig pro
Zeile befuellen ueber `crypto.encrypt_str()` - verschluesselt NUR, wenn
`is_enabled()`, sonst reiner Passthrough -, alte Spalte droppen, neue
umbenennen - gleiches Muster wie schon bei der
`auth_tokens.token`->`token_hash`-Migration) auf Text umgestellt, WEIL das
Modell (`models.py`) so oder so eine Text-Spalte erwartet, ob verschluesselt
oder nicht; `not_null=True` bei `ChargingLocation.latitude/longitude` haelt
die urspruengliche NOT-NULL-Eigenschaft ueber den Umbau hinweg aufrecht.
**Schritt 2** (`_encrypt_pending_plaintext()`, No-Op ohne Schluessel): fuer
alle sechs Spalten (die vier GPS- plus `notes`/`geocoded_place`, die schon
vorher VARCHAR/TEXT waren) werden noch unverschluesselte Bestandswerte
nachtraeglich verschluesselt, erkannt ueber `crypto.is_encrypted()`
(Praefix-Check) statt Entschluesselungsversuch. Das deckt auch den Fall
"Schluessel wird ERST NACH einer Weile aktiviert" ab: bis dahin angesammelte
Klartextwerte (inkl. GPS, das laengst zu VARCHAR migriert war) werden beim
naechsten Start mit gesetztem Schluessel automatisch nachverschluesselt. Auf
einer komplett neuen Installation legt `create_all()` die GPS-Spalten direkt
als VARCHAR an, Schritt 1 ist dort ein No-Op. **Vor dem Einsatz auf der
echten Installation unbedingt gegen eine Kopie der Produktiv-DB testen** -
eine fehlgeschlagene Verschluesselung von Bestandsdaten ist nicht trivial
rueckgaengig zu machen.

**Bekannte Einschraenkung, unbedingt beachten:** Der eingebaute Export
(`routers/backup.py`) und das automatische WebDAV-Backup liefern weiterhin
**Klartext-CSV** - die ORM-Objekte werden beim Export ganz normal entschluesselt
gelesen (das ist fuer das dokumentierte Ziel "portables, menschenlesbares
Backup" auch richtig so). Landet dieses Backup auf fremder Infrastruktur
(z.B. WebDAV auf einer nicht selbst kontrollierten Nextcloud-Instanz), liegen
GPS-Koordinaten und Notizen dort wieder im Klartext - die
Feld-Verschluesselung schuetzt nur die laufende Datenbank/deren
Rohdatentraeger, nicht das CSV-Backup. Bewusst nicht in diesem ersten Schritt
geloest (wuerde das dokumentierte, restore-faehige CSV-Format aendern);
Kandidat fuer einen spaeteren Schritt waere ein optional verschluesseltes
ZIP (z.B. Passwort-geschuetzt) speziell fuer den WebDAV-Weg.

## Sync: Grabsteine fuer geloeschte Datensaetze (`sync.py`, `routers/sync.py`, ab 2026-09-20, v0.22.0)

Beide Apps spiegeln die vier Kern-Entitaeten lokal (SwiftData bzw. Room). Eine
Loeschung auf dem Server - ueber die Web-UI, ein zweites Geraet, einen anderen
Client - kam dort bis v0.22.0 **nie** an: die Apps hatten das Aufraeumen
ausdruecklich abgeschaltet (`removeVanishedMirrors()` war ein leerer Rumpf) und
lebten stattdessen mit "Geisterzeilen".

**Das war richtig so, und der Grund gilt weiter:** aus der ABWESENHEIT einer
Zeile in einer Pull-Antwort darf man nicht auf eine Loeschung schliessen. Ein
Serverfehler, eine unerwartet leere Antwort oder ein Fehler in der
ID-Aufloesung beim vorangegangenen Push sehen genauso aus - und haetten still
und unwiderruflich lokale Ladevorgaenge vernichtet.

Die Loesung ist deshalb nicht, jene Pruefung wieder einzuschalten, sondern ein
**positives** Signal: `models.DeletedRecord` haelt pro geloeschter Zeile einen
Grabstein (`entity_type`, `entity_id`, `deleted_at`, `user_id`), angelegt in
`sync.record_deletion()` direkt VOR dem `db.delete()` und im selben Commit -
sonst gaebe es den Zustand "Zeile weg, Grabstein fehlt", also genau die Luecke,
die der Mechanismus schliessen soll. `GET /api/sync/deletions?since=` liefert
sie.

**Bewusst ohne Ablauf/Aufraeum-Job.** Eine Zeile ist ein paar Dutzend Byte, und
ein zu frueh entfernter Grabstein bringt die Geisterzeile still zurueck (ein
Geraet, das laenger offline war als die Frist, saehe die Loeschung nie). Der
Bestand waechst mit der Anzahl LOESCHUNGEN, nicht mit der Datenmenge.

**Der Cursor ist ein roher String, kein Datum.** `server_time` wird von den Apps
unveraendert zurueckgeschickt. Grund: der Server schreibt naive UTC-Zeitstempel
(`datetime.utcnow()`), die Datums-Decoder beider Apps deuten einen Zeitstempel
ohne Zone aber als LOKALE Zeit (bewusst, siehe `APIClient.swift` - fuer
`start_time` ist das richtig). Einmal hin- und zurueckgewandelt waere der Cursor
um den Zeitzonen-Offset verschoben und wuerde Loeschungen ueberspringen. Als
unveraenderte Zeichenkette kann das nicht passieren. `server_time` wird
ausserdem VOR der Abfrage genommen: ein Grabstein, der waehrenddessen entsteht,
faellt dann ins naechste Fenster statt zwischen beide.

**Der Grabstein gewinnt, auch gegen `isDirty`.** Die Zeile existiert auf dem
Server nicht mehr, ein Push darauf liefe in ein 404, und sie stehenzulassen
braechte die Geisterzeile zurueck. Die Apps setzen ihren Cursor erst NACH dem
erfolgreichen Anwenden - eine bereits geloeschte Zeile erneut zu loeschen ist
folgenlos, eine verpasste Loeschung waere dauerhaft. Gegen einen Server aelter
als v0.22.0 (404 auf den Endpunkt) verhalten sich beide Apps wie bisher, statt
den ganzen Sync als fehlgeschlagen zu melden.

**Was bewusst NICHT umgesetzt ist: ein Delta-Pull fuer Aenderungen.** Naechster
Gedanke waere, auch die vier Listen nur noch inkrementell zu holen
(`?since=`, `updated_at` liegt auf `ChargingSession` schon vor). Bei den
Ladevorgaengen geht das aber nicht ohne Weiteres: `consumption.py` berechnet
den Verbrauch aus der KETTE (Vorgaenger, Vollladungs-Intervalle), eine
Aenderung an einem Vorgang aendert also die Werte seiner Nachbarn mit, ohne
deren `updated_at` anzufassen. Ein Delta-Pull wuerde dort veraltete
Verbrauchswerte stehen lassen. Die Volluebertragung bleibt deshalb - sie ist
seit dem Limit-Fix (siehe unten) ohnehin erst vollstaendig.

**Nutzer loeschen:** `_purge_owned_data()` in `routers/auth.py` raeumt die
Grabsteine mit weg - sie haengen per Fremdschluessel am Nutzer, eine
verbliebene Zeile liesse das Loeschen des Kontos an Postgres scheitern (genau
der Fehlertyp, der 2026-09-09 schon einmal in der Admin-Loeschfunktion steckte).

## Paginierung von `GET /api/sessions` (Fix 2026-09-20, v0.22.0)

Der Endpunkt hatte `limit: int = Query(default=200, le=1000)` - und **kein
einziger Client** hat je einen Wert mitgeschickt: Web-UI (`sessions.html`),
iOS (`APIClient.swift`), Android (`ApiClient.kt`). Wer mehr als 200
Ladevorgaenge hatte (nach einem Spritmonitor-Import schnell der Fall), sah
ueberall nur die neuesten 200, ohne Hinweis und ohne Weg zu den aelteren; die
Apps spiegelten sie entsprechend nie. Dass die Statistik trotzdem die
vollstaendigen Zahlen zeigte (`routers/stats.py` aggregiert serverseitig ueber
alle Vorgaenge), machte die Luecke besonders schwer zu bemerken. Betraf auch
die Kartenansicht, die ihre Punkte aus demselben Endpunkt zieht.

Jetzt: **ohne Angabe die vollstaendige Ergebnismenge**, `limit` (1..5000) und
`offset` optional fuer seitenweises Laden. Unbegrenzt ist hier vertretbar, weil
`attach_consumption()` ohnehin die KOMPLETTE Fahrzeughistorie laedt (die
Verbrauchskette braucht sie) - ein Limit auf der Ergebnismenge hat die
eigentliche Arbeit also noch nie gespart. Die Clients mussten dafuer nicht
angefasst werden: sie schickten ja nie einen Wert.

## PWA: installierbare Web-Oberflaeche (ab 2026-09-20, v0.22.0)

`icon-192.png`/`icon-512.png` lagen seit dem Logo-Wechsel (v0.18.0) im Repo und
der Changelog sprach von "PWA-Installation" - es gab aber **weder ein Manifest
noch einen Service Worker**, die Oberflaeche war also nirgends installierbar.

Beide liegen bewusst auf OBERSTER Ebene (`main.py::web_app_manifest()`,
`service_worker()`), nicht unter `/static/`:

- Beim Manifest werden `start_url`/`scope` relativ zur Adresse DES MANIFESTS
  aufgeloest - unter `/static/manifest.webmanifest` waere der Geltungsbereich
  der App `/static/`, ein Start landete im Dateiverzeichnis.
- Der Service Worker gilt standardmaessig nur fuer sein eigenes Verzeichnis;
  aus `/static/` heraus deckte er die App gar nicht ab.

Inhalte ausschliesslich relativ (`.`, `static/...`), damit beides am
Domain-Root UND unter dem HA-Ingress-Unterpfad aufgeht. Beide Routen sind ohne
Anmeldung erreichbar: der Browser holt das Manifest teils ohne die Cookies der
Seite, und die Login-Seite soll installierbar sein.

**Der Service Worker cacht bewusst NICHTS** (`static/sw.js`) - er existiert
allein, weil Browser eine Installierbarkeit ohne ihn nicht anerkennen. Die App
ist auf "nichts Veraltetes ausliefern" gebaut (`no-store` auf HTML, `no-cache`
plus `?v=` auf statischen Dateien); ein cachender Worker waere eine dritte
Cache-Ebene und die einzige, die ein Nutzer nicht mit einem Neuladen loswird -
genau der Fehler, der 2026-09-06 schon einmal teuer war. Offline-Faehigkeit ist
ohnehin kein Ziel: die Seiten werden serverseitig gerendert. Dafuer gibt es die
beiden Apps mit lokalem Speicher.

## Tests (`backend/tests/`, ab 2026-09-20, v0.22.0)

Bis dahin gab es in KEINEM der fuenf Repos einen einzigen automatisierten Test.
Jetzt pytest gegen ein SQLite-in-memory, ohne Container, plus
`.github/workflows/tests.yml`. Abgedeckt sind bewusst die Stellen, an denen
dieses Projekt real gestolpert ist:

- `test_consumption.py` - die fuenfstufige Fallback-Kette, je Methode ein Fall,
  der sie ausloesen MUSS, plus die Faelle, in denen es `unavailable` bleiben
  muss (kein Vorgaenger, fallender Kilometerstand, fehlende Energie).
- `test_backdating.py` - `_backdated_start()` samt beider Waechter, nachgebaut
  an den echten DC-Vorgaengen vom 05./06.09.2026.
- `test_backup_roundtrip.py` - Export/Import inkl. der beiden Fehler von
  2026-08-31 (Import in ein zweites Konto; Idempotenz des Fixes dafuer).
- `test_sessions_api.py` - die 200er-Grenze als Regressionstest.
- `test_sync_deletions.py` - Grabsteine je Entitaet, `since`-Fenster (auch mit
  zeitzonenbehaftetem Cursor), Trennung pro Nutzer.
- `test_web_pages.py` - Manifest/Service Worker/Kartenseite, inkl. der Pruefung
  auf ausschliesslich relative Pfade (Ingress).

**Was bewusst NICHT getestet wird - und deshalb weiterhin von Hand gegen eine
Kopie der Produktiv-DB geprueft werden MUSS:**
`database.py::run_light_migrations()`. Das ist rohes, Postgres-eigenes SQL
(`information_schema`, `pg_constraint`, `ADD COLUMN IF NOT EXISTS`) und
scheitert auf SQLite schon an der Syntax; `conftest.py` ersetzt die Funktion
deshalb durch einen No-Op und baut die Tabellen direkt aus den Modellen. Die
Tests pruefen also das ZIEL-Schema, nicht den Weg dorthin - gerade bei der
Verschluesselungs-Migration ist das der gefaehrliche Teil. Ebenfalls offen: die
Kartenansicht hat ausser dem Ausliefern der Seite keine Tests (Leaflet-Logik im
Browser), und die vier uebrigen Repos haben weiterhin gar keine.

Zwei Dinge, die `conftest.py` bewusst tut und die man beim Erweitern kennen
sollte: SQLite prueft Fremdschluessel standardmaessig NICHT (ein `PRAGMA
foreign_keys=ON` schaltet das ein - ohne das waere der FK-Fehler in der
Loeschfunktion von 2026-09-09 gruen durchgelaufen), und der TestClient laeuft
ohne `with`, damit der `lifespan` und mit ihm die drei Hintergrund-Scheduler
nicht anspringen und nebenher auf derselben DB arbeiten.

## Verbrauch nach Aussentemperatur (`temperature.py`, ab 2026-09-21, v0.23.0)

Beantwortet fuer das eigene Fahrzeug, was sonst als Faustformel kursiert: wie
viel mehr braucht es im Winter. Drei Entscheidungen tragen den Rest.

**1. Wann die Temperatur gemessen wird: BEIM LADEBEGINN.** Das ist der Kern,
nicht ein Detail. `consumption.py` rechnet den Verbrauch eines Vorgangs N aus
der Strecke zwischen N-1 und N - der Wert beschreibt also eine FAHRT. Die endet
im Moment des Einsteckens; eine beim Ladeende gemessene Temperatur waere nach
Stunden an der Wallbox eine voellig andere. Deshalb merkt sich auch Home
Assistant den Wert beim `begin_charging_session` (wie SoC-Start und Lade-Art)
und der MyŠkoda-Poller in `MySkodaConfig.open_outside_temp_c`.

**2. Welche Temperatur zu welcher Fahrt gehoert: das Mittel beider Enden.**
`temp(N-1)` liegt ungefaehr am Anfang der Fahrt, `temp(N)` an ihrem Ende - also
das Mittel, wenn beide bekannt sind, sonst `temp(N)` allein. Fehlt `temp(N)`,
wird der Punkt verworfen statt auf `temp(N-1)` auszuweichen: zwischen dem
Einstecken davor und dieser Fahrt koennen Tage liegen. **Ausnahme seit
v0.24.1:** ein Wert vom Wetterdienst (`weather_daily`) ist bereits das Mittel
ueber genau diesen Zeitraum und wird unveraendert genommen - siehe
`temperature.py::_covers_the_whole_drive()` und den Abschnitt "Zeitraummittel
statt Punktwert".

**3. Gewichtet wird mit Kilometern**, wie beim Monatsdurchschnitt in
`routers/stats.py` - sonst zieht eine 5-km-Kurzstrecke mit unplausiblem Wert
eine ganze Temperaturklasse schief.

**Wo die Werte herkommen** (alle drei optional, alle drei gleichwertig):
Home-Assistant-Push (`schemas.AutoSessionPush.outside_temp_c`, ab
Lademonitor-HA 0.5.0), MyŠkoda-Poller (`myskoda.VehicleSnapshot.outside_temp_c`
- **unverifiziert**, ob die Public API sie ueberhaupt liefert; die Auswertung
klopft mehrere plausible Stellen ab und faellt sonst auf None zurueck, das
Debug-Protokoll zeigt das Ergebnis), oder von Hand im
Ladevorgangs-Formular.

**Nicht verschluesselt** (siehe crypto.py): eine Temperatur ist kein
personenbezogenes Datum, und die Auswertung filtert/sortiert per SQL danach.

**Wann bewusst NICHTS ausgewiesen wird.** Eine Ausgleichsgerade erscheint erst
ab `MIN_POINTS_FOR_TREND` (5) Fahrten UND `MIN_TEMP_SPAN_FOR_TREND_C` (8 Grad)
Spannweite. Vier Punkte zwischen 18 und 20 Grad ergeben rechnerisch auch eine
Steigung - nur sagt die nichts ueber den Winter, und einer Zahl sieht man das
nicht an. Zusaetzlich steht `r2` dabei (wieviel der Streuung die Temperatur
ueberhaupt erklaert). Vorgaenge ohne Temperatur werden gezaehlt und unter dem
Diagramm genannt, statt sie stillschweigend wegzulassen - Bestandsdaten haben
naturgemaess keine.

**Jahreszeiten** sind meteorologisch (Monatsgrenzen, Nordhalbkugel) - eine
bewusste Vereinfachung; die Temperaturklassen daneben sind davon unabhaengig
und bleiben ueberall richtig.

**Regression von Hand** (fuenf Summen fuer eine Gerade) statt numpy/scipy:
scipy haengt zwar ueber `reverse_geocoder` ohnehin im Image, ist aber genau
das Paket, das beim Bauen fuer fremde Architekturen schon Aerger gemacht hat.

### Darstellung (`templates/index.html`)

Drei Marken in einem Bild, weil erst sie zusammen die Frage beantworten: ein
Punkt je Fahrt (die Streuung - ohne sie wirkt jede Trendlinie ueberzeugender
als die Daten hergeben), das Mittel je 5-Grad-Klasse (das, was man ablesen
soll) und die Ausgleichsgerade (die Zusammenfassung). Darueber die eine Zahl
als Kennzahl: Mehrverbrauch bei 0 statt 20 Grad.

**Farben:** dieselben drei Farbtoene wie im Rest der Oberflaeche, aber je eine
Stufe dunkler (`#3d8ee0`/`#2ea87f`/`#bd8a26` statt `#5aa9ff`/`#4fd1a5`/
`#f2b84b`). Die hellen Toene sind fuer einzelne Balken und Badges gedacht; als
Feld aus ueber hundert Punkten auf dem dunklen Kartenhintergrund blenden sie.
Die drei Werte sind gegen den Kartenhintergrund auf Helligkeitsband,
Farbabstand bei Farbfehlsichtigkeit (Delta E >= 8) und Kontrast geprueft.

**Kein punktgenaues Zeigen:** der Tooltip sucht den naechstgelegenen Punkt im
Umkreis von ~24 px, ein 8-px-Ziel mittig zu treffen ist keine Bedienung. Die
Tabellenansicht unter dem Diagramm ist der barrierefreie Zwilling - jeder Wert
ist auch ohne Zeigegeraet erreichbar.

**Seitenverhaeltnis haengt an der Breite:** das SVG skaliert mit fester
Proportion, 720x320 wird auf 360 px Breite zu einem 160 px hohen Streifen. Unter
520 px bekommt es deshalb ein fast quadratisches Feld - und damit groessere
Schrift, weil die Einheiten mitskalieren.

**Jahreszeiten als liegende Balken**, nullbasiert. Der Unterschied zwischen
15,4 und 17,9 sieht auf einer Nullachse klein aus - das ist er auch; die Achse
abzuschneiden wuerde ihn kuenstlich vergroessern. Die Zahlen stehen direkt an
den Balken. (Die Balken stehen wie alle Diagramme dieser Art mittig in der
Karte statt sie auszufuellen - das haengt am gemeinsamen `renderBarChart` und
betrifft auch die Monatsdiagramme; bewusst nicht hier mitgeaendert.)

## Aussentemperatur vom Wetterdienst (`weather.py`, ab 2026-09-21, v0.24.0)

Die Auswertung aus dem vorigen Abschnitt lebt davon, dass ueberhaupt Werte da
sind. Sie kamen bis dahin nur aus dem Fahrzeug (HA-Push, MyŠkoda-Poller) oder
von Hand - **Bestandsdaten haben also keine, und wer weder HA noch einen
Fahrzeugsensor hat, bekommt nie welche**. Dieses Modul schlaegt sie stattdessen
am Ladeort nach.

**Das ist die DRITTE Ausnahme von der "kein Cloud-Dienst"-Linie - und die
erste, die der Server von sich aus macht.** Nominatim (`geocode.py`) laeuft nur
auf Nutzeraktion, die Kartenkacheln holt der Browser. Hier geht eine Koordinate
an einen fremden Server, moeglicherweise die des eigenen Zuhauses - also genau
die Sorte Datum, die seit 2026-09-09 verschluesselt in der DB liegt. Vier
Konsequenzen daraus:

1. **Opt-in pro NUTZER, Standard aus** (`User.weather_autofill_enabled`, nicht
   global wie SMTP): es sind die Daten des einzelnen Nutzers, also ist es auch
   seine Entscheidung. Ohne Schalter macht der Server keinen einzigen Abruf -
   `autofill_new_sessions()` filtert die Nutzer schon in der SQL-Abfrage.
2. **Koordinaten werden auf `COORD_PRECISION` (2) Nachkommastellen gerundet**,
   rund 1,1 km. Das kostet nichts: ERA5 rastert in 9-25 km, feinere Angaben
   landen im selben Gitterpunkt. Uebrig bleibt am fremden Server ein Ortsteil,
   keine Hausnummer.
3. **Was rausgeht, steht ueber dem Schalter**, nicht in einer Fussnote - plus
   ein eigener Punkt in der Datenschutzerklaerung (beide Sprachen).
4. **`weather_api_url`** kann auf eine selbst gehostete Open-Meteo-Instanz
   zeigen; dann verlaesst wieder nichts das eigene Netz.

### Zwei Endpunkte, weil keiner allein reicht

`/v1/forecast` liefert die juengste Vergangenheit ohne Verzug (bis 92 Tage
zurueck), das Archiv `/v1/archive` (ERA5) reicht bis 1940 zurueck, hinkt aber
rund fuenf Tage hinterher. `ARCHIVE_SWITCH_DAYS` (30) liegt bewusst INNERHALB
beider Fenster, damit kein Vorgang zwischen die Endpunkte faellt. Beim
oeffentlichen Dienst liegen sie auf zwei Hosts (`api.`/`archive-api.`), eine
eigene Instanz beantwortet beide Pfade - das entscheidet `archive_url_for()`.

### Gebuendelt wird nach Ort, nicht je Ladevorgang

Eine Anfrage deckt einen Ort und einen ganzen Datumsbereich ab. `CLUSTER_GAP_DAYS`
trennt nur echte Ausreisser ab (ein einzelner Vorgang aus einem lange
zurueckliegenden Urlaub), damit dafuer nicht Jahre an Stundenwerten uebertragen
werden. **Der Wert stand zuerst auf 7 Tagen und das war falsch herum gedacht:**
wer alle acht bis zehn Tage zuhause laedt - der Normalfall - bekam damit eine
Anfrage PRO Ladevorgang, also genau den Anfragensturm, den die Buendelung
verhindern soll. Im Live-Test gegen den echten Dienst sichtbar geworden (sechs
Anfragen fuer sechs Vorgaenge am selben Ort), seitdem 60 Tage: teuer ist die
ANZAHL der Anfragen (Rate-Limit), nicht die Antwortgroesse - ein Jahr
Stundenwerte sind rund 70 KB. `tests/test_weather.py` haelt genau diesen Fall
fest.

### Zeitzone

`start_time` liegt naiv als LOKALE Zeit in der DB, die API rechnet in UTC.
`_to_utc()` macht die Umrechnung genau einmal; die Anfrage laeuft immer mit
`timezone=UTC`. Ohne das liegen die Fenster (siehe Abschnitt "Zeitraummittel
statt Punktwert") um ein bis zwei Stunden verschoben - fuer den frueheren
Punktwert waren das direkt 2-3 K, also die Groessenordnung, die die Auswertung
ueberhaupt messen will.

### Woher die Koordinate kommt

`coordinates_for()`: eigenes GPS des Vorgangs zuerst, sonst die Position des
zugeordneten Ladeorts, sonst gar nichts. Spritmonitor-Importe haben nie
eigenes GPS - ueber den Ladeort bekommen sie trotzdem einen Wert. Vorgaenge
ohne beides werden gezaehlt und im Probelauf ausgewiesen, nicht geraten.

### Herkunft mitschreiben (`ChargingSession.outside_temp_source`)

`vehicle | manual | weather | weather_daily` (siehe Abschnitt darunter). Nicht Buchhaltung, sondern notwendig: ein
Wetterdienstwert ist nicht dasselbe wie der Fahrzeugsensor (Restwaerme, Sonne,
Standort - gern 1-2 K hoeher). Ohne die Spalte wuerde ein Sammel-Nachtrag beide
Arten unbemerkt vermischen und der Trend bekaeme einen Knick an genau dem Tag,
an dem jemand den Knopf gedrueckt hat. **Bestandszeilen bleiben NULL** - sie
sind alle `vehicle` oder `manual`, nur nicht mehr unterscheidbar, und ein
geratener Wert waere schlimmer als keiner.

Kein Client muss das Feld kennen: wer eine Temperatur schickt, ohne die
Herkunft zu nennen, hat sie von Hand eingetragen (`MANUAL`). Dabei gilt
dieselbe Vorsicht wie beim `energy_is_estimated`-Flag - Web-UI und Apps
schicken `outside_temp_c` bei JEDEM Speichern mit, deshalb zaehlt nur eine
tatsaechliche WERTAENDERUNG als Handeintrag. Sonst waere ein geholter Wert nach
einmal Oeffnen-und-Speichern als "von Hand" etikettiert.

### Zeitraummittel statt Punktwert (2026-09-21, v0.24.1)

**Jeder vom Wetterdienst geholte Wert ist seitdem das Mittel der Tagstunden
(6-20 Uhr LOKALZEIT) ueber den gesamten Zeitraum seit dem vorherigen
Ladevorgang** - `_interval_windows()` baut dafuer je Tag zwischen N-1 und N ein
Fenster, an den Raendern auf den tatsaechlichen Zeitraum beschnitten. Der
Punktwert zum Ladebeginn und die Interpolation auf die Stunde
(`_interpolate()`) sind ersatzlos entfallen.

Begruendung: der Verbrauch eines Vorgangs N stammt aus der Strecke zwischen
N-1 und N, beschreibt also ALLE Fahrten dieses Zeitraums (siehe Abschnitt
"Verbrauch nach Aussentemperatur", Punkt 1). Hier liegen zwischen zwei
Ladevorgaengen leicht zwei Wochen und zwanzig Fahrten - ein Messpunkt beim
Einstecken ist dafuer nur eine Tendenz. Der Wetterdienst KANN ueber den
Zeitraum mitteln, also tut er es.

Drei Sonderfaelle:

* **Zwei Ladungen am selben Tag** ergeben genau ein Fenster zwischen den beiden
  Uhrzeiten - dazwischen liegt ja auch nur diese eine Fahrt. Den ganzen Tag zu
  mitteln waere hier schlechter, nicht besser.
* **Kein Vorgaenger** (erster Vorgang eines Fahrzeugs) oder **gar keine
  Tagstunde im Zeitraum** (beide Ladungen nachts am selben Tag): zurueck auf
  die Tagstunden des Ladetages. Ein grober Wert ist besser als keiner.
* **Abstand groesser als `MAX_INTERVAL_DAYS` (60)**: gekappt. Ein groesserer
  Abstand ist keine Fahrt mehr, sondern eine Luecke (Urlaub ohne Auto,
  unvollstaendiger Import) - ueber Jahre zu mitteln ergaebe einen
  Jahresdurchschnitt, der ueber nichts mehr etwas aussagt.

**Werte aus dem Fahrzeug bleiben unberuehrt** (HA-Push, MyŠkoda-Poller): ein
Sensor misst zwangslaeufig punktuell. Genau deshalb schreibt der Wetterdienst
seitdem `TemperatureSource.WEATHER_DAILY` (`weather.WRITTEN_SOURCE`) statt
`WEATHER` - sonst laegen zwei verschiedene Messgroessen in derselben Spalte.
`WEATHER` steht seitdem nur noch fuer Bestandszeilen.

**`temperature.py` nutzt den Unterschied** (`_covers_the_whole_drive()`): ein
`weather_daily`-Wert wird unveraendert genommen statt wie bisher mit
`temp(N-1)` gemittelt - er deckt die Fahrt bereits ab, eine zweite Mittelung
wuerde die NACHBARfahrt hineinmischen und die bessere Angabe verwaessern. Fuer
Fahrzeug- und Handwerte bleibt es bei der Paarung beider Intervallenden.

**Der schaerfste Fall waren die Spritmonitor-Importe:** die tragen keine
Uhrzeit, `importer.py` setzt 00:00. Beim Wort genommen traf der alte Punktwert
dort das TAGESMINIMUM - an echten Stundendaten (Raum Stuttgart, 60 Tage) im
Mittel 3,9 K unter dem 6-20-Mittel, in der Spitze 8,8 K. Ein einseitiger Fehler
auf genau der Haelfte der Daten, also genau die Sorte, die eine Trendlinie
kippt, ohne dass man es der Zahl ansieht. Eine **`start_time` zu erfinden**,
die plausibler aussieht, waere der naheliegende, aber falsche Weg gewesen - das
Fehlen der Angabe bliebe dann nicht mehr erkennbar.

Warum 6-20 und nicht 24 h: nachts wird kaum gefahren. Das 24-h-Mittel laege
nochmal rund 1,6 K darunter. Gerechnet wird durchweg in LOKALER Zeit und erst
zum Schluss nach UTC geschoben (`_to_local_naive()`, `_to_utc()`); lokal 00:00
liegt in Mitteleuropa schon im UTC-Vortag, ein Umweg ueber die UTC-Datumsangabe
haette den falschen Tag erwischt.

**Die Koordinate ist die von Vorgang N, auch fuer die Tage davor** - wo das
Fahrzeug zwischendurch war, weiss niemand. Im Alltag ist das die Gegend, in der
auch gefahren wurde.

**Gebuendelt wird jetzt anders:** `fetch_temperatures()` sammelt erst alle
benoetigten Tage je Koordinate, holt sie (Vorhersage- und Archiv-Endpunkt
getrennt, `_cluster_dates()` wie bisher) und legt die Stundenwerte einer
Koordinate zu EINER Reihe zusammen; ausgewertet wird erst danach
(`_evaluate()`). Vorher entschied sich der Endpunkt je Vorgang - ein Intervall
kann aber laenger sein als ein Abfragebereich und sogar die Grenze zwischen
beiden Endpunkten ueberschreiten.

Die Migration in `database.py` ergaenzt den Enum-Wert per
`ALTER TYPE ... ADD VALUE IF NOT EXISTS` (idempotent; der neue Wert darf nur
nicht in DERSELBEN Transaktion schon benutzt werden, hier wird er
ausschliesslich angelegt).

**Korrekturlauf `refresh=true`**: wer den Nachtrag vor dieser Aenderung hat
laufen lassen, hat Punktwerte in der DB. `refresh` holt Werte neu, deren
Herkunft `weather`/`weather_daily` ist (`weather.WEATHER_SOURCES`), und laesst
`vehicle`/`manual` in Ruhe - das ist der Unterschied zu `overwrite`, das alles
ersetzt. Die Vorschau zeigt dann zusaetzlich `previous_temp_c`, sonst sieht man
dem Probelauf nicht an, ob sich ueberhaupt etwas aendert.

### Nachtrag und Automatik

- **Nachtrag** (`POST /api/weather/backfill?dry_run=`): Vorgabe ist der
  Probelauf, und der **fragt wirklich ab** und schreibt nur nichts. Eine
  Vorschau, die bloss zaehlt, wieviele Vorgaenge in Frage kaemen, saehe auch
  dann gut aus, wenn der Dienst gar keine Werte liefert. Vorhandene Werte
  bleiben unangetastet (`overwrite=false`). Bewusst unabhaengig vom Schalter:
  der Aufruf selbst ist die Einwilligung.
- **Automatik** laeuft im Scheduler (`main.py::_weather_scheduler_loop`, Takt
  wie WebDAV-Backup) und **nicht im Request-Pfad** von
  `POST /api/sessions/auto` - sonst haenge die Antwortzeit des HA-Pushes an
  einem fremden Server und ein langsamer Wetterdienst liesse die Automation des
  Nutzers ins Timeout laufen. Sie nimmt nur Vorgaenge der letzten
  `AUTOFILL_MAX_AGE_DAYS` (14): ohne Altersgrenze wuerde fuer einen Vorgang,
  zu dem es dauerhaft keinen Wert gibt, alle 15 Minuten bis in alle Ewigkeit
  erneut angefragt.

Fehler (Netz, Rate-Limit, unbekannter Ort) werfen NIE weiter - eine fehlende
Temperatur ist ein fehlendes Detail, kein Grund, einen Ladevorgang oder einen
ganzen Nachtrag scheitern zu lassen.

### Getestet ohne Netz, verifiziert mit Netz

`tests/test_weather.py` (35 Faelle) ersetzt den HTTP-Client durch einen
Transport, der die Anfragen mitschreibt: geprueft wird, was den Server
verlaesst (gerundete Koordinaten), welche Stunden in den Mittelwert eingehen
(Intervall ueber mehrere Tage, zwei Ladungen am selben Tag, Kappung, inkl.
umgestellter Prozess-Zeitzone fuer den naiven Fall), dass ein Fahrzeugwert
niemals durch einen Wetterdienstwert ersetzt wird, dass der Probelauf nichts
schreibt und dass ohne Opt-in keine einzige Anfrage entsteht. Die dafuer
gebauten Stundenreihen geben jedem Tag seine eigene Temperatur - am Ergebnis
laesst sich damit ablesen, WELCHE Tage eingegangen sind. Der echte Dienst
wurde daneben einmal von Hand gegen eine Test-Datenbank verifiziert (Werte
kamen ueber den Vorhersage-Endpunkt an, Bedienung im Browser durchgespielt).
**Bekannte Stolperstelle:** das freie Kontingent gilt pro IP - hinter CGNAT
oder auf einem geteilten VPS kann das Archiv mit 429 antworten, obwohl mit dem
eigenen Server alles stimmt.

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
  wegen FKs: Vehicles → Providers → Locations → Sessions.

  **Fix 2026-08-31:** Die ID-Pruefung war GLOBAL statt pro Nutzer
  (`if row["id"] in existing_vehicle_owners`). Folge: ein Export aus Konto A,
  importiert in Konto B DERSELBEN Instanz, uebersprang restlos alles ("0
  importiert, 46 uebersprungen") - die UUIDs existierten ja bereits, nur eben
  bei A. Der Restore auf einen frischen Server hatte den Fehler nicht, weil
  dort noch gar keine IDs vergeben sind. Jetzt entscheidet `_target_id()` nach
  Besitzer: ID unbekannt → Original-ID behalten (Restore-Fall, IDs bleiben
  stabil); ID gehoert dem importierenden Nutzer → echte Dublette,
  ueberspringen; ID gehoert einem ANDEREN Nutzer → neue UUID vergeben, weil
  die Primaerschluessel global sind, die Daten aber pro Nutzer getrennt.
  Die Fremdschluessel in locations.csv/sessions.csv laufen dafuer ueber
  `vehicle_id_map`/`provider_id_map`/`location_id_map`.
- Dublettenerkennung zusaetzlich ueber **Fachdaten**, nicht nur ueber die ID -
  sonst waere der Import nach obigem Fix nicht mehr idempotent (bei jedem
  erneuten Lauf haetten Ladeorte/Ladevorgaenge wieder neue IDs bekommen und
  waeren dupliziert worden; genau das hat der Test aufgedeckt). Schluessel:
  Fahrzeug = `external_id`, Anbieter = `name` (beide ohnehin pro Nutzer
  eindeutig, siehe `models.py __table_args__`), Ladeort = Name +
  gerundete Koordinaten, Ladevorgang = Fahrzeug + `start_time` (dasselbe Auto
  kann nicht zweimal zur selben Sekunde zu laden beginnen). Nebeneffekt: eine
  ZIP von einem ANDEREN Server (gleiche Fachdaten, voellig andere UUIDs) wird
  jetzt korrekt zusammengefuehrt statt dupliziert - und der Insert laeuft
  nicht mehr in einen IntegrityError (HTTP 500) wegen
  `uq_vehicles_user_external_id`.
- Web-UI: neue Sektion "Daten-Backup" unten in `settings.html`.
- Kein flexibles Multi-File-Upload mit Datei-Erkennung (bewusste
  Design-Entscheidung) - falls spaeter einzelne Tabellen unabhaengig
  im-/exportiert werden sollen, braeuchte es echte Spaltenerkennung wie beim
  Spritmonitor-Importer.

## Automatisches WebDAV-Backup (`webdav_backup.py`, `routers/webdav_backup.py`)

Laedt in konfigurierbarer Haeufigkeit dieselbe ZIP wie der manuelle Export
(dafuer wurde `export_backup()` in `routers/backup.py` in eine wiederverwendbare
`build_backup_zip(db, user)`-Funktion aufgeteilt) automatisch per WebDAV-PUT
auf ein Nutzer-Ziel hoch (z.B. Nextcloud).

- **Ein `WebdavBackupConfig` pro Nutzer** (nicht global) - konsistent mit der
  Pro-Nutzer-Datentrennung im Rest der App: jeder sichert nur seine eigenen
  Daten auf sein eigenes Ziel. `password` liegt optional verschluesselt in
  der DB (seit 2026-09-09, `FIELD_ENCRYPTION_KEY`, siehe Abschnitt
  "Verschluesselung personenbezogener Daten"), sonst Klartext - kein
  Secrets-Vault vorhanden, Postgres ist ohnehin nur via localhost im
  Container erreichbar. GET/PUT `/api/backup/webdav`
  geben das Passwort nie zurueck (nur `has_password: bool`) - ein leeres
  Passwort-Feld beim Speichern laesst ein bereits gesetztes Passwort
  unveraendert.
- **Kein neuer Scheduler-Dienst/Cron** - ein einzelner `asyncio`-Task
  (`main.py::_webdav_scheduler_loop`, per `lifespan`-Kontextmanager statt des
  deprecateten `@app.on_event`) prueft alle 15 Minuten per
  `asyncio.to_thread(run_due_backups)`, ob fuer irgendeinen Nutzer die
  konfigurierte Haeufigkeit (taeglich/woechentlich/monatlich, feste Tage-Werte
  1/7/30 statt echter Kalendermonate) abgelaufen ist. `to_thread` bewusst,
  weil die eigentliche Backup-Logik komplett synchron ist (sync SQLAlchemy
  `Session`, sync `httpx.Client`, wie der Rest der App) - ein Backup-Lauf soll
  den Event-Loop fuer alle anderen Requests waehrenddessen nicht blockieren.
- **Kein PROPFIND-Verzeichnislisting fuers Aufraeumen alter Backups** (WebDAV-
  Server antworten darauf mit teils sehr unterschiedlichem XML) - stattdessen
  fuehrt die neue Tabelle `WebdavBackupFile` selbst Buch ueber jede
  hochgeladene Datei; die Retention-Logik loescht daraus einfach alles
  aelter als `retention_days`, sowohl per WebDAV-DELETE als auch die DB-Zeile
  (bei fehlgeschlagenem DELETE bleibt die DB-Zeile bewusst stehen, damit der
  naechste Lauf es erneut versucht statt die Datei "zu vergessen").
- **Dateiname mit Mikrosekunden-Praezision**
  (`lademonitor-backup-%Y-%m-%dT%H-%M-%S-%f.zip`) statt nur Sekunden - bei
  einer Namenskollision (z.B. zweimal schnell hintereinander auf "Jetzt
  sichern" geklickt) wuerde die Retention-Logik sonst faelschlich die gerade
  erst hochgeladene Datei fuer eine gleichnamige aeltere DB-Zeile loeschen.
- Vor jedem Upload ein bestmoeglicher `MKCOL` auf den Zielordner (nicht
  rekursiv, Elternordner muessen existieren) - schlaegt bei den meisten
  Servern mit 405 fehl, wenn der Ordner schon da ist, das wird ignoriert.
- `POST /api/backup/webdav/run` dient als manueller "Jetzt sichern"-Button
  UND gleichzeitig als Verbindungstest (kein separater Test-Endpunkt) -
  einfacher, weil beides praktisch derselbe Codepfad ist.
- Web-UI: neue Sektion "Automatisches WebDAV-Backup" unten in
  `settings.html`, direkt unter "Daten-Backup".

## In-App-Versionierung (`backend/app/changelog.py`)

Nach dem Vorbild von [media-vault](https://github.com/halvar20000/media-vault)
(dortige `frontend/src/changelog.ts` + `ChangelogModal.tsx`), an die
server-rendered Jinja2-Architektur hier angepasst (keine separate
Frontend-API/Fetch noetig): `CHANGELOG` ist eine simple, absteigend sortierte
Python-Liste (`version`, `date`, `title`, `changes`), `VERSION` ist immer
`CHANGELOG[0]["version"]`. `main.py::_page()` reicht beides in jeden
Template-Kontext durch, `base.html` zeigt den Versions-Badge im Header (Klick
oeffnet ein Modal mit der vollen Historie, aktuelle Version bekommt ein
"Deine Version"-Badge, `<Config>`-freies reines Server-Rendering + minimales
Vanilla-JS zum Oeffnen/Schliessen). `/health` liefert `version` zusaetzlich
als JSON mit.

**Release-Workflow:** Bei einem Release einen neuen Eintrag oben in
`CHANGELOG` ergaenzen (+ denselben Text ins root `CHANGELOG.md`), COMMITTEN,
dann erst `git tag vX.Y.Z && git push origin vX.Y.Z` - das triggert den
GHCR-Publish-Workflow (siehe unten), der bei genau diesem Tag-Muster
`latest`/`X.Y.Z`/`X.Y` aktualisiert. Die Versionsnummer hier und der
Git-Tag sollten synchron bleiben (bewusst keine automatische Ableitung
z.B. aus `git describe` - der Wert im Modal soll auch bei einem
Dev-/Beta-Checkout ohne Git-Metadaten stabil und lesbar sein).

**Update 2026-08-24:** Der manuelle Sync ist genau beim allerersten Tag
(`v0.8.1`) schon einmal auseinandergelaufen (Tag gepusht, `changelog.py`
zeigte noch die vorherige Zwischenversion `1.0.0`) - dadurch zeigt das bereits
gebaute `v0.8.1`-Image dauerhaft faelschlich "v1.0.0" im Header (bewusst NICHT
per Tag-Neuvergabe korrigiert, siehe unten) - deshalb gibt es jetzt einen
harten Schutz statt nur der Konvention oben: der Schritt "Verify tag matches
in-app VERSION" in `.github/workflows/docker-publish.yml` laeuft bei jedem
`v*.*.*`-Tag-Push, vergleicht den Tag-Namen (ohne `v`-Prefix) mit
`backend.app.changelog.VERSION` per `python3 -c "from backend.app.changelog
import VERSION; ..."` und bricht den Build mit Fehler ab, wenn beides nicht
uebereinstimmt (Tag existiert dann zwar in Git, aber es wird kein Image
gebaut/gepusht - im Zweifel den Tag loeschen und mit passendem Changelog neu
setzen). Bewusst ein reiner String-Vergleich ohne SemVer-Parsing, da beide
Werte ohnehin von Hand gepflegt werden. Die eigentliche Korrektur kam als
neuer Tag `v0.8.2` (bewusst kein Force-Retag von `v0.8.1` - Nutzer hat sich
fuer den konventionelleren Weg entschieden, `v0.8.1` bleibt im GHCR mit dem
Anzeige-Bug stehen, aber nicht mehr `latest`).

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
   - `templates/lademonitor-server.xml`: Community-Applications-Template
     (v2-Schema, `ca_profile.xml` im Repo-Root fuers Repository-Profil) -
     `<Repository>` zeigt auf `ghcr.io/idomi94/lademonitor-server:latest`.
     Image wird per `.github/workflows/docker-publish.yml` (GitHub Actions)
     automatisch gebaut und nach GHCR gepusht - **Tag-Strategie bewusst
     getrennt**, damit Testen auf dem eigenen Server nicht versehentlich
     den `latest`-Tag fuer alle CA-Nutzer aktualisiert: jeder Push auf
     `main` baut nur `:beta` neu, `:latest` (+ `:X.Y.Z`/`:X.Y`) wird
     ausschliesslich bei einem Git-Tag `vX.Y.Z` aktualisiert (bewusster
     Release-Schritt, z.B. `git tag v0.1.0 && git push origin v0.1.0`).
     Das Template hat dafuer einen `<Branch>`-Tag-Selector (CA-UI:
     Stable/`latest` vs. Beta/`beta`), damit man in Unraid selbst ohne
     manuelles Editieren des Repository-Felds auf Beta wechseln kann.
     Struktur
     folgt dem offiziellen Unraid-CA-Starter-Repo (`ca_profile.xml` +
     `templates/*.xml`), damit die Einreichung ueber ca.unraid.net/submit
     durchlaeuft. **Einmalig nach dem ersten Workflow-Lauf noetig:** GHCR-Paket
     `lademonitor-server` in den GitHub-Package-Settings auf "Public" stellen,
     sonst kann Unraid das Image nicht pullen.
   - **Noch nicht auf echter Hardware getestet** (kein Docker in der
     Umgebung verfuegbar, in der das erstellt wurde) - beim ersten Test auf
     Unraid besonders pruefen: `reverse_geocoder`/`scipy`-Installation
     (`build-essential` ist als Sicherheitsnetz mit drin, falls kein
     vorgebautes Wheel fuer die Ziel-Architektur existiert), und ob `initdb`
     beim allerersten Start sauber durchlaeuft.
