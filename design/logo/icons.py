#!/usr/bin/env python3
"""Erzeugt den App-Icon-Satz aus Logo-Variante 17 ("Angeschnitten").

    python3 design/logo/icons.py                       # Server + Unraid
    python3 design/logo/icons.py --ios ../lademonitor-app   # zusaetzlich iOS

Schreibt direkt nach backend/app/static/ und legt zusaetzlich das icon.png im
Repo-Wurzelverzeichnis an, auf das die Unraid-CA-Vorlagen verweisen.

Mit --ios wird auch der Asset-Katalog der iOS-App bedient (eigenes Repo,
iDomi94/Lademonitor-App). Der Generator liegt bewusst hier und nicht dort:
die Zeichnung selbst hat nur einen Ursprung, und zwei Kopien davon wuerden
frueher oder spaeter auseinanderlaufen.

Warum drei Fassungen derselben Variante
---------------------------------------
* **voll** (512/192/256 px) - die komplette Zeichnung mit Binnenzeichnung.
* **klein** (48/32/16 px) - dieselbe Anordnung ohne Tuerfugen, Griffe,
  Leuchten und Displaydetails, dafuer mit kraeftigerem Kabel. Der Test bei
  32 px zeigte die volle Zeichnung als Fleck; reduziert bleibt die
  Silhouette lesbar. Beide entstehen aus derselben Funktion (build._v17),
  koennen also nicht auseinanderlaufen.
* **quadratisch** (180 px, apple-touch-icon) - randlos ohne Rundung. iOS legt
  seine eigene Maske darueber; eine schon gerundete Vorlage ergibt doppelt
  gerundete Ecken, und transparente Ecken fuellt iOS mit Schwarz oder Weiss.

Dazu kommt das Zeichen fuer die Navigationsleiste (logo-mark.svg): dasselbe
Motiv quer, ohne Kachel und ohne Hintergrund. Es bleibt SVG, wird also nicht
gerastert - in der Leiste soll es mit der Schriftgroesse mitskalieren und auf
jedem Bildschirm scharf sein.

Rasterung laeuft ueber das im Container vorhandene headless Chromium (kein
cairosvg/rsvg im Bild). Gerendert wird einmal gross und dann mit LANCZOS
heruntergerechnet - direkt bei 16 px zu rendern ergibt sichtbar rauhere
Kanten als das Herunterrechnen von 1024 px.
"""
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STATIC = os.path.join(ROOT, "backend", "app", "static")
CHROME = "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell"
RENDER_PX = 1024


def rasterise(svg_markup, px=RENDER_PX):
    """SVG -> RGBA-Bild, Ecken transparent."""
    with tempfile.TemporaryDirectory() as tmp:
        html = os.path.join(tmp, "r.html")
        png = os.path.join(tmp, "r.png")
        with open(html, "w") as fh:
            fh.write('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;'
                     'padding:0;background:transparent}svg{display:block;width:%dpx;'
                     'height:%dpx}</style>%s' % (px, px, svg_markup))
        subprocess.run(
            [CHROME, "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             "--default-background-color=00000000", "--force-device-scale-factor=1",
             f"--screenshot={png}", f"--window-size={px},{px}", "file://" + html],
            capture_output=True, check=True)
        return Image.open(png).convert("RGBA").copy()


def write_png(img, size, path):
    img.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
    print(f"  {os.path.relpath(path, ROOT):<40}{size}x{size}")


def write_ico(img, sizes, path):
    """ICO von Hand zusammensetzen (PNG-Nutzlast je Eintrag).

    Pillows eigener ICO-Schreiber skaliert selbst; hier wird jede Groesse
    einzeln mit LANCZOS gerechnet, was bei 16 px sichtbar sauberer ist.
    PNG-in-ICO ist seit Windows Vista und in allen Browsern gaengig.
    """
    payloads = []
    for s in sizes:
        buf = os.path.join(tempfile.gettempdir(), f"_ico{s}.png")
        img.resize((s, s), Image.LANCZOS).save(buf, "PNG", optimize=True)
        with open(buf, "rb") as fh:
            payloads.append(fh.read())
        os.unlink(buf)
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
    print(f"  {os.path.relpath(path, ROOT):<40}{'/'.join(str(s) for s in sizes)}")


