"""Auslieferung der Seiten und der PWA-Dateien.

Klein, aber genau an den Stellen, an denen diese App schon einmal gestolpert
ist: absolute statt relativer Pfade (bricht unter dem Home-Assistant-Ingress)
und Dateien, die es gar nicht gibt.
"""

from conftest import register


def test_manifest_is_served_without_login(client):
    """Der Browser holt das Manifest teils ohne die Cookies der Seite, und die
    Login-Seite selbst soll installierbar sein."""
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")


def test_manifest_uses_only_relative_paths(client):
    """Absolute Pfade wuerden unter dem Ingress-Unterpfad auf die HA-Wurzel
    zeigen - derselbe Fehler, der 2026-08-25 den Login dort zerlegt hat.
    `start_url`/`scope` werden ausserdem relativ zur Adresse DES MANIFESTS
    aufgeloest, weshalb es nicht unter /static/ liegen darf."""
    body = client.get("/manifest.webmanifest").json()

    assert body["start_url"] == "."
    assert body["scope"] == "."
    for icon in body["icons"]:
        assert not icon["src"].startswith("/")


def test_manifest_has_the_icon_sizes_browsers_require(client):
    body = client.get("/manifest.webmanifest").json()

    sizes = {icon["sizes"] for icon in body["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in body["icons"])


def test_manifest_icons_actually_exist(client):
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        assert client.get("/" + icon["src"]).status_code == 200, icon["src"]


def test_service_worker_is_served_from_the_root(client):
    """Aus /static/ heraus haette er nur dort Geltung und die App bliebe
    nicht installierbar."""
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "./"
    assert "fetch" in response.text


def test_map_page_requires_login(client):
    response = client.get("/map", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "login"


def test_map_page_renders_for_a_logged_in_user(client):
    register(client)

    response = client.get("/map")

    assert response.status_code == 200
    assert "leaflet" in response.text.lower()


def test_map_loads_leaflet_locally_and_not_from_a_cdn(client):
    """Die Karte darf ihre Bibliothek nicht aus dem Netz ziehen: seit der
    Chart.js-Entfernung kommt die Oberflaeche ohne externe Skriptquellen aus
    (ein Client hatte keinen CDN-Zugriff). Die Kacheln von openstreetmap.org
    sind die bewusste, in /privacy dokumentierte Ausnahme - die laedt der
    Browser, nicht der Server."""
    register(client)

    body = client.get("/map").text

    assert "static/vendor/leaflet/leaflet.js" in body
    for cdn in ("unpkg.com", "cdnjs.", "jsdelivr"):
        assert cdn not in body
    # Und die Datei muss auch wirklich ausgeliefert werden, nicht nur verlinkt sein.
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.css").status_code == 200


def test_html_pages_are_not_cached(client):
    register(client)

    assert client.get("/sessions").headers["cache-control"] == "no-store"
