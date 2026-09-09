"""In-App-Versionsverlauf, neueste zuerst. Beim Release: hier einen Eintrag
ergaenzen (und denselben Text in CHANGELOG.md pflegen), dann mit
`git tag vX.Y.Z` released - der Header-Badge und das "Was ist neu"-Fenster
lesen direkt aus dieser Liste, kein separater Build-Schritt noetig."""

CHANGELOG = [
    {
        "version": "0.15.0",
        "date": "2026-09-09",
        "title": "GPS-Koordinaten und Notizen können jetzt verschlüsselt werden",
        "changes": [
            "Neu, optional: GPS-Koordinaten (Ladeorte und Ladevorgänge), Notizen und automatisch ermittelte Ortsnamen können jetzt verschlüsselt statt im Klartext in der Datenbank gespeichert werden - relevant, sobald der Server öffentlich erreichbar ist (z.B. über einen eigenen Reverse Proxy). Standardmäßig AUS, wer nur im eigenen Heimnetz unterwegs ist, braucht nichts zu tun.",
            "Aktivieren: Umgebungsvariable `FIELD_ENCRYPTION_KEY` setzen (`python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`, beim Unraid-Template als neues optionales Feld, bei Docker Compose in der `.env` - siehe `.env.example`). Sicher aufbewahren (z.B. Passwort-Manager) und NICHT im Datenverzeichnis (`/config`) ablegen, sonst landet er im selben Backup wie die damit verschlüsselten Daten. Einmal gesetzt und benutzt nicht mehr entfernen - ein verlorener oder entfernter Schlüssel macht die betroffenen Felder dauerhaft unlesbar, der Server verweigert dann bewusst den Start statt kaputte Werte auszuliefern.",
            "Bestehende Installationen: wird der Schlüssel gesetzt, werden vorhandene Klartextwerte beim nächsten Start automatisch verschlüsselt - auch wenn das erst Wochen später passiert. Der aktuelle Stand (an/aus) steht als Badge unten in den Einstellungen.",
            "Kein Zero-Knowledge-Schutz: der Schlüssel liegt im Server-Environment, der Server entschlüsselt weiterhin transparent bei jedem Request. Das schützt gegen Diebstahl von Datenbank/Backup/Datenträger, nicht aber davor, dass jemand mit Kontrolle über den laufenden Server-Prozess selbst die Daten einsehen könnte - echtes Zero-Knowledge würde eine clientseitige Verschlüsselung brauchen und wäre mit Statistik, Geo-Matching, Offline-Reverse-Geocoding und dem Home-Assistant-/MyŠkoda-Push nicht vereinbar.",
            "Noch nicht verschlüsselt: Namen (Fahrzeug/Anbieter/Ladeort), die E-Mail-Adresse, sowie die schon vorher bekannten Klartext-Zugangsdaten (SMTP-/WebDAV-Passwort, MyŠkoda-API-Key). Der eingebaute CSV-Export und das automatische WebDAV-Backup liefern GPS-Koordinaten/Notizen weiterhin bewusst im Klartext, da sie ein portables, menschenlesbares Format bleiben sollen.",
        ],
    },
    {
        "version": "0.14.1",
        "date": "2026-09-08",
        "title": "Passwortwechsel gibt den neuen Zugang direkt zurueck",
        "changes": [
            "Beim Aendern des eigenen Passworts meldet der Server weiterhin alle Geraete ab und stellt sofort eine neue Sitzung aus - die gab es bisher aber nur als Cookie, also nur fuer die Web-Oberflaeche. Die iOS-App und andere Clients, die sich mit einem Token anmelden, standen danach ohne gueltigen Zugang da und mussten sich mit dem neuen Passwort ein zweites Mal anmelden, obwohl der Server die Sitzung schon erzeugt hatte.",
            "Der Endpunkt gibt den neuen Token jetzt zurueck, genau wie Anmeldung und Registrierung. Fuer die Web-Oberflaeche aendert sich nichts.",
        ],
    },
    {
        "version": "0.14.0",
        "date": "2026-09-08",
        "title": "E-Mail: Passwort vergessen, Einladungen und Benachrichtigungen",
        "changes": [
            "Neu: ein SMTP-Zugang, den ein Admin unter Einstellungen -> E-Mail einrichtet. Er gilt fuer die ganze Installation und nicht pro Nutzer - die \"Passwort vergessen\"-Mail muss ja gerade dann verschickt werden koennen, wenn niemand angemeldet ist. Mit Testmail-Knopf und Versandprotokoll, damit ein fehlgeschlagener Versand nicht unsichtbar bleibt.",
            "Jeder Nutzer kann unter \"Mein Konto\" eine E-Mail-Adresse hinterlegen; Admins koennen fremde Adressen nachtragen oder korrigieren. Eine neue Adresse bekommt einen Bestaetigungslink - erst eine bestaetigte Adresse darf ein Passwort zuruecksetzen, sonst wuerde ein Tippfehler einem Fremden Zugriff verschaffen.",
            "\"Passwort vergessen\" auf der Anmeldeseite: Link per Mail, eine Stunde gueltig, nur einmal verwendbar. Die Anfrage antwortet immer gleich, egal ob es das Konto gibt - sonst waere das Formular ein Verzeichnis aller Nutzernamen. Hoechstens drei Anfragen pro Konto und Stunde.",
            "Wichtig: Beim Zuruecksetzen werden alle angemeldeten Geraete abgemeldet - waere das Konto uebernommen worden, liefe die fremde Sitzung sonst weiter. Home Assistant und die iOS-App muessen sich danach neu anmelden. Dasselbe gilt beim Aendern des eigenen Passworts, das es bisher ueberhaupt nicht gab.",
            "Anmelden geht jetzt mit Nutzername ODER E-Mail-Adresse, Gross- und Kleinschreibung egal.",
            "Admins koennen Konten anlegen und per Mail einladen: der Nutzer setzt sein Passwort selbst ueber einen Link. So kennt es niemand sonst - ein Admin kann es weder vergeben noch sehen.",
            "Neue Benachrichtigungen, jede einzeln abschaltbar: fehlgeschlagenes WebDAV-Backup (das faellt sonst nur auf, wenn man zufaellig die Einstellungen oeffnet), abgelaufene MyŠkoda-Anmeldung samt Vorwarnung 14/7/1 Tage vor Ablauf des API-Keys, Sammelmeldung ueber zu pruefende Ladevorgaenge (aus/taeglich/woechentlich), Monatsbericht mit Kosten und Verbrauch, und eine Meldung an Admins, wenn sich jemand neu registriert.",
            "Sicherheit: Anmelde-Tokens liegen nicht mehr im Klartext in der Datenbank, sondern nur noch als SHA-256-Hash - ein Auth-Token ist eine fertige Anmeldung, im Klartext war die Tabelle ein Generalschluessel fuer jedes Konto. Bestehende Anmeldungen bleiben beim Update erhalten, niemand wird ausgeloggt. Passwoerter waren und bleiben mit bcrypt gehasht.",
            "Das SMTP-Passwort muss im Klartext gespeichert werden, weil SMTP es beim Anmelden uebertraegt - aus einem Hash liesse es sich nicht zurueckgewinnen. Es wird deshalb nie ueber die API zurueckgegeben und nicht in die Backup-ZIP exportiert; in den Einstellungen steht der Hinweis, ein App-spezifisches Passwort zu verwenden.",
        ],
    },
    {
        "version": "0.13.0",
        "date": "2026-09-08",
        "title": "Ladevorgangs-Tabelle passt aufs Bild, Einstellungen aufgeraeumt",
        "changes": [
            "Die Ladevorgangs-Liste musste am Rechner seitlich geschoben werden, egal wie breit das Fenster war - der Seiteninhalt war fest auf 1100 px begrenzt, die zwoelfspaltige Tabelle brauchte rund 1180 px. Die Liste darf jetzt bis 1500 px breit werden (die uebrigen Seiten bleiben bei 1100 px, dort liest sich schmaler besser), und die Aktionsspalte ist deutlich schlanker: statt der beschrifteten Knoepfe \"Bearbeiten\"/\"Löschen\" stehen dort zwei Icon-Knoepfe (Stift und Papierkorb) mit Beschriftung als Tooltip. Zusammen passt die Tabelle ab 1100 px Fensterbreite vollstaendig ins Bild.",
            "Dieselben Icon-Knoepfe gibt es jetzt auch in den Tabellen der Einstellungen (Fahrzeuge, Anbieter, Ladeorte, Benutzerverwaltung) und auf den Ladevorgangs-Karten der Handy-Ansicht.",
            "Die Einstellungen sind eine Uebersichtsseite geworden: Fahrzeuge, Ladeanbieter, Bekannte Ladeorte, Sprache und Benutzerverwaltung sind aufklappbare Abschnitte, die jeweils LISTE UND ANLEGE-FORMULAR enthalten - vorher waren die drei Tabellen immer sichtbar. Der Zaehler am Abschnittsnamen zeigt zugeklappt, wie viele Eintraege drinstehen. Die Seite ist damit am Rechner von 2136 px auf 604 px und auf dem Handy von 2774 px auf 783 px Hoehe geschrumpft.",
            "Import, Backup und die API-Einrichtung sind eigene Unterseiten und ueber Kacheln in den Einstellungen erreichbar. \"Backup\" fasst Daten-Backup, Backup-Import und das automatische WebDAV-Backup zusammen, \"API & Debug\" die MyŠkoda-Konfiguration samt Debug-Protokoll.",
            "Der Import ist dafuer aus der Hauptleiste verschwunden - er wird einmal beim Umstieg von Spritmonitor gebraucht und belegte dauerhaft einen von vier Plaetzen. Jede Unterseite hat oben einen Rueckweg in die Einstellungen.",
        ],
    },
    {
        "version": "0.12.1",
        "date": "2026-09-06",
        "title": "Handy-Ansicht kam nicht an (alte CSS aus dem Browser-Cache)",
        "changes": [
            "Nach dem Update auf 0.12.0 sah die Oberflaeche auf dem Handy weiterhin kaputt aus: der Menue-Knopf reagierte nicht, die Seiten-Links standen klein aneinandergereiht und die Ladevorgangs-Karten hatten keine Abgrenzung. Ursache war der Browser-Cache - das neue HTML kam an, die zugehoerige style.css nicht, sodass alle neuen Elemente voellig ungestylt blieben. Statische Dateien hatten keinen Cache-Control-Header, woraufhin Browser sie einfach fuer beliebig lange frisch halten.",
            "Stylesheet und Skript tragen jetzt die App-Version in der Adresse, und statische Dateien werden vor Benutzung kurz beim Server rueckgefragt. Ein Update schlaegt damit sofort durch, ohne den Browser-Cache von Hand zu leeren.",
            "Das aufgeklappte Menue nutzt jetzt die volle Breite: jeder Eintrag ist eine eigene Zeile mit 44 px Hoehe statt eines kleinen Textlinks. Auch der Menue-Knopf selbst und der Logout-Link sind auf Fingergroesse gebracht.",
            "Ladevorgangs-Karten heben sich deutlicher ab: kraeftigerer Rahmen, leichter Schatten und eine farbige linke Kante nach Lade-Art (blau AC, orange DC). Vorher verschwammen Kartenflaeche und Seitenhintergrund auf einem Handydisplay zu einer Flaeche.",
        ],
    },
    {
        "version": "0.12.0",
        "date": "2026-09-06",
        "title": "Web-Oberflaeche auf dem Handy benutzbar",
        "changes": [
            "Die Navigationsleiste war auf dem Handy zu breit - die Links liegen jetzt hinter einem Menue-Knopf und klappen darunter auf. Auf dem Desktop bleibt die Leiste unveraendert.",
            "Die Ladevorgangs-Liste war mit zwoelf Spalten auf schmalen Schirmen nicht lesbar. Sie erscheint dort jetzt als Karten im selben Aufbau wie in der iOS-App: Datum, Lade-Art und Ort links, kWh und Preis rechts, SoC und Kilometerstand darunter. Ein Tipp auf die Karte oeffnet den Bearbeiten-Modus. Auf breiten Schirmen bleibt es die gewohnte Tabelle.",
            "Das Formular fuer einen neuen Ladevorgang ist auf dem Handy eingeklappt - vorher schob es die Liste um gut zwei Bildschirmhoehen nach unten.",
            "Die Einstellungen sind deutlich kuerzer geworden, auf dem Handy wie am Rechner: die Anlege-Formulare fuer Fahrzeuge, Anbieter, Ladeorte und das WebDAV-Backup klappen nur bei Bedarf auf, die langen Erklaertexte liegen unter \"Hinweise\".",
            "Das MyŠkoda-Debug-Protokoll hat die Seite dominiert und steht jetzt in einem eigenen aufklappbaren Abschnitt - die 200 Zeilen werden erst beim Aufklappen geladen.",
            "Breite Tabellen schieben die Seite nicht mehr seitlich aus dem Bild, sondern scrollen fuer sich.",
            "Die Kennzahlen im Dashboard stehen auf dem Handy zweispaltig statt untereinander.",
        ],
    },
    {
        "version": "0.11.0",
        "date": "2026-09-06",
        "title": "DC-Ladevorgaenge: verschluckter Ladebeginn wird nachgetragen",
        "changes": [
            "Die automatische Ladeerkennung ueber die MyŠkoda-API merkte den Ladebeginn erst beim naechsten Abruf - beim DC-Schnellladen fehlte dadurch ein erheblicher Teil des Vorgangs. Zwei echte Vorgaenge im Debug-Log wurden als 30 %-77 % und 64 %-80 % erfasst, tatsaechlich waren es 8 %-77 % und 48 %-80 %: rund 17 bzw. 12 kWh, die in Energie, Kosten und Verbrauchsstatistik gefehlt haben.",
            "Als Startwert zaehlt jetzt der SoC des letzten Abrufs VOR dem Einstecken. Das ist zulaessig, weil Fahren den SoC senkt - dieser Wert kann nie unter dem echten Startwert liegen. In beiden Faellen oben trifft er den echten Wert exakt.",
            "Zwei Waechter verhindern, dass dabei ein woanders geladener Vorgang mitgezaehlt wird: der Abruf davor darf nicht zu alt sein (einstellbar, standardmaessig das Doppelte des Leerlaufintervalls), und der Zuwachs muss bei der beobachteten Ladeleistung ueberhaupt moeglich gewesen sein (dafuer muss die Akkukapazitaet am Fahrzeug hinterlegt sein).",
            "Die Startzeit wird passend mit vorgezogen, damit die abgeleitete Durchschnittsleistung plausibel bleibt.",
            "Abschaltbar in den Einstellungen. Die Rohwerte beider Abrufe stehen weiterhin in der Notiz des Ladevorgangs, die Korrektur ist dort und im Debug-Protokoll nachvollziehbar.",
        ],
    },
    {
        "version": "0.10.3",
        "date": "2026-09-01",
        "title": "MyŠkoda-Polling als Schalter, Datumsspalte bricht nicht mehr um",
        "changes": [
            "Das automatische Abfragen der MyŠkoda-API laesst sich jetzt ueber einen Schalter mit AN/AUS neben der Abschnittsueberschrift ein- und ausschalten - bisher war es eine Checkbox zwischen zehn weiteren Formularfeldern und wurde leicht uebersehen. Wer sie uebersah, bekam ausschliesslich die von Hand ausgeloesten Abfragen und keine automatische Ladeerkennung.",
            "Der Schalter speichert sofort, statt auf \"Speichern\" zu warten.",
            "Datumsspalte der Ladevorgangs-Tabelle bricht nicht mehr auf mehrere Zeilen um. In der englischen Oberflaeche ist die Ueberschrift (\"Date\") kuerzer als der Inhalt (\"Sep 1, 2026, 5:56 PM\"), woran sich die automatische Spaltenbreite orientiert hat.",
        ],
    },
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