def write_ios(voll, quadrat, ios_root):
    """Asset-Katalog der iOS-App bedienen.

    Zwei Dinge, die hier anders laufen als im Web:

    * **AppIcon ohne Alphakanal.** App Store Connect weist Icons mit
      Transparenz zurueck. Deshalb die quadratische Fassung und zusaetzlich
      convert("RGB") - die Vorlage ist zwar ohnehin deckend, traegt als RGBA
      aber trotzdem einen Alphakanal mit sich.
    * **PNG statt SVG fuer das Zeichen im Erststart-Screen.** Asset-Kataloge
      koennen SVG, aber Xcodes SVG-Import unterstuetzt `stroke-dasharray`
      nicht zuverlaessig - und genau daraus besteht das Kabel. Ein stiller
      Ausfall genau des Elements, das die Marke ausmacht, ist das Risiko
      nicht wert; die Kachel wird ohnehin nur in einer Groesse gezeigt.

    Das Zeichen ist bewusst die gerundete KACHEL, nicht das quere Zeichen aus
    der Web-Kopfleiste: der Erststart-Screen folgt dem Hell/Dunkel-Modus des
    Systems, und die graue Silhouette mit gruenem Kabel ist fuer dunklen
    Grund gezeichnet. Die Kachel bringt ihren eigenen Grund mit und sitzt
    dadurch in beiden Modi richtig.
    """
    assets = os.path.join(ios_root, "Lademonitor", "Assets.xcassets")
    if not os.path.isdir(assets):
        raise SystemExit(f"Asset-Katalog nicht gefunden: {assets}")

    icon = os.path.join(assets, "AppIcon.appiconset", "AppIcon-1024.png")
    quadrat.convert("RGB").resize((1024, 1024), Image.LANCZOS).save(
        icon, "PNG", optimize=True)
    print(f"  {os.path.relpath(icon, ios_root):<52}1024x1024, ohne Alpha")

    mark = os.path.join(assets, "LogoMark.imageset")
    os.makedirs(mark, exist_ok=True)
    voll.resize((512, 512), Image.LANCZOS).save(
        os.path.join(mark, "LogoMark.png"), "PNG", optimize=True)
    # Ohne "scale" behandelt Xcode das Bild als Single Scale - dieselbe Form,
    # die der AppIcon-Eintrag schon benutzt.
    with open(os.path.join(mark, "Contents.json"), "w") as fh:
        fh.write('{\n  "images" : [\n    {\n      "filename" : "LogoMark.png",'
                 '\n      "idiom" : "universal"\n    }\n  ],\n  "info" : {'
                 '\n    "author" : "xcode",\n    "version" : 1\n  }\n}\n')
    print(f"  {os.path.relpath(mark, ios_root):<52}512x512 + Contents.json")


def main():
    ios_root = None
    if "--ios" in sys.argv:
        ios_root = os.path.abspath(sys.argv[sys.argv.index("--ios") + 1])

    print("rastere Variante 17 ...")
    voll = rasterise(build.v17_angeschnitten())
    klein = rasterise(build.v17_klein())
    quadrat = rasterise(build.v17_quadratisch())

    write_png(voll, 512, os.path.join(STATIC, "icon-512.png"))
    write_png(voll, 192, os.path.join(STATIC, "icon-192.png"))
    write_png(quadrat, 180, os.path.join(STATIC, "apple-touch-icon.png"))
    write_png(klein, 32, os.path.join(STATIC, "favicon-32.png"))
    write_png(klein, 16, os.path.join(STATIC, "favicon-16.png"))
    write_ico(klein, (16, 32, 48), os.path.join(STATIC, "favicon.ico"))
    # Auf dieses icon.png zeigen templates/lademonitor-server.xml und
    # ca_profile.xml - die Datei fehlte im Repo, das CA-Eintragsbild war
    # dadurch kaputt.
    write_png(voll, 256, os.path.join(ROOT, "icon.png"))

    mark = os.path.join(STATIC, "logo-mark.svg")
    with open(mark, "w") as fh:
        fh.write(build.lockup_mark())
    print(f"  {os.path.relpath(mark, ROOT):<40}SVG, quer")
    if ios_root:
        print(f"\niOS-App ({ios_root}):")
        write_ios(voll, quadrat, ios_root)

    print("\nfertig - Version in backend/app/changelog.py nicht vergessen.")


if __name__ == "__main__":
    main()
