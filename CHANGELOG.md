# Changelog

Alle nennenswerten Änderungen an Lademonitor stehen hier. Dieselbe Historie ist
auch in der App sichtbar – auf den Versions-Badge im Header klicken.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/), Versionen
folgen [Semantic Versioning](https://semver.org/).

## [0.23.0] — 2026-09-21

### Added
- **Verbrauch nach Außentemperatur** im Dashboard: Streudiagramm (eine Fahrt =
  ein Punkt) mit Mittelwert je 5-°C-Klasse und Ausgleichsgerade, darüber als
  Kennzahl der Mehrverbrauch bei 0 °C gegenüber 20 °C, dazu ein Vergleich der
  vier Jahreszeiten. Neuer Endpunkt `GET /api/stats/temperature`.
- **`ChargingSession.outside_temp_c`**: Außentemperatur beim Ladebeginn –
  wahlweise von Home Assistant (ab Lademonitor-HA 0.5.0), vom MyŠkoda-Poller
  (falls die API sie liefert) oder von Hand im Ladevorgangs-Formular. Wandert
  in den Backup-Export; ältere Backup-ZIPs bleiben importierbar.

### Notes
- Die Auswertung berücksichtigt nur Ladevorgänge mit Temperatur und nennt die
  Anzahl der übrigen. Eine Ausgleichsgerade gibt es erst ab fünf Fahrten und
  mindestens 8 °C Spannweite, zusammen mit dem Bestimmtheitsmaß R².

## [0.22.0] — 2026-09-20

### Added
- **Web-Oberfläche als App installierbar** ("Zum Startbildschirm hinzufügen"):
  Web-App-Manifest und ein bewusst nicht zwischenspeichernder Service Worker.
- **`GET /api/sync/deletions`**: meldet ausdrücklich, welche Datensätze auf dem
  Server gelöscht wurden, damit die Apps ihren lokalen Spiegel aufräumen können,
  ohne das aus dem Fehlen eines Eintrags raten zu müssen.
- **Automatisierte Tests** (`backend/tests/`) und ein GitHub-Actions-Workflow
  dafür. Nicht Teil des Images.

### Fixed
- **Ladevorgangs-Liste zeigte überall nur die 200 neuesten Einträge** – in der
  Web-Oberfläche, auf der Karte, in der iOS- und in der Android-App.
  `GET /api/sessions` hatte `limit=200` als Vorgabe, und kein Client schickte je
  einen Wert mit. Nach einem Spritmonitor-Import mit mehreren hundert Vorgängen
  waren die älteren nicht erreichbar und wurden von den Apps nie gespiegelt; die
  Statistik zeigte trotzdem vollständige Zahlen, weil sie serverseitig
  aggregiert wird. Ohne Angabe liefert der Endpunkt jetzt alles,
  `limit`/`offset` bleiben für seitenweises Laden verfügbar.
- **Serverseitige Löschungen kamen in den Apps nie an** ("Geisterzeilen"):
  gelöschte Ladevorgänge, Fahrzeuge, Anbieter und Ladeorte verschwinden jetzt
  beim nächsten Abgleich auch auf dem Gerät.

## [0.21.0] — 2026-09-16

### Changed
- **Ein Klick auf einen Ladeort listet dessen Ladevorgänge auf**, statt
  direkt das Bearbeiten-Formular zu öffnen – nachsehen, was man dort
  überhaupt geladen hat, war vorher gar nicht möglich. Eine Zeile öffnet die
  Vorschau, das Bearbeiten des Ortes liegt als Knopf unter der Liste. Mit
  aufgeführt werden auch Ladevorgänge, die im Radius des Ortes liegen, aber
  noch keinem Ort zugeordnet sind (genau die, die das Geo-Matching diesem
  Ort zuschlagen würde).

### Fixed
- **Die Liste eines Clusters erschien praktisch nie.** Sie kam nur, wenn die
  Punkte enger als 11 Meter beieinanderlagen – die GPS-Punkte mehrerer
  Ladevorgänge an derselben Wallbox streuen aber um einige zehn Meter. Man
  zoomte deshalb bis zum Anschlag und stand dann vor einem Cluster, das sich
  nicht mehr auflöste. Ob Hineinzoomen überhaupt noch etwas trennt,
  entscheidet sich jetzt in Bildschirmpixeln bei maximalem Zoom.

## [0.20.0] — 2026-09-15

### Added
- **Kartenansicht** (neue Seite „Karte“ in der Hauptnavigation): zeigt alle
  bekannten Ladeorte und alle Ladevorgänge mit eigenen Koordinaten gemeinsam
  auf einer OpenStreetMap-Karte – das Gegenstück zum Karten-Tab der iOS-App
  (`MapOverviewView.swift`), das es im Web bislang gar nicht gab. Ladeorte
  tragen ihren Matching-Radius als Kreis, damit sichtbar wird, wie nah ein
  Ladevorgang liegen muss, um automatisch diesem Ort zugeordnet zu werden.
- **Zoom-abhängiges Clustering der Ladevorgänge** (dieselbe Single-Linkage-
  Logik wie in der App): ein Klick auf einen Zahlen-Marker zoomt hinein,
  solange sich die Punkte noch auftrennen lassen – liegen sie praktisch am
  selben Punkt (mehrfach an derselben Wallbox geladen), öffnet sich
  stattdessen eine Liste der betroffenen Ladevorgänge. Noch nicht geprüfte
  Einträge (`needs_review`) sind orange statt blau.
- **Vorschau und Schnellaktionen**: ein Klick auf einen einzelnen
  Ladevorgang öffnet eine Detailvorschau; von dort lässt er sich direkt
  bestätigen (nimmt das Prüf-Flag, ohne das Formular zu öffnen) oder auf der
  Ladevorgänge-Seite bearbeiten (`sessions#edit=<id>`). Ein Klick auf einen
  Ladeort öffnet dessen Bearbeiten-Dialog gleich auf der Karte.
- **Legende als Filter** (Ladeorte/Ladevorgänge einzeln ausblenden), der
  bestehende Zeitraum-Filter wirkt auch auf der Karte, und ein Knopf zoomt
  auf den eigenen Standort.

### Changed
- **Leaflet liegt lokal im Image** (`backend/app/static/vendor/leaflet/`)
  statt an einem CDN – dieselbe Überlegung wie bei der seinerzeitigen
  Entfernung von Chart.js, weil nicht jede Installation CDN-Zugriff hat. Nur
  die Kartenkacheln selbst kommen naturgemäß von openstreetmap.org.
- **Datenschutzerklärung** um einen Abschnitt zu den Kartenkacheln ergänzt
  (der Browser kontaktiert dabei OpenStreetMap direkt; die Ladedaten selbst
  werden nicht übermittelt).

## [0.19.0] — 2026-09-14

### Added
- **Rate-Limiting auf `/api/auth/login` und `/api/auth/register`**: neuer,
  leichtgewichtiger In-Memory-Limiter pro Client-IP (kein Redis/slowapi
  nötig) – Login max. 20 Versuche pro 5 Minuten, Registrierung max. 5 pro
  Stunde, danach HTTP 429. Bremst automatisiertes Passwort-Raten und
  Spam-Registrierungen aus, jetzt der Server öffentlich erreichbar ist.
  Zusätzliche Schutzschicht, kein Ersatz für ein starkes, einzigartiges
  Passwort.

### Fixed
- **Nutzername war case-sensitiv**, obwohl die Anmeldung mit E-Mail-Adresse
  Groß-/Kleinschreibung schon länger ignoriert – "Domi" und "domi" wären
  zwei getrennte Konten gewesen. Login, Registrierung und Admin-Nutzeranlage
  vergleichen den Nutzernamen jetzt einheitlich unabhängig von Groß-/
  Kleinschreibung.

## [0.18.0] — 2026-09-13

### Changed
- **Neues Logo**: gestauchte, comic-hafte Fahrzeugsilhouette nach dem Vorbild
  des Škoda Enyaq, dazu Ladesäule und gestricheltes Kabel in Electric Green auf
  dunklem Grund. Ersetzt das bisherige Symbol (Auto-Umriss mit Blitz) in
  Browser-Tab, Lesezeichen, Startbildschirm und PWA-Installation. Entwürfe und
  Generator liegen unter `design/logo/`.
- Favicon-Größen (16 und 32 px) nutzen eine **reduzierte Fassung** desselben
  Motivs – ohne Türfugen, Griffe und Leuchten, dafür mit kräftigerem Kabel; die
  volle Zeichnung zerläuft in dieser Größe zu einem Fleck. Das
  `apple-touch-icon` ist randlos quadratisch, weil iOS seine eigene Maske
  darüberlegt.
- Icon-Verweise in den Seitenköpfen tragen jetzt wie `style.css` eine
  Versionsnummer (`?v=…`) – Browser halten ein Favicon besonders hartnäckig
  fest.
- **Navigationsleiste**: Wort-Bild-Marke statt des bisherigen ⚡-Emojis. Das
  Zeichen liegt dort **quer** – in der Leiste gilt die Quadrat-Beschränkung des
  App-Icons nicht, quer passt die ganze Szene bei gleicher Höhe rund dreimal so
  breit hinein und bleibt lesbar. Größe von Zeichen und Schriftzug regelt
  `clamp()` fließend, damit Marke, Versions-Badge und Menü-Knopf auch auf 320 px
  eine Zeile bleiben, ohne einen zweiten Breakpoint einzuführen.
- **Anmeldeseiten** (Anmelden, Registrieren, Passwort vergessen, Passwort
  setzen, Adresse bestätigen) tragen die Marke ebenfalls – sie haben keine
  Navigationsleiste und waren dadurch als einzige Seiten ganz ohne
  Markenzeichen. Dort größer und zentriert über der Karte; die Grundform liegt
  nur einmal in `style.css`, damit beide Stellen nicht auseinanderlaufen.
- **Datenschutzseite** trägt die Marke ebenfalls – linksbündig über dem
  Zurück-Link statt zentriert, weil sie ein Fließtext-Dokument in voller
  Breite ist und keine zentrierte Karte. Dort fehlte außerdem noch die
  Versionsnummer an den Icon-Verweisen.

### Fixed
- Das von `templates/lademonitor-server.xml` und `ca_profile.xml` verlinkte
  `icon.png` existierte nicht im Repo; der Eintrag im Unraid-App-Katalog hatte
  dadurch ein kaputtes Bild. Die Datei wird jetzt miterzeugt.

## [0.17.0] — 2026-09-09

### Added
- **Datenschutzerklärung** unter `/privacy`, öffentlich erreichbar und im
  Footer jeder Seite sowie bei der Registrierung verlinkt. Beschreibt konkret
  Kontodaten, Ladeorte/-vorgänge, Fahrzeugdaten sowie die drei einzigen Fälle,
  in denen Daten den Server verlassen (OpenStreetMap Nominatim für die
  Adresssuche, die optionale MyŠkoda-Anbindung, ein selbst konfiguriertes
  WebDAV-Backup) – jeweils klar als optional gekennzeichnet.
- Verantwortlicher (Name/Anschrift/Kontakt-E-Mail) über die neuen,
  optionalen Umgebungsvariablen `PRIVACY_CONTROLLER_NAME`,
  `PRIVACY_CONTROLLER_ADDRESS`, `PRIVACY_CONTROLLER_EMAIL` konfigurierbar
  (siehe `.env.example`) – datenschutzrechtlich verantwortlich ist der
  Betreiber der jeweiligen Installation. Ohne gesetzte Variablen zeigt die
  Seite einen deutlichen Hinweis statt unvollständiger Angaben.

## [0.16.0] — 2026-09-09

### Added
- **Eigenes Konto löschen**, unter "Mein Konto" mit dem aktuellen Passwort
  bestätigt. Löscht alle eigenen Daten: Fahrzeuge, Ladevorgänge, Anbieter,
  bekannte Ladeorte, MyŠkoda- und WebDAV-Backup-Konfiguration samt
  Protokollen sowie das eigene Mail-Versandprotokoll. Der letzte verbliebene
  Admin kann sich nicht selbst löschen.

### Fixed
- Die bestehende Admin-Löschfunktion (Benutzerverwaltung) räumte bisher nur
  Nutzer-Datensatz und Sitzungen auf, nicht aber Fahrzeuge, Ladevorgänge,
  Anbieter usw. – bei einem Konto mit echten Daten schlug das an Postgres'
  Fremdschlüssel-Constraint mit einem Serverfehler fehl. Beide Löschwege
  räumen jetzt vollständig auf.

## [0.15.0] — 2026-09-09

### Added
- **Optional: GPS-Koordinaten (Ladeorte und Ladevorgänge, auch ein gerade
  laufender MyŠkoda-Ladevorgang), Notizen (Ladevorgänge und Anbieter),
  automatisch ermittelte Ortsnamen, Fahrzeug-/Ladeort-Namen, die
  Fahrzeug-Identifizierungsnummer (VIN), das MyŠkoda-Debug-Protokoll sowie
  gespeicherte Zugangsdaten (SMTP-Passwort, WebDAV-Adresse/-Nutzername/
  -Passwort, MyŠkoda-API-Key) können jetzt verschlüsselt in der Datenbank
  gespeichert werden** statt im Klartext – relevant, sobald der Server
  öffentlich erreichbar ist statt nur im eigenen Heimnetz. Standardmäßig
  AUS, keine Aktion nötig für einen reinen Heimnetz-Betrieb.

  Aktivieren über die neue optionale Umgebungsvariable
  `FIELD_ENCRYPTION_KEY`:
  ```bash
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
  und sicher aufbewahren (z.B. Passwort-Manager) – **nicht** im Datenverzeichnis
  (`/config`) ablegen, sonst landet er im selben Backup wie die damit
  verschlüsselten Daten. Einmal gesetzt und benutzt nicht mehr entfernen – ein
  verlorener oder entfernter Schlüssel macht die betroffenen Felder dauerhaft
  unlesbar, der Server verweigert dann bewusst den Start. Bestehende
  Klartextwerte werden beim nächsten Start mit gesetztem Schlüssel automatisch
  migriert, auch wenn er erst später gesetzt wird. Der aktuelle Stand steht
  als Badge unten in den Einstellungen.

  Kein Zero-Knowledge-Schutz: der Schlüssel liegt im Server-Environment, der
  Server entschlüsselt weiterhin transparent bei jedem Request. Schützt gegen
  Diebstahl von Datenbank/Backup/Datenträger, nicht gegen Einsicht durch, wer
  auch immer den laufenden Server-Prozess kontrolliert. Bewusst NICHT
  verschlüsselt (an SQL-Gleichheitsvergleiche bzw. Eindeutigkeits-Prüfungen pro
  Nutzer gebunden): Fahrzeug-Kurzschlüssel (`external_id`, Home-Assistant-Push),
  Anbieter-Name, Nutzername, E-Mail-Adresse. Der CSV-Export und das
  automatische WebDAV-Backup bleiben bewusst menschenlesbar und damit
  unverschlüsselt.

  Bei einer eigenen DSGVO-Prüfung (systematisch alle Datenbankfelder
  durchgegangen) gefunden und mitkorrigiert: der MyŠkoda-Poller baute die
  interne Dubletten-Kennung eines Ladevorgangs aus der VIN
  (`myskoda-<VIN>-<Startzeit>`) – die wäre trotz verschlüsselter VIN-Spalte
  über dieses zweite, für den Dublettencheck zwingend unverschlüsselte Feld
  wieder im Klartext gelandet. Läuft jetzt über die interne, nicht
  personenbezogene Fahrzeug-ID statt der VIN.

## [0.14.1] — 2026-09-08

### Changed
- **`PUT /api/auth/password` gibt den neuen Token zurück** (`{token, user}` mit
  Status 200) statt eines leeren 204.

  Der Wechsel beendet weiterhin alle Sitzungen des Nutzers und stellt sofort
  eine neue aus – die wurde bisher aber nur als Cookie gesetzt, half also
  ausschließlich der Web-Oberfläche. Ein Client, der sich per
  `Authorization: Bearer` anmeldet (die iOS-App, Home Assistant), hatte seinen
  Token gerade selbst ungültig gemacht, bekam keinen neuen und war beim
  nächsten Aufruf abgemeldet: er musste sich mit dem neuen Passwort ein zweites
  Mal anmelden, obwohl der Server die Sitzung längst erzeugt hatte.

  Für die Web-Oberfläche ändert sich nichts – sie liest nur den Status und
  fährt mit dem Cookie weiter. Und weil 200 wie 204 ein Erfolg ist, läuft auch
  ein Client weiter, der die Antwort gar nicht ausliest.

## [0.14.0] — 2026-09-08

### Added
- **SMTP-Zugang** (Einstellungen → E-Mail, nur für Admins). Gilt bewusst für
  die **gesamte Installation und nicht pro Nutzer** wie das WebDAV-Backup: die
  "Passwort vergessen"-Mail muss gerade dann verschickt werden können, wenn
  niemand angemeldet ist – eine am Nutzer hängende Konfiguration wäre in dem
  Moment nicht erreichbar. Mit Testmail (der einzige ehrliche Verbindungstest)
  und einem Versandprotokoll, ohne das ein fehlgeschlagener Versand unsichtbar
  bliebe.
- **E-Mail-Adresse pro Nutzer**, selbst pflegbar unter "Mein Konto", von Admins
  in der Benutzerverwaltung nachtragbar. Eine neue Adresse bekommt einen
  Bestätigungslink; **nur eine bestätigte Adresse darf ein Passwort
  zurücksetzen** – bei einem Tippfehler hielte sonst ein Fremder einen gültigen
  Token für ein fremdes Konto in der Hand.
- **"Passwort vergessen"**: Link per Mail, eine Stunde gültig, einmal
  verwendbar. Der Endpunkt antwortet **immer** mit 204 – auch bei unbekanntem
  Konto, fehlender Adresse oder erreichtem Limit; jede Unterscheidung wäre ein
  Verzeichnis aller Nutzernamen und Adressen dieses Servers. Höchstens drei
  Anfragen pro Konto und Stunde.
- **Eigenes Passwort ändern** – gab es bisher überhaupt nicht, das Passwort war
  nach der Registrierung unveränderbar.
- **Einladungen**: Admins legen ein Konto an, der Nutzer setzt sein Passwort
  über einen Link selbst. Kein vom Admin vergebenes Startpasswort, das über
  einen zweiten Kanal übermittelt werden müsste und danach zwei Leuten bekannt
  wäre. Das Einlösen bestätigt zugleich die Adresse.
- **Benachrichtigungen**, einzeln pro Nutzer abschaltbar:
  - fehlgeschlagenes WebDAV-Backup (Übergang "läuft" → "kaputt", danach
    höchstens wöchentlich erinnert). Das ist die wertvollste davon: ein
    nächtliches Backup, das seit Wochen scheitert, sieht man sonst nur beim
    zufälligen Öffnen der Einstellungen – und ein Backup, das man fälschlich
    für laufend hält, ist schlimmer als keins.
  - MyŠkoda-Anmeldung fehlgeschlagen (`auth_error`) und Vorwarnung 14/7/1 Tage
    vor Ablauf des API-Keys. Beides steht sonst still, bis jemand die fehlenden
    Ladevorgänge bemerkt.
  - Sammelmeldung über zu prüfende Ladevorgänge, pro Nutzer **aus / täglich /
    wöchentlich**.
  - Monatsbericht mit kWh, Kosten, Ø-Preis, Ø-Verbrauch, km und Anbietern –
    berechnet über denselben Code wie das Dashboard, damit Mail und Web-UI
    nicht auseinanderlaufen können.
  - Meldung an Admins bei neuer Registrierung (die ist bewusst offen; bisher
    fiel ein neues Konto nur beim Nachsehen auf).

### Changed
- **Anmeldung mit Nutzername ODER E-Mail-Adresse**, Groß-/Kleinschreibung der
  Adresse egal. Das Feld heißt in der API weiterhin `username`, damit die
  iOS-App und der Home-Assistant-Login unverändert funktionieren.
- **Beim Zurücksetzen und beim Ändern des Passworts werden alle Sitzungen
  beendet.** Wäre das Konto übernommen worden, liefe die Sitzung des Angreifers
  sonst weiter. Konsequenz in genau dieser App: der
  Home-Assistant-`rest_command`-Token und die iOS-Anmeldung sterben mit und
  müssen neu eingetragen werden – Mail, Bestätigungsseite und Einstellungen
  weisen darauf hin.
- Die Anmeldeseiten (Login, Registrieren + die drei neuen) teilen sich ein
  gemeinsames Grundgerüst `auth_base.html`. Vorher trugen Login und Registrieren
  denselben Style-Block je einmal selbst; mit drei weiteren Seiten wären daraus
  fünf Kopien geworden. Nebenbei haben sie jetzt ein Favicon (bisher `/favicon.ico`
  → 404).

### Security
- **Anmelde-Tokens liegen nicht mehr im Klartext in der Datenbank**, sondern als
  SHA-256-Hash. Ein Auth-Token *ist* eine fertige Anmeldung – wer die Tabelle
  lesen konnte, war damit sofort jeder Nutzer. Bewusst SHA-256 statt bcrypt: der
  Lookup geht über Gleichheit, ein gesalzener Hash ließe sich gar nicht suchen,
  und ein `token_urlsafe(32)` ist bereits 256 Bit Zufall – es gibt nichts zu
  erraten. Die Migration hasht bestehende Zeilen und entfernt danach die
  Klartextspalte; **niemand wird ausgeloggt**.
- Dieselbe Behandlung für die neuen Einmal-Tokens (Reset, Bestätigung,
  Einladung).
- Nutzer-Passwörter waren und bleiben bcrypt-gehasht; Klartext existiert nur im
  Request-Objekt und wird weder gespeichert noch geloggt noch exportiert.
- **Das SMTP-Passwort muss im Klartext liegen** – SMTP-AUTH überträgt es beim
  Anmelden, aus einem Hash ließe es sich nicht zurückgewinnen (beim Nutzer-Login
  wird dagegen nur *verglichen*). Der wirksame Schutz ist deshalb nicht
  Verstecken, sondern den Wert begrenzen: die Einstellungen empfehlen ein
  app-spezifisches Passwort bzw. ein eigenes Absender-Konto. Es wird über die
  API nie zurückgegeben (nur `has_password`) und nicht in die Backup-ZIP
  exportiert – dieselbe Linie wie beim MyŠkoda-API-Key.
- Die **Basis-Adresse für Links in Mails wird konfiguriert, nicht aus dem
  Request abgeleitet.** Der `Host`-Header kommt vom Client; wer ihn beim
  "Passwort vergessen"-POST fälscht, ließe dem Opfer sonst eine Mail mit einem
  Link auf seine eigene Domain zustellen (Host-Header-Poisoning). Außerdem gibt
  es bei geplanten Mails gar keinen Request.
- Reset- und Bestätigungslinks lösen **nichts per GET aus** – die Seiten zeigen
  ein Formular, gesetzt wird per POST. Mail-Scanner rufen Links beim Vorschauen
  automatisch ab und würden den Token sonst verbrauchen.

## [0.13.0] — 2026-09-08

### Fixed
- **Die Ladevorgangs-Tabelle musste am Rechner seitlich geschoben werden – bei
  jeder Fensterbreite.** Der Seiteninhalt ist auf 1100 px begrenzt (`.container`),
  die zwölf Spalten brauchten rund 1180 px; der `.tablewrap` scrollte also
  konstant um ~130 px, auch auf einem 1920-px-Bildschirm. Zwei Ursachen, zwei
  Gegenmaßnahmen:
  - Die Ladevorgangs-Seite darf bis 1500 px breit werden (`.container.wide`).
    Nur diese eine Seite – Dashboard und Einstellungen bleiben bei 1100 px,
    dort ist die volle Fensterbreite unangenehm zu lesen.
  - Die Aktionsspalte war mit den beschrifteten Knöpfen "Bearbeiten"/"Löschen"
    die breiteste Spalte der Tabelle (~200 px). Jetzt zwei quadratische
    Icon-Knöpfe (Stift/Papierkorb, ~76 px), Beschriftung als `title`/`aria-label`.

  Ergebnis: ab 1100 px Fensterbreite kein horizontaler Überlauf mehr (vorher
  128 px bei jeder Breite).

### Changed
- **Icon-Knöpfe in allen per JS gerenderten Tabellen** – Ladevorgänge,
  Fahrzeuge, Anbieter, Ladeorte, Benutzerverwaltung – sowie auf den
  Ladevorgangs-Karten der Handy-Ansicht. Inline-SVG statt Emoji
  (`static/ui.js`), weil Emoji je nach Plattform in Größe und Farbe
  auseinanderlaufen; `currentColor` lässt die Icons der Textfarbe des Knopfes
  folgen. Flächig gefärbt wären sie in einer langen Liste das auffälligste
  Element, deshalb nur Rahmen und Färbung beim Draufzeigen – dieselbe
  Zurückhaltung wie beim Löschen auf den Karten.
- **Die Einstellungen sind eine Übersichtsseite geworden.** Fahrzeuge,
  Ladeanbieter, Bekannte Ladeorte, Sprache und Benutzerverwaltung sind
  aufklappbare Abschnitte, die **Liste und Anlege-Formular gemeinsam**
  enthalten – vorher klappte nur das Formular, die Tabelle stand immer
  sichtbar darüber. Ein Zähler am Abschnittsnamen zeigt zugeklappt, wie viele
  Einträge drinstehen. "Bearbeiten" öffnet beide Ebenen, sonst scrollt es ins
  Leere.

  Messbar: Seitenhöhe am Rechner von 2136 px auf 604 px, auf dem Handy
  (375 px) von 2774 px auf 783 px.
- **Import, Backup und API-Einrichtung sind eigene Unterseiten**, erreichbar
  über Kacheln in den Einstellungen. "Backup" (`/backup`) fasst Daten-Backup,
  Backup-Import und das automatische WebDAV-Backup zusammen, "API & Debug"
  (`/api-debug`) die MyŠkoda-Konfiguration samt Debug-Protokoll. Die Pfade
  liegen bewusst auf oberster Ebene und **nicht** unter `/settings/...`: alle
  Links und `fetch()`-Aufrufe der App sind relativ, damit sie unter dem
  Home-Assistant-Ingress-Unterpfad genauso auflösen wie am Domain-Root – das
  funktioniert nur, solange jede Seite genau eine Ebene unter der Basis liegt.
- **Der Import ist aus der Hauptleiste verschwunden.** Er wird einmal beim
  Umstieg von Spritmonitor gebraucht und belegte dauerhaft einen von vier
  Plätzen. Jede Unterseite trägt oben einen Rückweg in die Einstellungen.

## [0.12.1] — 2026-09-06

### Fixed
- **Die Handy-Ansicht aus 0.12.0 kam beim Nutzer gar nicht an.** Der Menü-Knopf
  reagierte nicht, die Seiten-Links standen klein aneinandergereiht, die
  Ladevorgangs-Karten hatten keine Abgrenzung. Ursache war nicht das neue CSS,
  sondern dessen Auslieferung: `StaticFiles` setzt ETag und `Last-Modified`,
  aber **keinen `Cache-Control`-Header** – ohne den wenden Browser
  heuristisches Caching an und halten eine Datei ohne jede Rückfrage für
  frisch. Das neue HTML kam also an (`no-store` war dort schon gesetzt), die
  alte `style.css` blieb im Cache, und damit hatten `.navtoggle`, `.navlinks`
  und `.scard` schlicht keine Regeln.

  Zwei Ebenen dagegen: statische Dateien bekommen `Cache-Control: no-cache`
  (vor Benutzung rückfragen – dank ETag in aller Regel ein leeres 304, also
  praktisch dieselbe Ersparnis), und `style.css`/`filter.js` tragen die
  App-Version als `?v=`-Parameter. Ein Versionssprung ändert damit die Adresse
  und schlägt auch durch einen zwischengeschalteten Proxy-Cache durch.

### Changed
- **Das aufgeklappte Menü nutzt die volle Breite der Leiste**: jeder Eintrag
  ist eine eigene Zeile mit 44 px Höhe (übliche Mindestgröße für eine
  Fingerfläche) und eigener Trennlinie, statt eines kleinen Textlinks in einer
  Reihe. Der Menü-Knopf ist 44 × 44 px und färbt sich im geöffneten Zustand
  ein; der Logout-Link ist ein eigener Knopf statt Inline-Text.
- **Die Ladevorgangs-Karten heben sich deutlich ab.** Der Unterschied zwischen
  `--card` (#171e2e) und `--bg` (#0f1420) allein trug nicht – auf einem
  Handydisplay verschwammen beide zu einer Fläche. Jetzt: kräftigerer Rahmen,
  leichter Schatten und eine farbige linke Kante nach Lade-Art (blau AC,
  orange DC), die gleichzeitig trennt und codiert. `needs_review` bleibt
  unterscheidbar über eine schwache Flächenfärbung plus orange Kante.

## [0.12.0] — 2026-09-06

### Changed
- **Die Web-Oberfläche ist auf dem Handy benutzbar.** Bisher gab es keine
  einzige Media Query – alles war für den Desktop gebaut. Neu ab 720 px
  abwärts:
  - **Navigation:** Die Links liegen hinter einem Menü-Knopf und klappen unter
    der Leiste auf. Auf dem Desktop bleibt die Leiste unverändert (der
    Container nutzt `display: contents`, die Flexbox ist dieselbe wie vorher).
  - **Ladevorgänge:** Die zwölfspaltige Tabelle weicht Karten im selben Aufbau
    wie die Zeile in der iOS-App – Datum, Lade-Art und Ort links, kWh und Preis
    rechts, SoC und Kilometerstand darunter, Verbrauch mit Methoden-Symbol. Ein
    Tipp auf die Karte öffnet den Bearbeiten-Modus, Löschen ist ein
    zurückhaltender Knopf am Rand. Es liegt immer nur eine der beiden
    Darstellungen im DOM.
  - **Formulare:** Das Formular für einen neuen Ladevorgang ist auf dem Handy
    eingeklappt – ausgeklappt schob es die Liste um gut 1400 px nach unten. Ein
    Klick auf "Bearbeiten" klappt es selbstverständlich wieder auf.
  - **Dashboard:** Die Kennzahlen stehen zweispaltig statt untereinander.
- **Die Einstellungen sind kompakter – auf dem Handy wie am Desktop.** Die
  Anlege-Formulare für Fahrzeuge, Anbieter, Ladeorte und WebDAV-Backup klappen
  nur bei Bedarf auf (die bestehende Abschnittsüberschrift ist jetzt das
  `<summary>`), die langen Erklärtexte liegen unter "Hinweise". Die Seite ist
  dadurch am Handy von 4322 px auf 2568 px geschrumpft.
- **Das MyŠkoda-Debug-Protokoll steht in einem eigenen aufklappbaren
  Abschnitt** und wird erst beim Aufklappen geladen – zugeklappt spart das
  einen API-Aufruf bei jedem Öffnen der Einstellungen.

### Fixed
- Breite Tabellen (Ladeorte mit Koordinaten, das Debug-Protokoll) haben die
  ganze Seite seitlich aus dem Bild geschoben. Sie scrollen jetzt in einem
  eigenen Container; die Seite selbst bleibt an beiden Rändern stehen.

## [0.11.0] — 2026-09-06

### Changed
- **Der vom Abfrageintervall verschluckte Ladebeginn wird nachgetragen.** Die
  automatische Ladeerkennung über die MyŠkoda Public API bemerkte den
  Ladebeginn erst beim nächsten Abruf; beim DC-Schnellladen fehlte dadurch ein
  erheblicher Teil des Vorgangs. Zwei echte Vorgänge im Debug-Log wurden als
  30 %→77 % und 64 %→80 % erfasst, tatsächlich waren es 8 %→77 % und
  48 %→80 % — rund 17 bzw. 12 kWh, die in Energie, Kosten und
  Verbrauchsstatistik gefehlt haben.

  Als Startwert zählt jetzt der SoC des letzten Abrufs **vor** dem Einstecken.
  Das ist zulässig, weil Fahren den SoC senkt: solange nicht geladen wird, ist
  der SoC monoton fallend, dieser Wert kann also nie unter dem echten
  Startwert liegen. In beiden Fällen oben trifft er ihn exakt — beim zweiten
  sogar, obwohl zwischen den Abrufen noch 5 km gefahren wurden.

  Zwei Wächter verhindern, dass ein anderswo geladener Vorgang mitgezählt
  wird: der Abruf davor darf höchstens `backdate_max_gap_minutes` zurückliegen
  (Standard: das Doppelte des Leerlaufintervalls), und der Zuwachs muss bei der
  beobachteten Ladeleistung physikalisch möglich gewesen sein — letzteres
  setzt eine hinterlegte Akkukapazität am Fahrzeug voraus.

  Die Startzeit wird aus derselben Rechnung mit vorgezogen, sonst würde die
  aus Energie und Dauer abgeleitete Durchschnittsleistung unphysikalisch.

  Abschaltbar in den Einstellungen ("Ladebeginn auf den letzten Abruf davor
  zurückdatieren"). Die Rohwerte beider Abrufe stehen weiterhin in der Notiz
  des Ladevorgangs, die angewandte Korrektur ebenfalls — dort und als
  `session_backdated`-Zeile im Debug-Protokoll.

## [0.10.3] — 2026-09-01

### Changed
- **Automatisches API-Polling ist jetzt ein Schalter (AN/AUS) neben der
  Abschnittsüberschrift**, statt einer Checkbox zwischen zehn weiteren
  Formularfeldern. Dort wurde sie leicht übersehen – mit der Folge, dass
  ausschließlich die von Hand ausgelösten Abfragen liefen und die
  automatische Ladeerkennung stillschweigend nichts tat. Der Schalter
  speichert sofort, statt auf "Speichern" zu warten.

### Fixed
- Die Datumsspalte der Ladevorgangs-Tabelle bricht nicht mehr auf mehrere
  Zeilen um. In der englischen Oberfläche ist die Überschrift ("Date") kürzer
  als der Inhalt ("Sep 1, 2026, 5:56 PM"), und an der Überschrift hat sich die
  automatische Spaltenbreite orientiert.

## [0.10.2] — 2026-08-31

### Fixed
- MyŠkoda-Einstellungen: "Verbindung testen" und "Jetzt abfragen" lasen nur
  die zuletzt gespeicherte Konfiguration aus der Datenbank, nicht die gerade
  im Formular eingetragenen Werte. Da beide Buttons in der Reihenfolge vor
  "Speichern" stehen, führte das beim erstmaligen Einrichten leicht zu
  "Bitte zuerst API-Key und FIN eintragen und speichern", obwohl beide Felder
  ausgefüllt waren. Beide Buttons speichern die aktuellen Werte jetzt
  automatisch mit.

## [0.10.1] — 2026-08-31

### Fixed
- **Backup-Import in ein zweites Konto derselben Instanz übersprang restlos
  alles** (`0 importiert, 46 übersprungen`). Die Prüfung auf bereits
  vorhandene Datensätze lief über alle Nutzer hinweg statt nur über die
  eigenen – die UUIDs aus der ZIP existierten bereits, nur eben beim
  exportierenden Konto. Der importierende Nutzer bekommt jetzt eigene Kopien
  mit neu vergebenen IDs; die Fremdschlüssel werden dabei mitgezogen.
- Der Restore auf einen frischen Server behält die Original-IDs unverändert
  bei, und ein mehrfach ausgeführter Import legt weiterhin nichts doppelt an.
- Bereits vorhandene Datensätze werden zusätzlich an ihren Fachdaten erkannt
  (Fahrzeug an der External ID, Anbieter am Namen, Ladeort an Name +
  Koordinaten, Ladevorgang an Fahrzeug + Startzeit). Dadurch führt eine ZIP
  von einem *anderen* Server gleiche Einträge zusammen, statt sie zu
  duplizieren – und bricht nicht mehr mit HTTP 500 ab, wenn dort ein Fahrzeug
  mit derselben External ID existiert.

## [0.10.0] — 2026-08-31

### Added
- Automatische Ladeerkennung über die **MyŠkoda Public API**: neue Sektion in
  den Einstellungen, in der pro Fahrzeug ein API-Key (aus der MyŠkoda-App,
  [go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)) und die FIN hinterlegt
  werden. Der Server fragt das Fahrzeug dann selbst ab und legt erkannte
  Ladevorgänge an - ohne Home Assistant.
- Läuft **parallel** zum bisherigen Home-Assistant-Push, ersetzt ihn nicht.
  Pro Fahrzeug sollte nur einer der beiden Wege aktiv sein, sonst entstehen
  doppelte Ladevorgänge (die Quellen erkennen sich gegenseitig nicht).
- Adaptives Abfrageintervall (Standard 20 min im Leerlauf, 5 min während eines
  Ladevorgangs) inklusive Auswertung der `RateLimit-*`-Header - bleibt mit
  Reserve unter dem Limit der API von 20 Anfragen pro Stunde und API-Key.
  Restkontingent und Ablaufdatum des Keys werden in der Web-UI angezeigt.
- Nacherkennung verpasster Ladevorgänge über einen SoC-Sprung, falls das
  Fahrzeug zwischen zwei Abfragen geschlafen hat (Schwelle konfigurierbar).
- Diagnose-Notiz an jedem automatisch erkannten Vorgang mit den Messwerten
  vor und nach dem Einstecken, maximaler Ladeleistung und Anzahl Abfragen.
- Debug-Protokoll pro Fahrzeug mit "Verbindung testen"- und "Jetzt
  abfragen"-Buttons, gespeicherten Rohantworten der API und JSON-Download.

### Changed
- Die Backup-ZIP enthält weiterhin **keine** Zugangsdaten; der neue
  MyŠkoda-API-Key und das WebDAV-Passwort sind dort ausdrücklich
  ausgenommen (steht jetzt auch in der README innerhalb der ZIP).

## [0.9.1] — 2026-08-25

### Fixed
- Web-UI funktioniert jetzt auch über den Home-Assistant-Ingress-
  Sidebar-Button: Templates/Redirects nutzen relative statt absolute
  Pfade – vorher führte ein Login-Redirect außerhalb des Ingress-Bereichs
  auf eine falsche URL (404).
- HTML-Seiten senden `Cache-Control: no-store`, damit Browser sie nicht
  mehr zwischenspeichern – verhindert, dass nach einem Update/Rebuild
  eine veraltete Seite angezeigt wird, bis der Browser-Cache manuell
  geleert wird.

### Docs
- README (Add-on- und Server-Repo): neuer Abschnitt zu Ingress vs.
  Direktport, warum beides nur lokal funktioniert, und wie externer
  Zugriff (VPN oder eigener Reverse-Proxy-Vhost) eingerichtet wird.

## [0.9.0] — 2026-08-25

### Added
- Automatisches WebDAV-Backup: neue Sektion in den Einstellungen, lädt die
  Backup-ZIP in konfigurierbarer Häufigkeit (täglich/wöchentlich/monatlich)
  automatisch auf einen WebDAV-Server hoch (z.B. Nextcloud).
- Aufbewahrungsfrist in Tagen konfigurierbar - selbst hochgeladene, ältere
  Backups werden automatisch wieder entfernt.
- "Jetzt sichern"-Button für einen sofortigen Lauf inkl. Erfolg/Fehler-Anzeige
  - dient gleichzeitig als Verbindungstest.
- Läuft im Hintergrund (alle 15 Minuten geprüft), kein Cron oder externer
  Scheduler nötig.

## [0.8.2] — 2026-08-24

### Added
- Ladevorgänge manuell erfassen oder automatisch per Home-Assistant-Automation
  pushen lassen.
- Verbrauchsberechnung (kWh/100km) über eine priorisierte Fallback-Kette,
  inkl. Vollladungs-Intervallen als Goldstandard.
- Spritmonitor-CSV-Import, vollständiger Backup-Export/-Import als ZIP,
  Mehrbenutzerfähigkeit mit isolierten Datensätzen.
- Deployment als Unraid-Community-Applications-Container, Home-Assistant-
  Add-on, einzelner Docker-Container oder Docker-Compose-Stack.
- Login-Cookie funktioniert jetzt auch bei direktem HTTP-Zugriff (z.B.
  Unraid-CA-Standardfall `http://<ip>:8111`), nicht mehr nur hinter
  HTTPS-Reverse-Proxy.

### Fixed
- Versionsanzeige im Header korrigiert (zeigte in v0.8.1 fälschlich v1.0.0)
  und CI-Check ergänzt, der einen Tag-Release ablehnt, falls Git-Tag und
  In-App-Version auseinanderlaufen.
