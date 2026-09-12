"""Bausteine fuer die Lademonitor-Logo-Varianten.

Alle Varianten (build.py) setzen sich aus denselben Grundformen zusammen -
Fahrzeug, Ladesaeule, Kabel -, damit sie eine Familie bleiben und eine
Korrektur an der Silhouette in allen Entwuerfen gleichzeitig ankommt.

Koordinatensystem der Bausteine
-------------------------------
Jeder Baustein zeichnet in seinem EIGENEN lokalen System und wird von der
Variante per <g transform="translate(...) scale(...)"> platziert. Gemeinsame
Konvention: y = 100 ist die STANDLINIE (Boden). Dadurch stehen Auto und
Ladesaeule automatisch auf derselben Hoehe, egal wie sie skaliert werden.

  Fahrzeug (Seitenansicht, Blickrichtung RECHTS): x 0..190, y 26..100
  Fahrzeug (Frontansicht):                        x 0..200, y 14..100
  Ladesaeule:                                     x 0..40,  y 16..100

Blickrichtung des Fahrzeugs ist bewusst nach RECHTS: die MEB-Plattform
(Enyaq, ID.4) hat die Ladeklappe hinten RECHTS - nur wenn das Auto nach
rechts zeigt, schaut die rechte Fahrzeugseite zum Betrachter und die
Ladeklappe ist ueberhaupt sichtbar. Die Saeule steht deshalb links (hinter
dem Auto), das Kabel hat einen kurzen, lesbaren Weg zum Heck statt quer
ueber das ganze Fahrzeug.
"""

# ---------------------------------------------------------------------------
# Farbwelt
# ---------------------------------------------------------------------------
# Dunkler Hintergrund + graue Silhouette + "Electric Green" fuers Kabel, wie
# vom Nutzer vorgegeben. Die Grundtoene liegen bewusst nah an den bestehenden
# CSS-Variablen der Web-UI (static/style.css: --bg #0f1420, --card #171e2e,
# --muted #8b95ab), damit das Logo nicht neben der eigenen App steht.
BG_DEEP = "#0B1018"      # dunkelster Punkt des Hintergrundverlaufs
BG_BASE = "#0F1420"      # = --bg der Web-UI
BG_SOFT = "#1A2335"      # hellerer Punkt des Verlaufs

GREY_LIGHT = "#AEB8CC"   # Dachkante/Glanzkante
GREY = "#8C96AC"         # Haupt-Grau der Silhouette (nah an --muted)
GREY_MID = "#6E7890"     # Ladesaeule, damit sie hinter das Auto zuruecktritt
GREY_DARK = "#4A5468"    # Reifen, Schattenflaechen
GREY_DEEP = "#2B3446"    # Scheiben, Radausschnitte, Fugen

EV = "#3BF07C"           # Electric Green - Kabel
EV_SOFT = "#2BD46B"      # gedaempfte Variante (Glanz/Sekundaerstriche)
EV_GLOW = "#3BF07C"      # gleiche Farbe, wird nur mit Opazitaet als Schein gelegt

MINT = "#4FD1A5"         # --accent der Web-UI, als Sekundaerakzent verfuegbar

# Strichbild des Kabels: runde Kappen + grosszuegige Luecke, damit die
# Strichelung auch bei 32 px noch als Strichelung lesbar bleibt und nicht zu
# einer durchgezogenen Linie verschmiert.
CABLE_DASH = "9 10"


def cable(d, width=6.5, color=EV, dash=CABLE_DASH, glow=0.16, glow_mul=3.2):
    """Gestricheltes Ladekabel entlang des Pfades *d*.

    Unter dem gestrichelten Strich liegt derselbe Pfad noch einmal breit und
    transparent - das ergibt den elektrischen Schein, ohne einen SVG-Filter
    zu brauchen (Filter werden von Favicon-Renderern und beim Rastern auf
    kleine Groessen sehr unterschiedlich behandelt, eine zweite Linie nicht).
    """
    out = []
    if glow:
        out.append(
            f'<path d="{d}" fill="none" stroke="{EV_GLOW}" stroke-width="{width * glow_mul:.1f}" '
            f'stroke-linecap="round" opacity="{glow}"/>'
        )
    out.append(
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linecap="round" stroke-dasharray="{dash}"/>'
    )
    return "".join(out)


