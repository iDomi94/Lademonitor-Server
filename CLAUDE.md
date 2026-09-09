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
    geocoding.py, backup.py, auth.py, webdav_backup.py, myskoda.py, email.py
  auth.py            - Passwort-Hashing, Token-Handling, Auth-Dependencies
  myskoda.py         - Client fuer die offizielle MyŠkoda Public API (sync httpx)
  myskoda_poller.py  - Zustandsmaschine der automatischen Ladeerkennung + Debug-Log
  mailer.py          - SMTP-Versand, Mail-Vorlagen, Versandprotokoll
  notifications.py   - Benachrichtigungen (ereignisgetrieben + zeitgesteuert)
  templates/          - Jinja2 Web-UI (index=Dashboard, sessions, settings +
                          die Unterseiten import, settings_backup, settings_api,
                          settings_email; auth_base.html fuer die Seiten ohne
                          Anmeldung; emails/ fuer die Mail-Vorlagen)
  static/style.css, static/filter.js, static/ui.js
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
HA liefert eine eigene, der Poller nutzt `myskoda-<FIN>-<Startzeit>`).

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

`api_key` liegt im Klartext in der DB wie alle uebrigen Zugangsdaten dieser
App (Auth-Tokens, WebDAV-Passwort) - kein Vault vorhanden, Postgres ist nur
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

- **Kein Rate-Limiting auf `/api/auth/login`/`/register`** - seit die App
  oeffentlich per Nginx erreichbar ist, kein Schutz gegen automatisiertes
  Passwort-Raten oder Spam-Registrierungen. Bewusst zurueckgestellt (starkes
  Passwort des Nutzers als aktuelle Absicherung), waere ein separater,
  ueberschaubarer Zusatz (z.B. `slowapi` oder Nginx-seitig). **Der
  Passwort-vergessen-Endpunkt hat seit v0.14.0 ein eigenes Limit** (3 pro Konto
  und Stunde plus globale Obergrenze), weil er Mails ausloest - die beiden
  anderen bleiben offen.
- **Auth-Tokens ueberleben einen Passwort-Reset nicht** (siehe E-Mail-Abschnitt):
  danach muessen der Home-Assistant-Token und die iOS-Anmeldung neu eingetragen
  werden. Die saubere Loesung waeren typisierte, benannte API-Tokens
  (`kind: session | api`) mit Widerruf pro Eintrag - noch nicht umgesetzt.
- **Verschluesselung der gespeicherten Zugangsdaten** (SMTP-, WebDAV-Passwort,
  MyŠkoda-Key) waere nur mit einem Schluessel ausserhalb von `/config` sinnvoll,
  also ueber eine Env-Variable im Unraid-Template. Bewusst zurueckgestellt,
  siehe Begruendung im E-Mail-Abschnitt. **Update:** genau dieser Mechanismus
  (Env-Variable `FIELD_ENCRYPTION_KEY`) existiert seit 2026-09-09 fuer GPS-
  Koordinaten/Notizen (siehe Abschnitt "Verschluesselung personenbezogener
  Daten") - liesse sich grundsaetzlich auf diese Secrets ausweiten, ist aber
  noch nicht passiert.
- **Feld-Verschluesselung deckt noch nicht alles ab:** Namen (Fahrzeug/
  Anbieter/Ladeort) und `User.email` sind mangels Blind-Index-Loesung noch
  Klartext (haengen an DB-Unique-Constraints), und der eingebaute CSV-Export/
  das WebDAV-Backup liefern GPS/Notizen weiterhin unverschluesselt (siehe
  Abschnitt "Verschluesselung personenbezogener Daten" fuer Details).
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

**Hauptleiste: Dashboard, Ladevorgaenge, Einstellungen.** Der Import ist von
dort verschwunden - er wird einmal beim Umstieg von Spritmonitor gebraucht und
belegte dauerhaft einen von vier Plaetzen.

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
zu nehmen - Schluessel und DB wuerden sonst immer gemeinsam abfliessen. Bewusst
zurueckgestellt.

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
namens "Zuhause") sowie `ChargingSession.notes` und `.geocoded_place`. NICHT
verschluesselt: Namen (Vehicle/Provider/ChargingLocation - haengen an
Unique-Constraints pro Nutzer, siehe Abschnitt "Pro-Nutzer-Datentrennung"
weiter oben, eine deterministische Verschluesselung/Blind-Index dafuer ist
noch offen), `User.email` (haengt am funktionalen `lower(email)`-Unique-Index
fuer den Login/Reset-Lookup, siehe Abschnitt "Authentifizierung" - ebenfalls
ein Blind-Index-Thema), sowie die bereits vorher bekannten Klartext-Secrets
(SMTP-/WebDAV-Passwort, MyŠkoda-API-Key - siehe deren jeweilige Abschnitte
und "Bekannte offene Punkte" unten, unveraendert).

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
`main.py` ruft `crypto.require_key()` vor jedem DB-Zugriff auf: fehlt die
Variable oder ist sie kein gueltiger Fernet-Schluessel, startet die App
bewusst gar nicht erst statt still unverschluesselt weiterzulaufen. **Das ist
ein Breaking-Update** - bestehende Installationen (Unraid-Template, Docker
Compose) muessen den Schluessel VOR dem Update setzen, sonst startet der
Container nach dem Update nicht mehr (Unraid-Template hat dafuer ein neues
Pflichtfeld, `.env.example` fuer Compose entsprechend ergaenzt).

**Migration Bestandsdaten** (`database.py::run_light_migrations()`): laeuft
wie alle anderen leichten Migrationen bei jedem Container-Start und ist
idempotent. Fuer die GPS-Spalten (bisher `DOUBLE PRECISION`) wird der
Spaltentyp per Umbenennungs-Trick (neue VARCHAR-Spalte, Python-seitig pro
Zeile verschluesselt befuellen, alte Spalte droppen, neue umbenennen -
gleiches Muster wie schon bei der `auth_tokens.token`->`token_hash`-Migration)
auf verschluesselten Text umgestellt; `not_null=True` bei
`ChargingLocation.latitude/longitude` haelt die urspruengliche
NOT-NULL-Eigenschaft ueber den Umbau hinweg aufrecht. Fuer `notes`/
`geocoded_place` (bereits VARCHAR/TEXT) genuegt ein Versuch, jeden
Bestandswert zu entschluesseln (`crypto.is_encrypted()`) - schlaegt das fehl,
war der Wert noch Klartext und wird verschluesselt. Auf einer komplett neuen
Installation legt `create_all()` diese Spalten direkt als VARCHAR an, die
Migration ist dort ein No-Op. **Vor dem Einsatz auf der echten Installation
unbedingt gegen eine Kopie der Produktiv-DB testen** - eine fehlgeschlagene
Verschluesselung von Bestandsdaten ist nicht trivial rueckgaengig zu machen.

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
  Daten auf sein eigenes Ziel. `password` liegt im Klartext in der DB, wie
  auch die Auth-Tokens - kein Secrets-Vault vorhanden, Postgres ist ohnehin
  nur via localhost im Container erreichbar. GET/PUT `/api/backup/webdav`
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
