#!/usr/bin/env python3
"""Erzeugt alle Icons und Logos aus dem Motiv „Säule lädt Auto“.

    python3 design/logo/icons.py                              # Server + Unraid
    python3 design/logo/icons.py --ios ../Lademonitor-App     # + iOS
    python3 design/logo/icons.py --android ../Lademonitor-Android
    python3 design/logo/icons.py --ha ../Lademonitor-HA       # HACS-Integration
    python3 design/logo/icons.py --addon ../Lademonitor-HA-Addon

Die Optionen lassen sich kombinieren. Quelle sind die SVGs in
`design/logo/saeule-auto/` (eine Ladesäule mit Ring-Display, per Kabel mit
einem Auto von oben verbunden; entworfen in Lademonitor-App PR #53). Der
Generator liegt bewusst nur hier: die Zeichnung hat einen Ursprung, die
übrigen Repos werden von hier aus bedient (Android ruft ihn über
`tools/generate_icons.py` auf).

Fassungen
---------
* **Kachel** (`light.svg` mit gerundeten Ecken, Radius 22,4 %) - Favicons,
  PWA-Icons, Unraid-icon.png, Erststart-Screen der Apps, HA-Icons. Das Motiv
  ist einfach genug, dass es ohne eigene Kleinfassung bis 16 px lesbar bleibt.
* **quadratisch** (randlos) - Apple-Touch-Icon, iOS-AppIcon, Play-Store-Bild.
  iOS/Play legen ihre eigene Maske darüber; eine schon gerundete Vorlage
  ergäbe doppelt gerundete Ecken.
* **maskierbar** - Motiv verkleinert auf den Verlaufsgrund, so dass es ganz in
  der Sicherheitszone liegt (PWA `purpose: maskable`: Kreis mit 80 %
  Durchmesser; Android-Adaptive-Icon: Kreis mit 66 von 108 dp).
* **monochrom** - Android-13-Themensymbole: Motiv einfarbig, die dunklen
  Binnenflächen (Display, Fenster, Blitz) ausgespart.
* **quer** (`logo-mark.svg`) - Navigationsleiste der Weboberfläche: nur das
  Motiv ohne Grund, auf seine Ausdehnung beschnitten, als SVG.
* **Wort-Bild-Marke** (Kachel + „Lademonitor“) - HA-Integration und Add-on.
  Schrift in Mitteltönen, damit sie auf hellem UND dunklem HA-Thema lesbar ist.

Rasterung über headless Chromium (Playwright-Installation, kein cairosvg im
Bild): einmal groß rendern, dann mit LANCZOS herunterrechnen.
"""
import argparse
import glob
import io
import os
import re
import struct
import subprocess
import tempfile

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "saeule-auto")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATIC = os.path.join(ROOT, "backend", "app", "static")
RENDER_PX = 1024

# Verlauf des Hintergrunds (light.svg, oben -> unten).
BG_TOP = "#3A4A6B"
BG_BOTTOM = "#1F2A44"
# Akzent (Kabel, Ring, Batterie) - auch --brand der Weboberfläche.
ACCENT = "#5EEAD4"
# Ausdehnung des Motivs im 100er-Raster der SVGs (gemessen, mit etwas Luft).
MOTIF_BOX = (10.5, 24.5, 79.0, 51.0)   # x, y, Breite, Höhe
CORNER = 0.2237                         # Rundung der Kachel (Anteil der Kante)