# ---------------------------------------------------------------------------
# Fahrzeug - Seitenansicht
# ---------------------------------------------------------------------------
# Anlehnung an den Enyaq, bewusst "gestaucht": echte Laenge/Hoehe waeren rund
# 2,9:1, hier sind es 190:74 = 2,6:1. Dazu ueberproportional grosse Raeder
# (r=20 statt massstaeblich ~15) und dicke, runde Radlaeufe - das ist der
# Comic-Griff, der die Form freundlich statt technisch wirken laesst.
#
# Enyaq-Merkmale, die in der Seitenansicht ueberhaupt ablesbar sind:
#   - kurzer Vorderwagen, steil stehende, hohe Front (MEB: kein Motorraum)
#   - flach ansteigende Gürtellinie, nach hinten abfallendes Dach (Coupé-Zug)
#   - kraeftige, eckig angedeutete Radhaus-Verbreiterungen
#   - schmale Leuchten vorn/hinten, durchgehende Heckleuchte

CAR_BODY = (
    "M 4 80 "
    "C 3 71, 3 66, 6 60 "          # Heckschuerze, aufrecht und stumpf
    "C 9 52, 12 46, 18 41 "        # steile Heckklappe
    "C 26 34, 32 30, 42 28.5 "     # Schulter in die Dachlinie
    "L 114 27 "                     # Dach: flach, nur minimal gewoelbt
    "C 126 27, 136 33, 146 45 "    # gezogene A-Saeule
    "C 156 45.5, 166 45.5, 174 46.5 "  # sehr kurze Haube (MEB: kein Motorraum)
    "C 180 47, 182 53, 182 61 "    # hoher, stumpfer Bug
    "C 182 71, 181 77, 179 80 "    # Frontschuerze
    "L 164 80 "
    "A 22 22 0 0 0 120 80 "        # vorderer Radlauf (Mitte 142)
    "L 62 80 "
    "A 22 22 0 0 0 18 80 "         # hinterer Radlauf (Mitte 40)
    "Z"
)

# Flaches, hoch angesetztes Fensterband - der Enyaq hat deutlich mehr Blech
# als Glas. Verhaeltnis hier rund 1:2,6 (14 Einheiten Glas, 36 Karosserie).
CAR_GLASS = (
    "M 22 41 "
    "C 29 34.5, 37 31.5, 48 30.5 "
    "L 114 30 "
    "C 124 31.5, 133 37, 140 44 "
    "Z"
)

# Laenge 178, Hoehe 73 -> 2,44:1. Der echte Enyaq liegt bei 2,88:1; die
# fehlenden gut 15 % sind genau die gewuenschte Stauchung. Dazu Raeder mit
# r=20 statt massstaeblichen ~15 - zusammen ergibt das den Comic-Griff.
WHEEL_FRONT = (142, 80)
WHEEL_REAR = (40, 80)
CHARGE_PORT = (27, 49)   # Ladeklappe: hinteres Seitenteil (MEB: hinten rechts)
CAR_W, CAR_H = 186, 100  # nutzbarer Rahmen inkl. Standlinie


def wheel(cx, cy, r=20, tire=GREY_DEEP, rim=GREY, hub=GREY_DEEP):
    """Rad im Comic-Schnitt: dicker Reifen, wenige, breite Speichen."""
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{tire}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r * 0.52:.1f}" fill="{rim}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r * 0.17:.1f}" fill="{hub}"/>'
    )


