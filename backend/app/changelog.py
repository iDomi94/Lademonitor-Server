"""In-App-Versionsverlauf, neueste zuerst. Beim Release: hier einen Eintrag
ergaenzen (und denselben Text in CHANGELOG.md pflegen), dann mit
`git tag vX.Y.Z` released - der Header-Badge und das "Was ist neu"-Fenster
lesen direkt aus dieser Liste, kein separater Build-Schritt noetig."""

CHANGELOG = [
    {
        "version": "0.9.1",
        "date": "2026-08-25",
        "title": "Ingress-Kompatibilitaet und Cache-Fix",
        "changes": [
            "Web-UI funktioniert jetzt auch ueber den Home-Assistant-Ingress-Sidebar-Button (relative statt absolute Pfade in Templates/Redirects) - vorher fuehrte ein Login-Redirect dort auf eine falsche URL (404).",
            "HTML-Seiten werden nicht mehr vom Browser gecacht (Cache-Control: no-store) - verhindert, dass nach einem Update/Rebuild eine veraltete Seite angezeigt wird, bis der Browser-Cache manuell geleert wird.",
        ],
    },
    {
        "version": "0.9.0",
        "date": "2026-08-25",
        "title": "Automatisches WebDAV-Backup",
        "changes": [
            "Neue Sektion in den Einstellungen: laedt die Backup-ZIP in konfigurierbarer Haeufigkeit (taeglich/woechentlich/monatlich) automatisch auf einen WebDAV-Server hoch (z.B. Nextcloud).",
            "Aufbewahrungsfrist in Tagen konfigurierbar - selbst hochgeladene, aeltere Backups werden automatisch wieder entfernt.",
            "\"Jetzt sichern\"-Button loest einen sofortigen Lauf aus und zeigt Erfolg/Fehler direkt an - dient gleichzeitig als Verbindungstest.",
            "Laeuft im Hintergrund (alle 15 Minuten geprueft), kein Cron oder externer Scheduler noetig.",
        ],
    },
    {
        "version": "0.8.2",
        "date": "2026-08-24",
        "title": "Erste Version",
        "changes": [
            "Ladevorgaenge manuell erfassen oder automatisch per Home-Assistant-Automation pushen lassen.",
            "Verbrauchsberechnung (kWh/100km) ueber eine priorisierte Fallback-Kette, inkl. Vollladungs-Intervallen als Goldstandard.",
            "Spritmonitor-CSV-Import, vollstaendiger Backup-Export/-Import als ZIP, Mehrbenutzerfaehigkeit mit isolierten Datensaetzen.",
            "Deployment als Unraid-Community-Applications-Container, Home-Assistant-Add-on, einzelner Docker-Container oder Docker-Compose-Stack.",
            "Login-Cookie funktioniert jetzt auch bei direktem HTTP-Zugriff (z.B. Unraid-CA-Standardfall http://<ip>:8111), nicht mehr nur hinter HTTPS-Reverse-Proxy.",
            "Versionsanzeige im Header korrigiert (zeigte in v0.8.1 faelschlich v1.0.0) und CI-Check ergaenzt, der einen Tag-Release ablehnt, falls Git-Tag und In-App-Version auseinanderlaufen.",
        ],
    },
]

VERSION = CHANGELOG[0]["version"]
