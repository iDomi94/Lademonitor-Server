# Changelog

Alle nennenswerten Änderungen an Lademonitor stehen hier. Dieselbe Historie ist
auch in der App sichtbar – auf den Versions-Badge im Header klicken.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/), Versionen
folgen [Semantic Versioning](https://semver.org/).

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