def car_side(body=GREY, glass=GREY_DEEP, detail=GREY_DEEP, tire=GREY_DEEP,
             rim=GREY_MID, port=EV, outline=None, outline_width=0,
             details=True, glow_edge=False):
    """Fahrzeug-Silhouette in der Seitenansicht.

    *details* schaltet Fugen, Griffe, Leuchten und Scheiben ab - fuer die
    ganz reduzierten Varianten (Favicon-Groesse), in denen jede zusaetzliche
    Linie bei 32 px nur noch Matsch ergibt.
    """
    s = []
    stroke = ""
    if outline and outline_width:
        stroke = (f' stroke="{outline}" stroke-width="{outline_width}"'
                  f' stroke-linejoin="round"')

    s.append(f'<path d="{CAR_BODY}" fill="{body}"{stroke}/>')

    if details:
        s.append(f'<path d="{CAR_GLASS}" fill="{glass}"/>')
        # B-/C-Saeule teilen das Fensterband in Front-, Fond- und
        # Dreiecksscheibe. In Karosseriefarbe, damit sie wie Blech wirken.
        s.append(f'<path d="M 88 29.8 L 88 43.4" stroke="{body}" stroke-width="4"/>')
        s.append(f'<path d="M 45 29.6 L 40 41.6" stroke="{body}" stroke-width="4.4"/>')
        # Tuerfugen
        s.append(f'<path d="M 90 45 L 90 72" stroke="{detail}" stroke-width="1.8" '
                 f'stroke-linecap="round" opacity="0.45"/>')
        s.append(f'<path d="M 46 42 L 46 71" stroke="{detail}" stroke-width="1.8" '
                 f'stroke-linecap="round" opacity="0.45"/>')
        # Tuergriffe auf der Guertellinie
        s.append(f'<rect x="64" y="49" width="13" height="4" rx="2" fill="{detail}" opacity="0.6"/>')
        s.append(f'<rect x="98" y="49" width="13" height="4" rx="2" fill="{detail}" opacity="0.6"/>')
        # Schulterlinie
        s.append(f'<path d="M 16 60 C 70 57, 120 57, 174 59" stroke="{detail}" '
                 f'stroke-width="2.2" fill="none" opacity="0.28" stroke-linecap="round"/>')
        # Leuchten: vorn ein schmales Band im Bug, hinten die hohe Ecke
        s.append(f'<path d="M 163 52 L 180 54 L 180 59 L 163 57 Z" fill="{detail}" opacity="0.85"/>')
        s.append(f'<path d="M 5 50 L 15 48.5 L 15 54 L 5.4 55.5 Z" fill="{detail}" opacity="0.85"/>')
        # Kantige Radhaus-Verbreiterungen (Kunststoffleisten)
        s.append(f'<path d="M 15 78 A 25 25 0 0 1 65 78" fill="none" stroke="{detail}" '
                 f'stroke-width="3" opacity="0.16" stroke-linecap="round"/>')
        s.append(f'<path d="M 117 78 A 25 25 0 0 1 167 78" fill="none" stroke="{detail}" '
                 f'stroke-width="3" opacity="0.16" stroke-linecap="round"/>')

    if glow_edge:
        # Lichtkante entlang Dach und Haube - laesst die Flaeche plastisch
        # wirken, ohne einen Verlauf im Fuellwert zu brauchen.
        s.append(f'<path d="M 16 42 C 26 34, 33 30.5, 42 29 L 114 27.5 '
                 f'C 126 27.5, 136 34, 146 45 C 158 45.5, 168 45.5, 176 46.6" '
                 f'fill="none" stroke="{GREY_LIGHT}" stroke-width="2.6" '
                 f'stroke-linecap="round" opacity="0.45"/>')

    s.append(wheel(*WHEEL_FRONT, tire=tire, rim=rim))
    s.append(wheel(*WHEEL_REAR, tire=tire, rim=rim))

    if port:
        # Ladeklappe als kleiner leuchtender Punkt - der visuelle Ankerpunkt,
        # an dem das Kabel sichtbar "andockt".
        s.append(f'<circle cx="{CHARGE_PORT[0]}" cy="{CHARGE_PORT[1]}" r="3.8" fill="{port}"/>')
        s.append(f'<circle cx="{CHARGE_PORT[0]}" cy="{CHARGE_PORT[1]}" r="7.2" fill="{port}" opacity="0.2"/>')
    return "".join(s)


def car_side_flat(color=GREY):
    """Reine Silhouette ohne jede Binnenzeichnung - fuer Favicon/Stempel."""
    return (
        f'<path d="{CAR_BODY}" fill="{color}"/>'
        f'<circle cx="{WHEEL_FRONT[0]}" cy="{WHEEL_FRONT[1]}" r="20" fill="{color}"/>'
        f'<circle cx="{WHEEL_REAR[0]}" cy="{WHEEL_REAR[1]}" r="20" fill="{color}"/>'
    )


