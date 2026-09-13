# Lademonitor – Logo-Entwürfe

17 Varianten für ein neues Lademonitor-Logo. Vorgabe: **gestauchte,
comic-hafte Fahrzeugsilhouette in Anlehnung an den Škoda Enyaq, Ladesäule,
gestricheltes Kabel** – dunkler Hintergrund, graue Silhouette und Säule,
Kabel in Electric Green.

## Ausgewählt: 17 „Angeschnitten"

Diese Variante ist seit v0.18.0 das Logo der App. Der Icon-Satz wird daraus
erzeugt:

```bash
python3 design/logo/icons.py
```

schreibt `favicon.ico`, `favicon-16/32.png`, `apple-touch-icon.png` und
`icon-192/512.png` nach `backend/app/static/` sowie das `icon.png` im
Repo-Wurzelverzeichnis, auf das die Unraid-CA-Vorlagen verweisen.

Drei Fassungen derselben Variante, alle aus `build._v17()`:

| Fassung | Größen | Warum |
|---|---|---|
| voll | 512, 256, 192 | die komplette Zeichnung |
| klein | 48, 32, 16 | ohne Türfugen, Griffe, Leuchten, Displaydetails, dafür kräftigeres Kabel – die volle Zeichnung zerläuft bei 32 px zu einem Fleck |
| quadratisch | 180 (apple-touch) | randlos ohne Rundung: iOS legt seine eigene Maske darüber, eine schon gerundete Vorlage ergäbe doppelt gerundete Ecken, und transparente Ecken füllt iOS mit Schwarz oder Weiß |
| quer | `logo-mark.svg` | Navigationsleiste. Bleibt SVG (nicht gerastert), damit es mit der Schriftgröße mitskaliert |

**Warum das Zeichen in der Leiste quer liegt:** dort gilt die
Quadrat-Beschränkung nicht, die Variante 17 überhaupt erst nötig gemacht hat.
Quer passt die ganze Szene bei gleicher Höhe rund dreimal so breit hinein – bei
30 px Höhe ist das Fahrzeug damit rund 60 px breit statt 24 px und liegt damit
klar über der Schwelle, ab der es zum Fleck zerläuft. Kachel und Hintergrund
entfallen, weil die Leiste ihren eigenen Grund mitbringt (`--card`) – ein
zweiter dunkler Kasten darin sähe aus wie ein hineingeklebtes App-Icon.

Rasterung läuft über das im Container vorhandene headless Chromium (kein
cairosvg/rsvg im Image), einmal bei 1024 px und dann mit LANCZOS
heruntergerechnet – direkt bei 16 px zu rendern ergibt sichtbar rauhere
Kanten. Die `.ico` wird von Hand zusammengesetzt (PNG-Nutzlast je Eintrag),
damit jede Größe einzeln mit LANCZOS gerechnet wird.

**Nach jeder Änderung an den Icons die Version in `backend/app/changelog.py`
hochzählen** – die Icon-Verweise in `base.html`/`auth_base.html` tragen
`?v={{ version }}`, sonst bleibt das alte Symbol im Tab stehen.

```
design/logo/
  parts.py      gemeinsame Bausteine (Fahrzeug, Säule, Wallbox, Kabel, Farben)
  build.py      die 17 Varianten + Übersichtsseite
  icons.py      erzeugt den App-Icon-Satz aus Variante 17
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
durch, statt siebzehnmal nachgezogen werden zu müssen (und dabei
auseinanderzulaufen). Genau das ist während der Entwurfsarbeit mehrfach
passiert – die Silhouette ist in vier Durchgängen überarbeitet worden, das
Layout in zweien.

## Warum die Breite über die Größe des Fahrzeugs entscheidet

Die Szene aus Säule, Lücke und Fahrzeug ist rund **2,6 : 1** breit, die
Kachel ist **1 : 1**. Damit begrenzt immer die *Breite* die Skalierung, die
Höhe steht im Überfluss zur Verfügung. Jede Einheit Breite, die an Lücke
oder Rand verloren geht, geht deshalb direkt von der Größe des Fahrzeugs ab.

Der erste Wurf verschenkte davon reichlich: 34 Einheiten Lücke zwischen
Säule und Auto, 10 Einheiten Rand in der Bildfläche und ein zu kleines
Bildfeld. Das Fahrzeug belegte dadurch nur **55 %** der Kachelbreite. Eng
gepackt (Lücke auf 14 Einheiten, Bildfeld auf 460 px) sind es **70 %** –
ohne dass sich an der Anordnung selbst etwas ändert.

Weiter kommt man nur, wenn die Anordnung selbst die Höhe nutzt oder Breite
freigibt:

| Anordnung | Fahrzeug | Prinzip |
|---|---|---|
| 01 und die übrigen Kachelentwürfe | 70 % | Säule und Auto nebeneinander |
| 16 Wandbox | 79 % | gestapelt – Wandbox oben, Auto unten |
| 17 Angeschnitten | 83 % | Säule läuft aus der Kachel heraus |

Zweiter Hebel gegen den optischen Leerlauf: die **Standlinie läuft über den
Rand hinaus** statt sichtbar im Bild zu enden (`tiled()` beschneidet die
Szene auf die Kachelform). Das lässt das Quadrat gefüllt wirken, obwohl das
Motiv breit und flach bleibt. Kreis, Sechseck und Kabelrahmen bekommen
dafür eine kürzere Linie mit (`scene(ground_x=…)`) – dort würde eine
auslaufende Linie sichtbar aus dem Zeichen ausbrechen.

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
