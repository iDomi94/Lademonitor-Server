#!/usr/bin/env python3
"""Erzeugt die Logo-Varianten nach out/.

    python3 design/logo/build.py

Alle Varianten teilen sich die Bausteine aus parts.py (Fahrzeug, Ladesaeule,
Kabel) - eine Korrektur an der Silhouette schlaegt dadurch in allen Entwuerfen
gleichzeitig durch, statt 15-mal nachgezogen werden zu muessen.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parts import *  # noqa: F403

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
SIZE = 512

# Gemeinsamer Bildausschnitt der Seitenansicht-Szene (Saeule links, Auto
# rechts). Bewusst als feste Konstante: dadurch sitzt das Motiv in allen
# Varianten gleich gross im Rahmen und die Reihe wirkt als Familie.
#
# Die Szene ist rund 2,6:1 breit, die Kachel 1:1 - die BREITE begrenzt also
# die Skalierung, die Hoehe ist im Ueberfluss da. Jede Einheit Breite, die
# Luecke oder Rand verschenkt, geht deshalb direkt von der Groesse des
# Fahrzeugs ab. Der erste Entwurf verschenkte davon reichlich (Luecke von 34
# Einheiten zwischen Saeule und Auto, dazu 10 Einheiten Rand im Rahmen und
# ein zu kleines Bildfeld) und liess das Auto nur 55 % der Kachelbreite
# einnehmen; eng gepackt sind es rund 70 %.
STATION_X = 0
CAR_X = 52
SCENE_BB = (-5, 10, 236, 102)

OUTLET = (STATION_OUTLET[0] + STATION_X, STATION_OUTLET[1])
PORT = (CAR_X + CHARGE_PORT[0], CHARGE_PORT[1])


def fit(bbox, bx, by, bw, bh):
    """Transform, der *bbox* mittig in das Rechteck (bx,by,bw,bh) einpasst."""
    minx, miny, maxx, maxy = bbox
    w, h = maxx - minx, maxy - miny
    s = min(bw / w, bh / h)
    tx = bx + (bw - s * w) / 2 - s * minx
    ty = by + (bh - s * h) / 2 - s * miny
    return f"translate({tx:.2f},{ty:.2f}) scale({s:.4f})"


TILE_CLIP = (f'<clipPath id="tile"><rect x="0" y="0" width="{SIZE}" '
             f'height="{SIZE}" rx="{SIZE * 0.2237:.1f}"/></clipPath>')


def tiled(bbox, box, inner):
    """Szene, eingepasst UND auf die Kachelform beschnitten.

    Ohne den Beschnitt duerfte die Standlinie nicht ueber den Rahmen
    hinauslaufen - und genau das laesst die quadratische Kachel gefuellt
    wirken, obwohl das Motiv breit und flach ist: die Linie endet nicht
    sichtbar im Bild, sondern laeuft aus ihm heraus.
    """
    return (f'<g clip-path="url(#tile)"><g transform="{fit(bbox, *box)}">'
            f'{inner}</g></g>')


def ground(y=101, x0=-90, x1=340, color=GREY_DEEP, op=0.9, w=2.2):
    return (f'<path d="M {x0} {y} L {x1} {y}" stroke="{color}" stroke-width="{w}" '
            f'stroke-linecap="round" opacity="{op}"/>')


def shadow(cx, rx, y=100.5, op=0.32):
    """Weicher Bodenschatten - gibt dem Motiv Gewicht, ohne einen Filter."""
    return (f'<ellipse cx="{cx}" cy="{y}" rx="{rx}" ry="3.4" fill="{BG_DEEP}" '
            f'opacity="{op}"/>')


# Standard-Kabelverlauf: sackt in die Luecke zwischen Saeule und Auto und
# steigt am Heck zur Ladeklappe an. Ein durchhaengendes Kabel liest sich
# sofort als Kabel - eine straff gezogene Linie eher als Strich.
CABLE_SAG = (f"M {OUTLET[0]} {OUTLET[1]} C 46 84, 58 88, 68 78 "
             f"C 74 71, 76 58, {PORT[0]} {PORT[1]}")


def write(name, markup):
    path = os.path.join(OUT, name + ".svg")
    with open(path, "w") as fh:
        fh.write(markup)
    return path




def scene(car_kw=None, station_kw=None, cable_d=None, cable_kw=None,
          ground_op=0.5, ground_x=(-90, 340), extra_back="", extra_front=""):
    """Die Standardszene: Saeule links, Auto rechts, Kabel dazwischen.

    Zehn der fuenfzehn Entwuerfe unterscheiden sich nur im Rahmen und im
    Kabelverlauf - die bauen alle hierauf auf, statt die Anordnung jeweils
    neu zu setzen (und dabei minimal auseinanderzulaufen).
    """
    car_kw = car_kw or {}
    station_kw = station_kw or {}
    cable_kw = cable_kw or {}
    return (
        extra_back
        + ground(x0=ground_x[0], x1=ground_x[1], op=ground_op)
        + shadow(CAR_X + 92, 92) + shadow(20, 26)
        + f'<g>{station(**station_kw)}</g>'
        + f'<g transform="translate({CAR_X},0)">{car_side(**car_kw)}</g>'
        + cable(cable_d or CABLE_SAG, **cable_kw)
        + extra_front
    )


ICON_BOX = (26, 88, 460, 336)   # Bildflaeche innerhalb der 512er Kachel


# ===========================================================================
# 01 - Klassik: die Grundaufstellung, an der sich alles andere misst.
# ===========================================================================
def v01_klassik():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g1", EV, 0.22)
    inner = scene(car_kw={"glow_edge": True},
                  extra_back=f'<ellipse cx="46" cy="62" rx="74" ry="56" '
                             f'fill="url(#g1)" opacity="0.55"/>')
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(SCENE_BB, ICON_BOX, inner),
               defs, "Lademonitor")


# ===========================================================================
# 02 - Kreisemblem: das Kabel laeuft zusaetzlich als gestrichelter Ring um
#      die Szene. Die Luecke oben laesst ihn wie ein aufgerolltes Kabel
#      wirken statt wie einen blossen Rahmen.
# ===========================================================================
def v02_kreis():
    defs = TILE_CLIP + bg_gradient("bg", BG_SOFT, BG_DEEP) + radial_glow("g2", EV, 0.13)
    c, r = SIZE / 2, 226
    inner = scene(ground_x=(-20, 250), cable_kw={"width": 5.6})
    body = (
        f'<circle cx="{c}" cy="{c}" r="{r + 18}" fill="url(#bg)"/>'
        f'<circle cx="{c}" cy="{c}" r="{r + 18}" fill="url(#g2)"/>'
        f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{GREY_DEEP}" stroke-width="2"/>'
        f'<path d="M {c + r * 0.32:.1f} {c - r * 0.95:.1f} '
        f'A {r} {r} 0 1 1 {c - r * 0.32:.1f} {c - r * 0.95:.1f}" '
        f'fill="none" stroke="{EV}" stroke-width="10" stroke-linecap="round" '
        f'stroke-dasharray="15 18"/>'
        f'<g transform="{fit(SCENE_BB, 84, 160, 344, 216)}">{inner}</g>'
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 03 - Puls: das Kabel wird zur Herzschlag-/Messkurve.
#      Der eigentliche Einfall dieser Reihe - die App heisst Lade-MONITOR,
#      nicht Ladezaehler. Der Ausschlag sitzt genau in der Luecke zwischen
#      Saeule und Auto, wo sonst nur Leerraum waere.
# ===========================================================================
def v03_puls():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g3", EV, 0.20)
    pulse = (f"M {OUTLET[0]} {OUTLET[1]} C 43 76, 43 82, 46 84 "
             f"L 53 84 L 58 62 L 66 104 L 73 72 L 78 84 L 86 84 "
             f"C 93 82, 95 58, {PORT[0]} {PORT[1]}")
    inner = scene(cable_d=pulse, ground_op=0.4,
                  cable_kw={"width": 6, "dash": "none", "glow": 0.22},
                  extra_back=f'<ellipse cx="66" cy="80" rx="74" ry="44" '
                             f'fill="url(#g3)" opacity="0.6"/>')
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(SCENE_BB, ICON_BOX, inner),
               defs, "Lademonitor")


# ===========================================================================
# 04 - Ladering: der Ring zeigt den Ladestand (hier 72 %). Dieselbe Metapher,
#      die die App im Dashboard benutzt - das Zeichen sagt damit nicht nur
#      "Auto + Strom", sondern "wie voll ist es".
# ===========================================================================
def v04_ladering():
    defs = TILE_CLIP + bg_gradient()
    c, r, w = SIZE / 2, 198, 20
    pct = 72
    inner = scene(ground_x=(-20, 250), ground_op=0.35, cable_kw={"width": 5.4})
    body = (
        squircle(SIZE)
        + f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{GREY_DEEP}" '
          f'stroke-width="{w}"/>'
        # pathLength=100 macht den Ladestand direkt in Prozent adressierbar:
        # stroke-dasharray="<prozent> 100" - keine Umrechnung ueber 2*pi*r.
        + f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{EV}" '
          f'stroke-width="{w}" stroke-linecap="round" pathLength="100" '
          f'stroke-dasharray="{pct} 100" transform="rotate(-90 {c} {c})"/>'
        + tiled(SCENE_BB, (116, 196, 280, 150), inner)
        + f'<text x="{c}" y="{c + 118}" text-anchor="middle" font-size="46" '
          f'font-weight="700" font-family="Inter, -apple-system, Segoe UI, sans-serif" '
          f'fill="{EV}" opacity="0.95">{pct} %</text>'
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 05 - Blitzkabel: das Kabel knickt als Blitz, statt durchzuhaengen.
#      Nimmt das Motiv des bisherigen Icons auf, bindet es aber INS Kabel
#      ein, statt es als zweites Zeichen daneben zu stellen.
# ===========================================================================
def v05_blitz():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g5", EV, 0.24)
    # Klassische Blitzform: hin, scharf zurueck, hin. Der erste Versuch mit
    # vier gleichmaessigen Zacken las sich als Schallwelle, nicht als Blitz.
    bolt = f"M {OUTLET[0]} {OUTLET[1]} L 74 50 L 56 80 L {PORT[0]} {PORT[1]}"
    inner = scene(cable_d=bolt, ground_op=0.4,
                  cable_kw={"width": 6.6, "dash": "13 9"},
                  extra_back=f'<ellipse cx="66" cy="74" rx="66" ry="48" '
                             f'fill="url(#g5)" opacity="0.6"/>')
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(SCENE_BB, ICON_BOX, inner),
               defs, "Lademonitor")


# ===========================================================================
# 06 - Monogramm: das Kabel schreibt das "L" von Lademonitor.
#      Nutzt bewusst eine WANDBOX statt der Standsaeule - an einer
#      bodenstehenden Saeule muesste das Kabel erst hochlaufen, der
#      Buchstabe waere dahin.
# ===========================================================================
def v06_monogramm():
    defs = TILE_CLIP + bg_gradient()
    x0, ytop, ybot = 122, 132, 400
    s_car = 1.34
    port_xy = (268, 332)
    car_x = port_xy[0] - CHARGE_PORT[0] * s_car
    car_y = port_xy[1] - CHARGE_PORT[1] * s_car
    lpath = (f"M {x0} {ytop} L {x0} {ybot} L 214 {ybot} "
             f"C 244 {ybot}, 246 358, {port_xy[0]} {port_xy[1]}")
    body = (
        squircle(SIZE)
        + f'<g transform="translate({x0 - 31},50)">{wallbox(w=62, h=76)}</g>'
        + cable(lpath, width=17, dash="24 20", glow=0.13, glow_mul=2.3)
        + f'<g transform="translate({car_x:.1f},{car_y:.1f}) scale({s_car})">'
          f'{car_side()}</g>'
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 07 - Crystal Face: Frontansicht. Das Kabel laeuft hinter dem Fahrzeug weg
#      (vor ihm gezeichnet, also davon verdeckt) - so steckt es sichtbar auf
#      der abgewandten Seite, ohne die Front zu zerschneiden.
# ===========================================================================
def v07_front():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g7", EV, 0.26)
    inner = (
        f'<ellipse cx="78" cy="56" rx="96" ry="66" fill="url(#g7)" opacity="0.45"/>'
        + ground(x0=-30, x1=250, op=0.5) + shadow(75, 72, op=0.4)
        + shadow(196, 24, op=0.35)
        + cable("M 196 70 C 186 92, 162 96, 136 88", width=6.4)
        + f'<g transform="translate(172,4) scale(0.82)">{station()}</g>'
        + car_front()
    )
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled((-10, 0, 224, 104), (26, 74, 460, 364), inner),
               defs, "Lademonitor")


# ===========================================================================
# 08 - Statistik: Monatsbalken als Untergrund, das Kabel als Verlaufskurve.
#      Die App ist in erster Linie eine Auswertung - das sagt das Zeichen mit.
# ===========================================================================
def v08_statistik():
    defs = TILE_CLIP + bg_gradient()
    heights = [16, 25, 21, 33, 28, 41]
    bars = "".join(
        f'<rect x="{2 + i * 38}" y="{100 - h}" width="19" height="{h}" rx="4" '
        f'fill="{GREY_MID}" opacity="{0.11 + i * 0.021:.3f}"/>'
        for i, h in enumerate(heights))
    inner = scene(
        ground_op=0.6, extra_back=bars,
        cable_d=f"M {OUTLET[0]} {OUTLET[1]} C 52 84, 66 74, 80 68 "
                f"C 90 63, 94 56, {PORT[0]} {PORT[1]}",
        cable_kw={"width": 6.2})
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(SCENE_BB, ICON_BOX, inner),
               defs, "Lademonitor")


# ===========================================================================
# 09 - Monoline: bewusst arm an Details, damit das Zeichen bei 32 px und
#      16 px (Favicon, Tab-Leiste) nicht zu Matsch zerfaellt. Keine Fugen,
#      keine Griffe, wenige lange Striche im Kabel statt vieler kurzer -
#      und eine dunkle Fuge zwischen Rad und Radlauf, sonst verschmilzt
#      beides bei kleiner Groesse zu einem Klumpen.
# ===========================================================================
def v09_monoline():
    defs = TILE_CLIP + bg_gradient()
    # Deutlich groesser im Rahmen als die uebrigen Entwuerfe und ohne Sockel,
    # Display und Statusband an der Saeule: der Test bei 32 px hat gezeigt,
    # dass die volle Szene dort zu einem Fleck zerlaeuft. Hier bleiben genau
    # drei Zeichen uebrig - graue Saeule, graues Auto, gruener Strich.
    bb = (2, 14, 244, 102)
    inner = (
        f'<g>{station_slim()}</g>'
        + f'<g transform="translate(56,0)">{car_side_simple()}</g>'
        + cable(f"M {STATION_SLIM_OUTLET[0]} {STATION_SLIM_OUTLET[1]} "
                f"C 54 92, 72 94, 84 80 C 90 72, 90 60, 96 50",
                width=12, dash="20 17", glow=0)
    )
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(bb, (30, 126, 452, 260), inner),
               defs, "Lademonitor")


# ===========================================================================
# 10 - Lockup: Zeichen + Wortmarke nebeneinander, fuer Kopfleiste, README
#      und Store-Zeile. Andere Leinwand als die Icon-Varianten (breit statt
#      quadratisch), deshalb eigene Datei statt einer Ableitung.
#
#      Hinweis fuer die Reinzeichnung: der Schriftzug liegt hier noch als
#      <text> vor und wird damit von der Schrift des jeweiligen Systems
#      gerendert. Fuer die finale Marke muss er in Pfade umgewandelt werden,
#      sonst sieht das Logo auf jedem Geraet anders aus.
# ===========================================================================
FONT = "Inter, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif"


def v10_lockup():
    W, H = 1280, 360
    defs = TILE_CLIP + bg_gradient("bg", BG_SOFT, BG_DEEP, 0, 0, 1, 1)
    inner = scene(ground_op=0.4, cable_kw={"width": 6})
    body = (
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>'
        + tiled(SCENE_BB, (44, 66, 452, 228), inner)
        + f'<text x="522" y="186" font-family="{FONT}" font-size="92" '
          f'font-weight="700" letter-spacing="-2" fill="{GREY_LIGHT}">'
          f'Lade<tspan fill="{EV}">monitor</tspan></text>'
        + f'<text x="526" y="244" font-family="{FONT}" font-size="26" '
          f'font-weight="500" letter-spacing="6.4" fill="{GREY_MID}">'
          f'LADEVORGÄNGE IM BLICK</text>'
    )
    return svg(W, H, body, defs, "Lademonitor")


# ===========================================================================
# 11 - Kabelrahmen: das Kabel umschliesst die Szene als gestrichelter
#      Rahmen. Sehr eigenstaendige Aussenform in einer Reihe runder
#      App-Icons - das Zeichen ist schon am Rand erkennbar.
# ===========================================================================
def v11_rahmen():
    defs = TILE_CLIP + bg_gradient()
    m, r = 46, 86
    frame = (f"M {SIZE/2} {m} L {SIZE-m-r} {m} A {r} {r} 0 0 1 {SIZE-m} {m+r} "
             f"L {SIZE-m} {SIZE-m-r} A {r} {r} 0 0 1 {SIZE-m-r} {SIZE-m} "
             f"L {m+r} {SIZE-m} A {r} {r} 0 0 1 {m} {SIZE-m-r} "
             f"L {m} {m+r} A {r} {r} 0 0 1 {m+r} {m} Z")
    inner = scene(ground_x=(-20, 250), ground_op=0.4, cable_kw={"width": 5.8})
    body = (
        squircle(SIZE)
        + f'<path d="{frame}" fill="none" stroke="{EV}" stroke-width="12" '
          f'stroke-linecap="round" stroke-dasharray="18 21" opacity="0.2"/>'
        + f'<path d="{frame}" fill="none" stroke="{EV}" stroke-width="7.5" '
          f'stroke-linecap="round" stroke-dasharray="18 21"/>'
        + tiled(SCENE_BB, (84, 174, 344, 190), inner)
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 12 - Nachtladen: Lichtkegel der Saeule auf dem Boden, Auto im gruenen
#      Schein. Erzaehlt die Situation (geladen wird meist nachts in der
#      eigenen Garage) statt nur die Gegenstaende aufzuzaehlen.
# ===========================================================================
def v12_nacht():
    defs = (TILE_CLIP + bg_gradient("bg", "#121C2C", "#070B12")
            + radial_glow("g12", EV, 0.55)
            + f'<linearGradient id="cone" x1="0" y1="0" x2="0" y2="1">'
              f'<stop offset="0" stop-color="{EV}" stop-opacity="0.42"/>'
              f'<stop offset="0.55" stop-color="{EV}" stop-opacity="0.14"/>'
              f'<stop offset="1" stop-color="{EV}" stop-opacity="0"/></linearGradient>')
    inner = scene(
        ground_op=0.75,
        station_kw={"body": "#69738B"},
        car_kw={"body": "#79839B", "rim": "#5D6780", "glow_edge": True},
        cable_kw={"width": 6.6, "glow": 0.3, "glow_mul": 4},
        extra_back=(f'<path d="M 6 38 L 36 38 L 168 101 L -96 101 Z" fill="url(#cone)"/>'
                    f'<path d="M 6 38 L -96 101 M 36 38 L 168 101" stroke="{EV}" '
                    f'stroke-width="1.4" opacity="0.18"/>'
                    f'<ellipse cx="20" cy="44" rx="52" ry="44" fill="url(#g12)" opacity="0.55"/>'))
    stars = "".join(
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{GREY_LIGHT}" opacity="{o}"/>'
        for x, y, r, o in [(112, 92, 3.2, 0.45), (392, 78, 2.6, 0.38),
                           (330, 132, 2, 0.24), (438, 168, 2.2, 0.26)])
    return svg(SIZE, SIZE, squircle(SIZE) + stars
               + tiled(SCENE_BB, (44, 118, 424, 296), inner),
               defs, "Lademonitor")


# ===========================================================================
# 13 - Sticker: dicke dunkle Kontur um jedes Element, Auto zusaetzlich
#      gestaucht. Der comic-hafteste Entwurf der Reihe - und der einzige,
#      der sich auch auf HELLEM Grund haelt, weil dort die Kontur die
#      Trennung uebernimmt, die sonst der dunkle Hintergrund leistet.
# ===========================================================================
def v13_sticker():
    defs = TILE_CLIP + bg_gradient("bg", "#1C2638", "#0B111B")
    o, ow = "#141C2A", 7
    # Kontur: dieselben Formen einmal breit dunkel darunter. Die Raeder
    # brauchen ihre eigene, sonst haengen sie ohne Kante an der Karosserie.
    car_outline = (f'<g fill="{o}" stroke="{o}" stroke-width="{ow}" stroke-linejoin="round">'
                   f'<circle cx="{WHEEL_FRONT[0]}" cy="{WHEEL_FRONT[1]}" r="20"/>'
                   f'<circle cx="{WHEEL_REAR[0]}" cy="{WHEEL_REAR[1]}" r="20"/>'
                   f'<path d="{CAR_BODY}"/></g>')
    inner = (
        shadow(CAR_X + 92, 92, op=0.5) + shadow(20, 26, op=0.45)
        + f'<g stroke="{o}" stroke-width="{ow}" stroke-linejoin="round">'
          f'<rect x="22" y="57" width="20" height="18" rx="8" fill="{o}"/>'
          f'<rect x="3" y="16" width="34" height="80" rx="13" fill="{o}"/></g>'
        + f'<g>{station(body=GREY, panel="#1B2334")}</g>'
        + f'<g transform="translate({CAR_X},3) scale(1.03,0.93)">'
          f'{car_outline}{car_side(glow_edge=True)}</g>'
        + cable(CABLE_SAG, width=8.5, dash="12 11", glow=0.2)
    )
    return svg(SIZE, SIZE, squircle(SIZE)
               + tiled(SCENE_BB, (38, 108, 436, 296), inner),
               defs, "Lademonitor")


# ===========================================================================
# 14 - Sechseck: Wabenform statt Kachel. Liest sich technisch/modular und
#      hebt sich in einer Reihe runder App-Icons deutlich ab.
# ===========================================================================
def v14_hexagon():
    import math
    defs = TILE_CLIP + bg_gradient() + radial_glow("g14", EV, 0.12)
    c = SIZE / 2

    def hexd(R):
        pts = [(c + R * math.cos(math.radians(a)), c + R * math.sin(math.radians(a)))
               for a in range(-90, 271, 60)]
        return "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"

    inner = scene(ground_x=(-20, 250), ground_op=0.4, cable_kw={"width": 5.6})
    body = (
        f'<path d="{hexd(248)}" fill="url(#bg)"/>'
        f'<path d="{hexd(248)}" fill="url(#g14)"/>'
        # Innensechseck konzentrisch ueber den Radius, NICHT ueber scale() -
        # eine Skalierung um den Ursprung haette es aus der Mitte geschoben.
        f'<path d="{hexd(212)}" fill="none" stroke="{EV}" stroke-width="8.5" '
        f'stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="21 23"/>'
        f'<g transform="{fit(SCENE_BB, 116, 196, 280, 164)}">{inner}</g>'
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 15 - Typ-2-Stecker: die Aussenform ist der Mennekes-Stecker (Kreis mit
#      abgeflachter Oberseite), die Kontaktbohrungen sitzen im Rand, das
#      Auto im Negativraum darin. Das eigenstaendigste Zeichen der Reihe -
#      es braucht einen Moment, ist danach aber unverwechselbar.
# ===========================================================================
def v15_typ2():
    import math
    defs = TILE_CLIP + bg_gradient() + radial_glow("g15", EV, 0.13)
    c, r = SIZE / 2 - 12, 206
    fx, fy = r * 0.7926, r * 0.61
    plug = (f"M {c - fx:.1f} {c - fy:.1f} L {c + fx:.1f} {c - fy:.1f} "
            f"A {r} {r} 0 1 1 {c - fx:.1f} {c - fy:.1f} Z")
    # Kontakte am Rand: zwei kleine Signalkontakte oben, vier grosse im
    # oberen Kranz. Weiter unten waere Platz, aber dort steht das Auto.
    pins = (f'<circle cx="{c-52}" cy="{c-98}" r="14" fill="{BG_DEEP}" opacity="0.9"/>'
            f'<circle cx="{c+52}" cy="{c-98}" r="14" fill="{BG_DEEP}" opacity="0.9"/>')
    for a in (-158, -122, -58, -22):
        px = c + (r - 42) * math.cos(math.radians(a))
        py = c + (r - 42) * math.sin(math.radians(a))
        pins += f'<circle cx="{px:.1f}" cy="{py:.1f}" r="20" fill="{BG_DEEP}" opacity="0.9"/>'
    car_bb = (-6, 20, 192, 104)
    inner = ground(-2, -14, 202, op=0.3) + shadow(94, 84) + car_side()
    body = (
        squircle(SIZE)
        + f'<path d="{plug}" fill="{GREY_MID}" opacity="0.16"/>'
        + f'<path d="{plug}" fill="url(#g15)"/>'
        + f'<path d="{plug}" fill="none" stroke="{GREY}" stroke-width="16"/>'
        + pins
        + tiled(car_bb, (100, 206, 312, 168), inner)
        + cable(f"M {c} {c + r - 4} C {c} {SIZE - 16}, {c + 96} {SIZE - 8}, "
                f"{c + 168} {SIZE - 44}", width=9.5, dash="15 16", glow=0.16)
    )
    return svg(SIZE, SIZE, body, defs, "Lademonitor")


# ===========================================================================
# 16 - Wandbox: die Anordnung, die die Kachelhoehe ueberhaupt erst nutzt.
#      Nebeneinander gestellt begrenzt IMMER die Breite (Szene 2,6:1 in einer
#      1:1-Kachel), das Auto kommt damit nie ueber rund 70 % der Breite
#      hinaus. Uebereinander gestapelt - Wandbox oben, Kabel als Fall dazwi-
#      schen, Fahrzeug unten - sind es 78 %, und die Kachel steht voll.
# ===========================================================================
def v16_wandbox():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g16", EV, 0.18)
    s = 2.18
    car_x, car_y = 48, 200          # Standlinie landet damit auf y=418
    px = car_x + CHARGE_PORT[0] * s
    py = car_y + CHARGE_PORT[1] * s
    wb_w, wb_h = 92, 112
    wb_x, wb_y = px - wb_w / 2, 74  # Wandbox mittig ueber der Ladeklappe
    inner = (
        f'<ellipse cx="{px:.0f}" cy="{(wb_y + wb_h + 40):.0f}" rx="150" ry="150" '
        f'fill="url(#g16)" opacity="0.5"/>'
        + f'<g transform="translate(-70,{car_y}) scale({s})">'
          f'{ground(x0=-40, x1=340, op=0.55)}</g>'
        + f'<ellipse cx="{px + 150:.0f}" cy="416" rx="200" ry="8" fill="{BG_DEEP}" opacity="0.4"/>'
        + f'<g transform="translate({wb_x:.1f},{wb_y})">{wallbox(w=wb_w, h=wb_h)}</g>'
        + f'<g transform="translate({car_x},{car_y}) scale({s})">{car_side(glow_edge=True)}</g>'
        # Das Kabel haengt links am Fahrzeug vorbei durch und kommt von unten
        # in die Ladeklappe - ein senkrechter Fall waere ein Strich, keine
        # Leitung.
        + cable(f"M {px:.0f} {wb_y + wb_h} C {px:.0f} 224, 66 238, 66 272 "
                f"C 66 298, 90 314, {px:.0f} {py:.0f}", width=9, dash="15 14")
    )
    return svg(SIZE, SIZE, squircle(SIZE) + f'<g clip-path="url(#tile)">{inner}</g>',
               defs, "Lademonitor")


# ===========================================================================
# 17 - Angeschnitten: dieselbe Aufstellung wie 01, aber die Saeule laeuft aus
#      der Kachel heraus. Sie ist das Nebenmotiv - wird sie angeschnitten
#      statt vollstaendig gezeigt, bleibt die gesamte Breite fuers Fahrzeug:
#      84 % der Kachel statt 70 %. Wer sie ganz sehen will, nimmt 01.
# ===========================================================================
def v17_angeschnitten():
    defs = TILE_CLIP + bg_gradient() + radial_glow("g17", EV, 0.2)
    s = 2.3
    car_x, car_y = 78, 172          # Standlinie auf y=402
    st_x, st_y = -20, 172
    ox = st_x + STATION_OUTLET[0] * s
    oy = st_y + STATION_OUTLET[1] * s
    px = car_x + CHARGE_PORT[0] * s
    py = car_y + CHARGE_PORT[1] * s
    inner = (
        f'<ellipse cx="40" cy="308" rx="140" ry="126" fill="url(#g17)" opacity="0.5"/>'
        + f'<g transform="translate(0,{car_y}) scale({s})">'
          f'{ground(x0=-30, x1=230, op=0.55)}</g>'
        + f'<ellipse cx="{px + 170:.0f}" cy="400" rx="210" ry="8" fill="{BG_DEEP}" opacity="0.4"/>'
        + f'<g transform="translate({st_x},{st_y}) scale({s})">{station(base=False)}</g>'
        + f'<g transform="translate({car_x},{car_y}) scale({s})">{car_side(glow_edge=True)}</g>'
        + cable(f"M {ox:.0f} {oy:.0f} C 84 366, 108 374, 124 344 "
                f"C 132 328, 134 306, {px:.0f} {py:.0f}", width=9, dash="15 14")
    )
    return svg(SIZE, SIZE, squircle(SIZE) + f'<g clip-path="url(#tile)">{inner}</g>',
               defs, "Lademonitor")


VARIANTS = [
    ("01-klassik", "Klassik", v01_klassik),
    ("02-kreis", "Kreisemblem", v02_kreis),
    ("03-puls", "Puls / Monitor", v03_puls),
    ("04-ladering", "Ladering 72 %", v04_ladering),
    ("05-blitz", "Blitzkabel", v05_blitz),
    ("06-monogramm", "Monogramm L", v06_monogramm),
    ("07-front", "Crystal Face", v07_front),
    ("08-statistik", "Statistik", v08_statistik),
    ("09-monoline", "Monoline / Favicon", v09_monoline),
    ("10-lockup", "Lockup mit Wortmarke", v10_lockup),
    ("11-rahmen", "Kabelrahmen", v11_rahmen),
    ("12-nacht", "Nachtladen", v12_nacht),
    ("13-sticker", "Sticker", v13_sticker),
    ("14-hexagon", "Sechseck", v14_hexagon),
    ("15-typ2", "Typ-2-Stecker", v15_typ2),
    ("16-wandbox", "Wandbox (hoch)", v16_wandbox),
    ("17-angeschnitten", "Angeschnitten", v17_angeschnitten),
]


# Kurzbeschreibung je Variante - wandert in die Uebersichtsseite (preview.html)
# und in die README, damit die Begruendung nicht nur im Code steht.
NOTES = {
    "01-klassik": "Die Grundaufstellung: Säule links, Fahrzeug rechts, durchhängendes Kabel. "
                  "Ruhigster Entwurf der Reihe und der sicherste Kandidat fürs App-Icon.",
    "02-kreis": "Rundes Emblem, das Kabel läuft zusätzlich als gestrichelter Ring außen herum. "
                "Die Lücke oben lässt ihn wie aufgerolltes Kabel wirken statt wie einen Rahmen.",
    "03-puls": "Das Kabel wird zur Messkurve. Die App heißt Lade-MONITOR, nicht Ladezähler – "
               "das ist der einzige Entwurf, der die Auswertung selbst zum Motiv macht.",
    "04-ladering": "Ring als Ladestand (72 %). Dieselbe Metapher wie im Dashboard; sagt nicht "
                   "nur „Auto + Strom“, sondern „wie voll ist es“. Trägt auch bei 32 px noch.",
    "05-blitz": "Das Kabel knickt als Blitz. Nimmt das Motiv des bisherigen Icons auf, bindet "
                "es aber ins Kabel ein, statt es als zweites Zeichen danebenzustellen.",
    "06-monogramm": "Das Kabel schreibt das L von Lademonitor. Nutzt eine Wandbox statt der "
                    "Standsäule – sonst müsste das Kabel erst hochlaufen und der Buchstabe wäre hin.",
    "07-front": "Frontansicht mit Crystal Face. Der senkrecht gerippte Grill ist das eigentliche "
                "Erkennungszeichen des Enyaq und in der Seitenansicht gar nicht darstellbar.",
    "08-statistik": "Monatsbalken als Untergrund, Kabel als Verlaufskurve. Die App ist in erster "
                    "Linie eine Auswertung – das sagt das Zeichen mit.",
    "09-monoline": "Auf drei Zeichen reduziert und deutlich größer im Rahmen. Der einzige "
                   "Entwurf, der bei 24–32 px noch als Fahrzeug lesbar bleibt – Favicon-Kandidat.",
    "10-lockup": "Zeichen plus Wortmarke für Kopfleiste, README und Store-Zeile. Der Schriftzug "
                 "liegt noch als &lt;text&gt; vor und muss für die finale Marke in Pfade "
                 "umgewandelt werden.",
    "11-rahmen": "Das Kabel umschließt die Szene als gestrichelter Rahmen. Sehr eigenständige "
                 "Außenform – das Zeichen ist schon am Bildrand erkennbar.",
    "12-nacht": "Lichtkegel der Säule, Fahrzeug im grünen Schein. Erzählt die Situation "
                "(geladen wird meist nachts in der eigenen Garage), statt Gegenstände aufzuzählen.",
    "13-sticker": "Dicke dunkle Kontur um jedes Element, Fahrzeug zusätzlich gestaucht. Der "
                  "comic-hafteste Entwurf – und der einzige, der sich auch auf HELLEM Grund hält.",
    "14-hexagon": "Wabenform statt Kachel. Liest sich technisch/modular und hebt sich in einer "
                  "Reihe runder App-Icons deutlich ab.",
    "16-wandbox": "Die Anordnung, die die Kachelhöhe nutzt: Wandbox oben, Kabel als Fall, "
                  "Fahrzeug unten. Nebeneinander begrenzt immer die Breite – gestapelt "
                  "belegt das Auto 78 % der Kachel statt 70 %.",
    "17-angeschnitten": "Aufstellung wie 01, aber die Säule läuft aus der Kachel heraus. Sie ist "
                        "das Nebenmotiv – angeschnitten bleibt die ganze Breite fürs Fahrzeug: "
                        "84 % statt 70 %.",
    "15-typ2": "Außenform ist der Typ-2-Stecker (Kreis mit abgeflachter Oberseite), Kontakte im "
               "Rand, Fahrzeug im Negativraum. Braucht einen Moment, ist danach unverwechselbar.",
}


def preview_html():
    """Uebersichtsseite zum Vergleichen - oeffnet sich ohne Server per Doppelklick."""
    cards = []
    for name, label, _ in VARIANTS:
        with open(os.path.join(OUT, name + ".svg")) as fh:
            markup = fh.read()
        wide = " wide" if name == "10-lockup" else ""
        cards.append(
            f'<article class="card{wide}">'
            f'<div class="art">{markup}</div>'
            f'<h2>{name.split("-", 1)[0]} &middot; {label}</h2>'
            f'<p>{NOTES.get(name, "")}</p>'
            f'<div class="sizes">'
            + "".join(f'<span style="width:{p}px;height:{p}px">{markup}</span>' for p in (48, 32, 24))
            + f'<em>48 / 32 / 24 px</em></div></article>')
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lademonitor – Logo-Entwürfe</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:{BG_BASE}; color:{GREY_LIGHT}; margin:0; padding:32px 20px 64px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
header {{ max-width:1100px; margin:0 auto 32px; }}
h1 {{ font-size:26px; margin:0 0 8px; }}
header p {{ color:{GREY_MID}; margin:0; max-width:64ch; line-height:1.55; font-size:14px; }}
.grid {{ max-width:1100px; margin:0 auto; display:grid;
  grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:22px; }}
.card {{ background:{BG_SOFT}; border:1px solid #2A3348; border-radius:14px; padding:14px; }}
.card.wide {{ grid-column:1/-1; }}
.art svg {{ width:100%; height:auto; border-radius:10px; display:block; }}
h2 {{ font-size:14px; margin:12px 0 6px; color:{GREY_LIGHT}; }}
.card p {{ font-size:12.5px; line-height:1.55; color:{GREY_MID}; margin:0 0 12px; }}
.sizes {{ display:flex; align-items:center; gap:10px; }}
.sizes span {{ display:inline-block; flex:none; }}
.sizes svg {{ width:100%; height:100%; display:block; }}
.sizes em {{ font-size:10.5px; color:{GREY_MID}; font-style:normal; margin-left:4px; }}
@media (max-width:720px) {{ body {{ padding:20px 14px 48px; }} .grid {{ gap:16px; }} }}
</style></head><body>
<header><h1>Lademonitor – Logo-Entwürfe</h1>
<p>15 Varianten aus denselben Bausteinen (parts.py): gestauchte Enyaq-Silhouette,
Ladesaeule, gestricheltes Kabel in Electric Green. Neu erzeugen mit
<code>python3 design/logo/build.py</code>. Die drei kleinen Abbildungen unter jeder
Karte zeigen, was bei 48, 32 und 24&nbsp;px übrig bleibt.</p></header>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, label, fn in VARIANTS:
        write(name, fn())
        print(f"  {name:<14} {label}")
    with open(os.path.join(OUT, "preview.html"), "w") as fh:
        fh.write(preview_html())
    print(f"\n{len(VARIANTS)} Varianten + preview.html -> {OUT}")


if __name__ == "__main__":
    main()