# ---------------------------------------------------------------------------
# Ladesaeule
# ---------------------------------------------------------------------------
# Bewusst schmal und einen Tick hoeher als das Fahrzeugdach (Saeulenkopf y=16
# gegen Dachkante y=26): so bleibt sie als zweites Element erkennbar, drueckt
# aber das Auto nicht aus der Rolle des Hauptmotivs.

def station(body=GREY_MID, panel=GREY_DEEP, light=EV, cap=GREY_LIGHT,
            screen=True, base=True):
    """Ladesaeule, Standlinie y=100.

    Der Kabelabgang sitzt auf der dem Auto ZUGEWANDTEN Seite (rechts, siehe
    STATION_OUTLET) und auf Huefthoehe - so faellt das Kabel von selbst zum
    Fahrzeug hin durch. Sass er anfangs links, musste sich das Kabel um die
    Saeule herumwickeln, was im Test wie ein Knoten aussah.

    Etwas hoeher als die Dachkante des Autos (Kopf y=16 gegen Dach y=27),
    damit sie als eigenstaendiges zweites Element lesbar bleibt - aber
    schmal genug, dass das Fahrzeug das Hauptmotiv bleibt.
    """
    s = []
    if base:
        # Sockel, angedeutet statt ausmodelliert: ohne ihn scheint die Saeule
        # auf der Standlinie zu schweben, mit zu viel Sockel kippt sie optisch.
        s.append(f'<rect x="-4" y="93" width="48" height="7" rx="3.5" fill="{body}" opacity="0.5"/>')
    # Kabelabgang liegt HINTER der Saeule, damit die Kante sauber bleibt
    s.append(f'<rect x="22" y="57" width="20" height="18" rx="8" fill="{body}"/>')
    s.append(f'<rect x="3" y="16" width="34" height="80" rx="13" fill="{body}"/>')
    # Schmale Lichtkante am Kopf - gibt der Saeule vor dunklem Grund eine
    # Oberkante, statt sie als Balken ohne Anfang auslaufen zu lassen.
    s.append(f'<path d="M 4.2 33 L 4.2 29 A 11.8 11.8 0 0 1 16 17.2 L 24 17.2 '
             f'A 11.8 11.8 0 0 1 35.8 29 L 35.8 33" fill="none" stroke="{cap}" '
             f'stroke-width="2.4" stroke-linecap="round" opacity="0.4"/>')
    if screen:
        s.append(f'<rect x="9" y="24" width="22" height="23" rx="5" fill="{panel}"/>')
        # Batteriesymbol im Display: sagt "laedt gerade" ohne Text und bleibt
        # auch bei 32 px noch als kleiner Balken erkennbar.
        s.append(f'<rect x="12.5" y="30" width="15" height="11" rx="2.5" fill="none" '
                 f'stroke="{light}" stroke-width="1.8" opacity="0.55"/>')
        s.append(f'<rect x="14.5" y="32" width="7" height="7" rx="1.2" fill="{light}"/>')
        s.append(f'<rect x="28.2" y="33" width="2" height="5" rx="1" fill="{light}" opacity="0.55"/>')
    # Statusband unter dem Display
    s.append(f'<rect x="9" y="51" width="22" height="4" rx="2" fill="{light}" opacity="0.9"/>')
    # Buchse im Kabelabgang - der sichtbare Anschlusspunkt des Kabels
    s.append(f'<circle cx="35" cy="66" r="4.6" fill="{panel}"/>')
    return "".join(s)


STATION_OUTLET = (40.0, 66.0)   # Ankerpunkt des Kabels an der Saeule


def station_slim(body=GREY_MID, light=EV):
    """Stark reduzierte Saeule fuer kleine Groessen: keine Displaydetails."""
    return (
        f'<rect x="24" y="58" width="18" height="17" rx="8" fill="{body}"/>'
        f'<rect x="4" y="18" width="32" height="80" rx="14" fill="{body}"/>'
        f'<rect x="10" y="28" width="20" height="22" rx="5" fill="{GREY_DEEP}"/>'
        f'<rect x="10" y="56" width="20" height="5" rx="2.5" fill="{light}"/>'
    )


STATION_SLIM_OUTLET = (41.0, 66.5)


