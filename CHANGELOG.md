# Changelog

Alle nennenswerten Änderungen an Lademonitor stehen hier. Dieselbe Historie ist
auch in der App sichtbar – auf den Versions-Badge im Header klicken.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/), Versionen
folgen [Semantic Versioning](https://semver.org/).

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
