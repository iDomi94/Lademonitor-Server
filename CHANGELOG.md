# Changelog

Alle nennenswerten Änderungen an Lademonitor stehen hier. Dieselbe Historie ist
auch in der App sichtbar – auf den Versions-Badge im Header klicken.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/), Versionen
folgen [Semantic Versioning](https://semver.org/).

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