# ---------------------------------------------------------------------------
# Fahrzeug - Frontansicht ("Crystal Face")
# ---------------------------------------------------------------------------
# Die Front ist das eigentliche Erkennungszeichen des Enyaq: breiter,
# aufrechter Grill mit senkrechten Rippen (beim Topmodell beleuchtet),
# schmale zweiteilige Scheinwerfer, hoher Aufbau. In der Seitenansicht ist
# davon nichts ablesbar - deshalb gibt es diese zweite Ansicht als eigene
# Entwurfsrichtung. Standlinie ebenfalls y=100.

# Aufbau bewusst aus ZWEI ueberlappenden Flaechen (Unterbau + Kabine) statt
# aus einer Kontur: in der Frontansicht sitzt die Kabine deutlich schmaler auf
# einem breiten Unterbau. Als eine einzige Kontur gezeichnet verschliff der
# Absatz an der Schulter und das Ganze las sich als Brotlaib.
#
# Proportionen: 150 breit, 94 hoch (Standlinie y=100, Dach y=6) -> 1,6:1.
# Der echte Enyaq liegt von vorn bei 1879/1616 = 1,16:1, ist also noch
# aufrechter; 1,6 ist der Kompromiss aus "wiedererkennbar hoch" und
# "funktioniert in einer quadratischen Kachel". Der erste Entwurf mit 2,6:1
# las sich als Kombi-Front und war der eigentliche Fehler.
FRONT_LOWER = (
    "M 12 88 "
    "C 7 78, 7 60, 12 50 "
    "C 17 44, 36 41, 75 41 "
    "C 114 41, 133 44, 138 50 "
    "C 143 60, 143 78, 138 88 "
    "C 132 95, 112 96, 75 96 "
    "C 38 96, 18 95, 12 88 "
    "Z"
)

FRONT_CABIN = (
    "M 28 52 "
    "C 30 24, 38 10, 51 7 "
    "L 99 7 "
    "C 112 10, 120 24, 122 52 "
    "Z"
)

FRONT_GLASS = (
    "M 35 47 "
    "C 37 26, 44 15, 55 13 "
    "L 95 13 "
    "C 106 15, 113 26, 115 47 "
    "Z"
)


def car_front(body=GREY, glass=GREY_DEEP, grille=GREY_DEEP, bar=EV,
              tire=GREY_DEEP, rim=GREY_MID, lit_grille=True):
    """Frontansicht mit angedeutetem Crystal Face.

    Der senkrecht gerippte Grill ist das eigentliche Erkennungszeichen des
    Enyaq und in der Seitenansicht ueberhaupt nicht darstellbar - deshalb
    gibt es diese zweite Ansicht ueberhaupt.
    """
    s = []
    # Raeder zuerst: sie stehen seitlich unter dem Unterbau hervor
    s.append(f'<rect x="0" y="66" width="20" height="34" rx="9" fill="{tire}"/>')
    s.append(f'<rect x="130" y="66" width="20" height="34" rx="9" fill="{tire}"/>')
    s.append(f'<path d="{FRONT_CABIN}" fill="{body}"/>')
    s.append(f'<path d="{FRONT_GLASS}" fill="{glass}"/>')
    s.append(f'<path d="M 32 38 C 34 22, 40 11, 52 8 L 98 8 '
             f'C 110 11, 116 22, 118 38" fill="none" stroke="{GREY_LIGHT}" '
             f'stroke-width="2.6" stroke-linecap="round" opacity="0.42"/>')
    s.append(f'<path d="{FRONT_LOWER}" fill="{body}"/>')
    # Schulterfuge: trennt Kabine und Unterbau sichtbar voneinander
    s.append(f'<path d="M 15 48 C 42 43, 108 43, 135 48" fill="none" '
             f'stroke="{GREY_LIGHT}" stroke-width="2.4" opacity="0.4" stroke-linecap="round"/>')
    # Scheinwerfer: schmale Baender, nach aussen leicht ansteigend
    s.append(f'<path d="M 14 58 L 42 54 L 42 63 L 16 66 Z" fill="{glass}"/>')
    s.append(f'<path d="M 136 58 L 108 54 L 108 63 L 134 66 Z" fill="{glass}"/>')
    s.append(f'<path d="M 17.5 59 L 39 55.8 L 39 59.4 L 18.5 62.4 Z" fill="{GREY_LIGHT}"/>')
    s.append(f'<path d="M 132.5 59 L 111 55.8 L 111 59.4 L 131.5 62.4 Z" fill="{GREY_LIGHT}"/>')
    # Crystal Face: grosser aufrechter Grill mit senkrechten Rippen
    s.append(f'<rect x="45" y="52" width="60" height="28" rx="9" fill="{grille}"/>')
    for i in range(8):
        x = 49.5 + i * 6.9
        col = bar if lit_grille else rim
        op = 0.95 - abs(i - 3.5) * 0.10 if lit_grille else 0.4
        s.append(f'<rect x="{x:.1f}" y="56" width="2.9" height="20" rx="1.4" '
                 f'fill="{col}" opacity="{op:.2f}"/>')
    # Untere Schuerze
    s.append(f'<rect x="38" y="84" width="74" height="8" rx="4" fill="{glass}" opacity="0.6"/>')
    return "".join(s)