def find_chrome():
    env = os.environ.get("LADEMONITOR_CHROME")
    if env:
        return env
    for pattern in ("/opt/pw-browsers/chromium_headless_shell-*/chrome-linux*/headless_shell",
                    "/opt/pw-browsers/chromium-*/chrome-linux*/chrome"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    raise SystemExit("Chromium nicht gefunden - Pfad über LADEMONITOR_CHROME setzen.")


CHROME = None


def svg(name):
    with open(os.path.join(SRC, name + ".svg")) as fh:
        return fh.read()


def render_html(body, w, h):
    """HTML -> RGBA-Bild (transparenter Grund)."""
    global CHROME
    CHROME = CHROME or find_chrome()
    with tempfile.TemporaryDirectory() as tmp:
        html = os.path.join(tmp, "r.html")
        png = os.path.join(tmp, "r.png")
        with open(html, "w") as fh:
            fh.write('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;'
                     'padding:0;background:transparent;overflow:hidden}'
                     'svg{display:block}</style>' + body)
        args = [CHROME, "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                "--default-background-color=00000000", "--force-device-scale-factor=1",
                f"--screenshot={png}", f"--window-size={w},{h}", "file://" + html]
        if not CHROME.endswith("headless_shell"):
            args.insert(1, "--headless")
        subprocess.run(args, capture_output=True, check=True)
        return Image.open(png).convert("RGBA").copy()


def rasterise(markup, px=RENDER_PX):
    markup = re.sub(r'width="\d+" height="\d+"', f'width="{px}" height="{px}"', markup, count=1)
    return render_html(markup, px, px)


def rounded(img):
    w, h = img.size
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1),
                                           radius=int(w * 4 * CORNER), fill=255)
    out = img.copy()
    out.putalpha(mask.resize((w, h), Image.LANCZOS))
    return out


def motif_on_background(scale, background=True):
    """Motiv mittig, auf `scale` der Kantenlänge verkleinert (Sicherheitszone)."""
    fg = svg("foreground")
    inner = fg[fg.index(">", fg.index("<svg")) + 1:fg.rindex("</svg>")]
    cx = MOTIF_BOX[0] + MOTIF_BOX[2] / 2
    cy = MOTIF_BOX[1] + MOTIF_BOX[3] / 2
    bg = ""
    if background:
        bg = ('<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
              f'<stop offset="0" stop-color="{BG_TOP}"/><stop offset="1" '
              f'stop-color="{BG_BOTTOM}"/></linearGradient></defs>'
              '<rect width="100" height="100" fill="url(#bg)"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
            f'width="1024" height="1024">{bg}<g transform="translate(50 50) '
            f'scale({scale}) translate({-cx} {-cy})">{inner}</g></svg>')


def monochrome(markup):
    """Einfarbige Fassung: helle Flächen und Akzent weiß, Binnenflächen frei."""
    body = re.sub(r"<style>.*?</style>", "", markup, flags=re.S)
    body = body[body.index(">", body.index("<svg")) + 1:body.rindex("</svg>")]
    style = ("<style>*{stroke-width:0} .p,.a{fill:#fff;stroke:#fff} "
             ".k{fill:#000;stroke:#000} .s{fill:rgba(255,255,255,.35);"
             "stroke:rgba(255,255,255,.35)}</style>")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
            'width="1024" height="1024">' + style +
            '<mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="100" '
            f'height="100">{body}</mask><rect width="100" height="100" '
            'fill="#fff" mask="url(#m)"/></svg>')


def mark_svg():
    """Querformat für die Navigationsleiste: Motiv ohne Grund, beschnitten."""
    fg = svg("foreground")
    x, y, w, h = MOTIF_BOX
    fg = re.sub(r'viewBox="[^"]*" width="\d+" height="\d+"',
                f'viewBox="{x} {y} {w} {h}" width="{round(w * 3)}" '
                f'height="{round(h * 3)}" role="img"', fg, count=1)
    return fg.replace(">", "><title>Lademonitor</title>", 1)


def lockup(tile, height, light_text=False):
    """Kachel + Schriftzug, transparent, auf Inhalt beschnitten."""
    buf = io.BytesIO()
    tile.save(buf, "PNG")
    import base64
    data = base64.b64encode(buf.getvalue()).decode()
    big = height * 4
    lade = "#E9EEF7" if light_text else "#6B7A99"
    body = (f'<div style="display:inline-flex;align-items:center;gap:{big // 5}px;'
            f'padding:2px;font-family:Inter,-apple-system,\'Segoe UI\',Roboto,'
            f'sans-serif;font-weight:700;font-size:{int(big * 0.5)}px;'
            f'letter-spacing:-0.01em;white-space:nowrap">'
            f'<img src="data:image/png;base64,{data}" style="height:{big}px;'
            f'width:{big}px"><span style="color:{lade}">Lade<span '
            f'style="color:#14B8A6">monitor</span></span></div>')
    img = render_html(body, big * 6, big + 8)
    img = img.crop(img.getchannel("A").getbbox())
    w = round(img.width * height / img.height)
    return img.resize((w, height), Image.LANCZOS)


