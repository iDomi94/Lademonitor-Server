"""In-App-Versionsverlauf, neueste zuerst. Beim Release: hier einen Eintrag
ergaenzen (und denselben Text in CHANGELOG.md pflegen), dann mit
`git tag vX.Y.Z` released - der Header-Badge und das "Was ist neu"-Fenster
lesen direkt aus dieser Liste, kein separater Build-Schritt noetig."""

CHANGELOG = [
    {
        "version": "0.10.2",
        "date": "2026-08-31",
        "title": "MyŠkoda: \"Verbindung testen\"/\"Jetzt abfragen\" vor dem Speichern repariert",
        "changes": [
            "\"Verbindung testen\" und \"Jetzt abfragen\" lasen bisher nur die zuletzt gespeicherte Konfiguration - wer API-Key und FIN eintrug und direkt auf einen der beiden Buttons klickte (sie stehen in der Reihenfolge vor \"Speichern\"), bekam \"Bitte zuerst API-Key und FIN eintragen und speichern\", obwohl beide Felder ausgefuellt waren.",
            "Beide Buttons speichern die aktuell eingetragenen Werte jetzt automatisch mit, bevor sie den Abruf ausloesen.",
        ],
    },
    {
        "version": "0.10.1",
        "date": "2026-08-31",
        "title": "Backup-Import in ein zweites Konto repariert",
        "changes": [
            "Backup-Import in ein anderes Nutzerkonto derselben Instanz uebersprang bisher restlos alles (\"0 importiert, 46 uebersprungen\"): die Pruefung auf bereits vorhandene Datensaetze lief ueber alle Nutzer hinweg statt nur ueber die eigenen. Der importierende Nutzer bekommt jetzt eigene Kopien.",
            "Beim Restore auf einen frischen Server bleiben die Original-IDs weiterhin erhalten, und ein mehrfach ausgefuehrter Import legt nach wie vor nichts doppelt an.",
            "Bereits vorhandene Datensaetze werden zusaetzlich an ihren Fachdaten erkannt (Fahrzeug an der External ID, Anbieter am Namen, Ladeort an Name und Koordinaten, Ladevorgang an Fahrzeug und Startzeit) - dadurch legt auch eine Backup-ZIP von einem anderen Server nichts doppelt an, statt wie bisher mit einem Serverfehler abzubrechen.",
        ],
    },
    {
        "version": "0.10.0",
        "date": "2026-08-31",
        "title": "Automatische Ladeerkennung über die MyŠkoda Public API",
        "changes": [
            "Neue Sektion in den Einstellungen: der Server kann Ladevorgaenge jetzt selbst erkennen, indem er die offizielle MyŠkoda Public API direkt abfragt - ohne Home Assistant. Noetig sind nur ein API-Key aus der MyŠkoda-App und die FIN.",
            "Laeuft parallel zum bisherigen Home-Assistant-Push, ersetzt ihn nicht - pro Fahrzeug sollte aber nur ein Weg aktiv sein, sonst entstehen doppelte Ladevorgaenge.",
            "Adaptives Abfrageintervall (Standard 20 Minuten im Leerlauf, 5 Minuten waehrend eines Ladevorgangs) bleibt mit Reserve unter dem Rate-Limit der API von 20 Anfragen pro Stunde und API-Key; Restkontingent und Ablaufdatum des Keys werden angezeigt.",
            "Nacherkennung verpasster Ladevorgaenge ueber einen SoC-Sprung, falls das Fahrzeug zwischen zwei Abfragen geschlafen hat.",
            "Jeder automatisch erkannte Vorgang bekommt eine Notiz mit den Messwerten davor und danach, damit sich die Genauigkeit beim Nachbearbeiten beurteilen laesst.",
            "Debug-Protokoll pro Fahrzeug mit \"Verbindung testen\"- und \"Jetzt abfragen\"-Buttons, Rohantworten der API und JSON-Download - gedacht, um das Verhalten der noch jungen API an einem echten Ladevorgang nachzuvollziehen.",
        ],
    },
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
