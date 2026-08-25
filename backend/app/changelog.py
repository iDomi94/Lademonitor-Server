"""In-App-Versionsverlauf, neueste zuerst. Beim Release: hier einen Eintrag
ergaenzen (und denselben Text in CHANGELOG.md pflegen), dann mit
`git tag vX.Y.Z` released - der Header-Badge und das "Was ist neu"-Fenster
lesen direkt aus dieser Liste, kein separater Build-Schritt noetig."""

CHANGELOG = [
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