def save(img, path, size=None, rgb=False, base=ROOT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if size:
        img = img.resize(size if isinstance(size, tuple) else (size, size), Image.LANCZOS)
    if rgb:
        img = img.convert("RGB")
    img.save(path, "PNG", optimize=True)
    print(f"  {os.path.relpath(path, base):<60}{img.width}x{img.height}"
          + (", ohne Alpha" if rgb else ""))


def write_ico(img, sizes, path):
    """ICO von Hand (PNG-Nutzlast je Eintrag, jede Größe einzeln mit LANCZOS)."""
    payloads = []
    for s in sizes:
        buf = io.BytesIO()
        img.resize((s, s), Image.LANCZOS).save(buf, "PNG", optimize=True)
        payloads.append(buf.getvalue())
    offset = 6 + 16 * len(sizes)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    entries, blob = b"", b""
    for s, data in zip(sizes, payloads):
        entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blob += data
    with open(path, "wb") as fh:
        fh.write(head + entries + blob)
    print(f"  {os.path.relpath(path, ROOT):<60}{'/'.join(map(str, sizes))}")


class Set:
    """Einmal gerasterte Grundbilder, von allen Zielen geteilt."""

    def __init__(self):
        print("rastere Motiv ...")
        self.square = rasterise(svg("light"))
        self.dark = rasterise(svg("dark"))
        self.tinted = rasterise(svg("tinted"))
        self.tile = rounded(self.square)
        self.maskable = rasterise(motif_on_background(0.84))


def write_server(s):
    print("\nServer:")
    save(s.tile, os.path.join(STATIC, "icon-512.png"), 512)
    save(s.tile, os.path.join(STATIC, "icon-192.png"), 192)
    save(s.maskable, os.path.join(STATIC, "icon-maskable-512.png"), 512, rgb=True)
    save(s.square, os.path.join(STATIC, "apple-touch-icon.png"), 180, rgb=True)
    save(s.tile, os.path.join(STATIC, "favicon-32.png"), 32)
    save(s.tile, os.path.join(STATIC, "favicon-16.png"), 16)
    write_ico(s.tile, (16, 32, 48), os.path.join(STATIC, "favicon.ico"))
    # Unraid-CA (templates/lademonitor-server.xml, ca_profile.xml)
    save(s.tile, os.path.join(ROOT, "icon.png"), 256)
    with open(os.path.join(STATIC, "logo-mark.svg"), "w") as fh:
        fh.write(mark_svg())
    print(f"  {'backend/app/static/logo-mark.svg':<60}SVG, quer")


def write_ios(s, ios_root):
    print(f"\niOS ({ios_root}):")
    assets = os.path.join(ios_root, "Lademonitor", "Assets.xcassets")
    if not os.path.isdir(assets):
        raise SystemExit(f"Asset-Katalog nicht gefunden: {assets}")
    icon = os.path.join(assets, "AppIcon.appiconset")
    # App Store Connect weist Icons mit Alphakanal zurück.
    save(s.square, os.path.join(icon, "AppIcon-1024.png"), 1024, rgb=True, base=ios_root)
    save(s.dark, os.path.join(icon, "AppIcon-1024-dark.png"), 1024, rgb=True, base=ios_root)
    save(s.tinted, os.path.join(icon, "AppIcon-1024-tinted.png"), 1024, rgb=True, base=ios_root)
    # Erststart-Screen: die Kachel bringt ihren Grund mit, sitzt also in Hell
    # und Dunkel richtig.
    save(s.tile, os.path.join(assets, "LogoMark.imageset", "LogoMark.png"), 512, base=ios_root)


ANDROID_DENSITIES = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def write_android(s, android_root):
    print(f"\nAndroid ({android_root}):")
    res = os.path.join(android_root, "app", "src", "main", "res")
    if not os.path.isdir(res):
        raise SystemExit(f"res-Verzeichnis nicht gefunden: {res}")
    # 108 dp Feld, sicher sichtbar ist ein Kreis mit 66 dp: Motiv auf 0,7.
    fg = rasterise(motif_on_background(0.7, background=False))
    mono = rasterise(monochrome(motif_on_background(0.7, background=False)))
    for d, px in ANDROID_DENSITIES.items():
        save(fg, os.path.join(res, f"mipmap-{d}", "ic_launcher_foreground.png"), px, base=android_root)
        save(mono, os.path.join(res, f"mipmap-{d}", "ic_launcher_monochrome.png"), px, base=android_root)
    path = os.path.join(res, "drawable", "ic_launcher_background.xml")
    with open(path, "w") as fh:
        fh.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!-- Erzeugt vom Logo-Generator des Server-Repos (design/logo/icons.py)\n'
            '     - nicht von Hand ändern. Derselbe Verlauf wie das App-Icon. -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    xmlns:aapt="http://schemas.android.com/aapt"\n'
            '    android:width="108dp"\n'
            '    android:height="108dp"\n'
            '    android:viewportWidth="108"\n'
            '    android:viewportHeight="108">\n'
            '    <path android:pathData="M0,0h108v108h-108z">\n'
            '        <aapt:attr name="android:fillColor">\n'
            '            <gradient\n'
            '                android:type="linear"\n'
            '                android:startX="54"\n'
            '                android:startY="0"\n'
            '                android:endX="54"\n'
            '                android:endY="108">\n'
            f'                <item android:offset="0" android:color="{BG_TOP}" />\n'
            f'                <item android:offset="1" android:color="{BG_BOTTOM}" />\n'
            '            </gradient>\n'
            '        </aapt:attr>\n'
            '    </path>\n'
            '</vector>\n')
    print(f"  {os.path.relpath(path, android_root):<60}Verlauf {BG_TOP} -> {BG_BOTTOM}")
    save(s.tile, os.path.join(res, "drawable-nodpi", "logo_mark.png"), 512, base=android_root)
    save(s.square, os.path.join(android_root, "app", "src", "main", "ic_launcher-playstore.png"),
         512, rgb=True, base=android_root)


