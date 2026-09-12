# Lademonitor – Logo-Entwürfe

15 Varianten für ein neues Lademonitor-Logo. Vorgabe: **gestauchte,
comic-hafte Fahrzeugsilhouette in Anlehnung an den Škoda Enyaq, Ladesäule,
gestricheltes Kabel** – dunkler Hintergrund, graue Silhouette und Säule,
Kabel in Electric Green.

```
design/logo/
  parts.py      gemeinsame Bausteine (Fahrzeug, Säule, Wallbox, Kabel, Farben)
  build.py      die 15 Varianten + Übersichtsseite
  out/          erzeugte SVGs + preview.html   (generiert, nicht von Hand ändern)
```

Neu erzeugen:

```bash
python3 design/logo/build.py
```

Danach `design/logo/out/preview.html` im Browser öffnen – dort liegen alle
Varianten nebeneinander, jeweils mit Begründung und mit Abbildungen bei 48,
32 und 24 px.

## Warum ein Generator statt 15 einzelner SVG-Dateien

Alle Varianten ziehen Fahrzeug, Säule und Kabel aus `parts.py`. Eine
Korrektur an der Silhouette schlägt dadurch in allen Entwürfen gleichzeitig
durch, statt fünfzehnmal nachgezogen werden zu müssen (und dabei
auseinanderzulaufen). Genau das ist während der Entwurfsarbeit mehrfach
passiert – die Silhouette ist in vier Durchgängen überarbeitet worden.

## Farben

Die Grundtöne liegen bewusst nah an den CSS-Variablen der Web-UI
(`backend/app/static/style.css`), damit das Logo nicht neben der eigenen App
steht:

| Rolle                | Wert      | Bezug                        |
|----------------------|-----------|------------------------------|
| Hintergrund dunkel   | `#0F1420` | `--bg` der Web-UI            |
| Hintergrund hell     | `#1A2335` | Verlaufsende                 |
| Silhouette (Haupt)   | `#8C96AC` | nah an `--muted` `#8b95ab`   |
| Ladesäule            | `#6E7890` | tritt hinter das Auto zurück |
| Scheiben / Reifen    | `#2B3446` | Binnenzeichnung              |
| Kabel (Electric Green)| `#3BF07C`| neu, greller als `--accent`  |

## Die Silhouette

Länge 178, Höhe 73 Einheiten → **2,44 : 1**. Der echte Enyaq liegt bei
2,88 : 1; die fehlenden gut 15 % sind die gewünschte Stauchung. Dazu Räder
mit r = 20 statt maßstäblichen ~15 und dicke, runde Radläufe – zusammen
ergibt das den Comic-Griff.

Enyaq-Merkmale, die in der Seitenansicht überhaupt ablesbar sind und
deshalb betont wurden: sehr kurze Haube und hoher, stumpfer Bug (MEB hat
keinen Motorraum), flaches Dach mit abfallendem Heck, hoch angesetzte
Gürtellinie mit viel Blech und wenig Glas, kantige Radhaus-Verbreiterungen.
Der Crystal-Face-Grill ist von der Seite nicht darstellbar – dafür gibt es
Variante 07 als Frontansicht.

**Blickrichtung rechts, Säule links** ist kein Zufall: die MEB-Plattform
(Enyaq, ID.4) hat die Ladeklappe hinten rechts. Nur wenn das Fahrzeug nach
rechts zeigt, schaut diese Seite zum Betrachter und die Ladeklappe ist
überhaupt sichtbar. Steht die Säule dann links, hat das Kabel einen kurzen,
lesbaren Weg zum Heck statt quer über das ganze Fahrzeug.

## Befund zur Größentauglichkeit

Der Test bei 48/32/24 px (in `preview.html` unter jeder Karte) ist eindeutig:
**die volle Szene aus Fahrzeug, Säule und Kabel trägt nicht bis in
Favicon-Größe.** Unter etwa 48 px zerläuft sie zu einem Fleck. Praktisch
heißt das: zwei Stufen statt einer Datei.

* **Groß** (App-Icon, Kopfleiste, README, 192/512 px) – jede der Varianten.
* **Klein** (Favicon 16–32 px, Tab-Leiste) – Variante **09** (auf drei
  Zeichen reduziert und deutlich größer im Rahmen, bleibt bis ~24 px als
  Fahrzeug lesbar) oder **04** bzw. **06**, deren Ring bzw. Buchstabe auch
  bei 32 px noch trägt.

## Offene Punkte vor dem Einsatz

* **Wortmarke in Pfade umwandeln.** Der Schriftzug in Variante 10 liegt noch
  als `<text>` vor und wird damit von der Schrift des jeweiligen Systems
  gerendert – auf einem Gerät ohne Inter sieht die Marke anders aus.
* **PNG-Ableitungen erzeugen.** Die App bindet `favicon.ico`,
  `favicon-16/32.png`, `apple-touch-icon.png` und `icon-192/512.png` ein
  (`backend/app/templates/base.html`). Die liegen bisher unverändert im
  Repo; ausgetauscht wird erst, wenn eine Variante ausgewählt ist.
* **Version mitziehen.** Beim Austausch der Dateien unter `static/` muss die
  Version in `backend/app/changelog.py` hochgezählt werden – sonst bleibt die
  Adresse gleich und ein Proxy-Cache dazwischen liefert weiter die alten
  Icons aus (siehe CLAUDE.md, Abschnitt „Statische Dateien MÜSSEN einen
  Cache-Control-Header haben").
