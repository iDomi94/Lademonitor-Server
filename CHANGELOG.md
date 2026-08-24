# Changelog

Alle nennenswerten Änderungen an Lademonitor stehen hier. Dieselbe Historie ist
auch in der App sichtbar – auf den Versions-Badge im Header klicken.

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/), Versionen
folgen [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-08-24

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