def write_ha(s, ha_root):
    """Markenbilder der HACS-Integration (custom_components/<domain>/brand/)."""
    print(f"\nHome-Assistant-Integration ({ha_root}):")
    brand = os.path.join(ha_root, "custom_components", "lademonitor", "brand")
    save(s.tile, os.path.join(brand, "icon.png"), 256, base=ha_root)
    save(s.tile, os.path.join(brand, "icon@2x.png"), 512, base=ha_root)
    save(lockup(s.tile, 128), os.path.join(brand, "logo.png"), base=ha_root)
    save(lockup(s.tile, 256), os.path.join(brand, "logo@2x.png"), base=ha_root)
    save(lockup(s.tile, 128, light_text=True), os.path.join(brand, "dark_logo.png"), base=ha_root)
    save(lockup(s.tile, 256, light_text=True), os.path.join(brand, "dark_logo@2x.png"), base=ha_root)


def write_addon(s, addon_root):
    """icon.png (128 px, quadratisch) und logo.png (ca. 250x100) des Add-ons."""
    print(f"\nHome-Assistant-Add-on ({addon_root}):")
    folder = os.path.join(addon_root, "lademonitor")
    save(s.tile, os.path.join(folder, "icon.png"), 128, base=addon_root)
    logo = lockup(s.tile, 100)
    if logo.width > 400:
        logo = logo.resize((400, round(logo.height * 400 / logo.width)), Image.LANCZOS)
    save(logo, os.path.join(folder, "logo.png"), base=addon_root)


def main():
    ap = argparse.ArgumentParser(description="Icons aus design/logo/saeule-auto erzeugen")
    ap.add_argument("--ios", help="Checkout von Lademonitor-App")
    ap.add_argument("--android", help="Checkout von Lademonitor-Android")
    ap.add_argument("--ha", help="Checkout von Lademonitor-HA")
    ap.add_argument("--addon", help="Checkout von Lademonitor-HA-Addon")
    ap.add_argument("--no-server", action="store_true", help="Server-Icons nicht schreiben")
    args = ap.parse_args()

    s = Set()
    if not args.no_server:
        write_server(s)
    if args.ios:
        write_ios(s, os.path.abspath(args.ios))
    if args.android:
        write_android(s, os.path.abspath(args.android))
    if args.ha:
        write_ha(s, os.path.abspath(args.ha))
    if args.addon:
        write_addon(s, os.path.abspath(args.addon))
    if not args.no_server:
        print("\nfertig - Version in backend/app/changelog.py nicht vergessen.")


if __name__ == "__main__":
    main()