def wallbox(body=GREY_MID, panel=GREY_DEEP, light=EV, w=54, h=66):
    """Wandladestation - Kopf ohne Standsaeule.

    Gebraucht, wo das Kabel eine eigene Form zeichnen soll (Monogramm): eine
    bodenstehende Saeule wuerde das Kabel zwingen, erst hochzulaufen.
    Kabelabgang unten mittig, siehe WALLBOX_OUTLET.
    """
    return (
        f'<rect x="0" y="0" width="{w}" height="{h}" rx="14" fill="{body}"/>'
        f'<rect x="{w*0.17:.1f}" y="{h*0.15:.1f}" width="{w*0.66:.1f}" '
        f'height="{h*0.42:.1f}" rx="6" fill="{panel}"/>'
        f'<rect x="{w*0.27:.1f}" y="{h*0.27:.1f}" width="{w*0.30:.1f}" '
        f'height="{h*0.18:.1f}" rx="3" fill="{light}"/>'
        f'<rect x="{w*0.20:.1f}" y="{h*0.70:.1f}" width="{w*0.60:.1f}" '
        f'height="{h*0.08:.1f}" rx="3" fill="{light}" opacity="0.9"/>'
    )


# ---------------------------------------------------------------------------
# Hintergruende
# ---------------------------------------------------------------------------

def squircle(size=512, r_ratio=0.2237, fill="url(#bg)"):
    """iOS-artige Kachel. r ~ 22,4 % der Kantenlaenge trifft die Apple-Form
    deutlich besser als das oft benutzte 'einfach sehr rund'."""
    r = size * r_ratio
    return f'<rect x="0" y="0" width="{size}" height="{size}" rx="{r:.1f}" fill="{fill}"/>'


def bg_gradient(gid="bg", a=BG_SOFT, b=BG_DEEP, x1=0, y1=0, x2=0.35, y2=1):
    return (
        f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
        f'<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/>'
        f'</linearGradient>'
    )


def radial_glow(gid="glow", color=EV, inner=0.30, outer=0.0):
    return (
        f'<radialGradient id="{gid}">'
        f'<stop offset="0" stop-color="{color}" stop-opacity="{inner}"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="{outer}"/>'
        f'</radialGradient>'
    )


def svg(width, height, body, defs="", title=""):
    t = f"<title>{title}</title>" if title else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">{t}'
        f'<defs>{defs}</defs>{body}</svg>'
    )


def car_side_simple(body=GREY, glass=GREY_DEEP, tire=GREY_DEEP):
    """Fahrzeug auf drei Formen reduziert - fuer Favicon-Groessen.

    Weniger geht nicht: ohne die dunkle Scheibe kippt die Silhouette zum
    Brotlaib, ohne dunkle Raeder verschwindet der Unterschied zwischen Rad
    und Karosserie. Fugen, Griffe und Leuchten dagegen sind bei 32 px
    ohnehin nur noch Grauschleier und entfallen.
    """
    return (
        f'<path d="{CAR_BODY}" fill="{body}"/>'
        f'<path d="{CAR_GLASS}" fill="{glass}"/>'
        f'<circle cx="{WHEEL_FRONT[0]}" cy="{WHEEL_FRONT[1]}" r="19" fill="{tire}"/>'
        f'<circle cx="{WHEEL_REAR[0]}" cy="{WHEEL_REAR[1]}" r="19" fill="{tire}"/>'
    )
