/*
 * Service Worker - bewusst OHNE jeden Cache.
 *
 * Er existiert einzig, weil Browser eine Web-App nur dann als installierbar
 * behandeln, wenn neben dem Manifest auch ein Service Worker mit einem
 * fetch-Handler registriert ist. Inhaltlich reicht dieser Anhaltspunkt aus.
 *
 * Warum hier NICHT gecacht wird: die ganze App ist bewusst auf "nichts
 * Veraltetes ausliefern" gebaut - HTML mit `no-store`, statische Dateien mit
 * `no-cache` plus `?v=<version>` (siehe main.py::_no_cache_html). Ein
 * cachender Service Worker waere die dritte Cache-Ebene und gleichzeitig die
 * einzige, die ein Nutzer nicht mit einem Neuladen loswird - genau der Fehler,
 * der 2026-09-06 schon einmal eine halbe Stunde Fehlersuche gekostet hat
 * ("neues HTML, alte style.css"). Offline-Faehigkeit ist ausserdem kein Ziel:
 * die Seiten werden serverseitig gerendert, ohne Server gibt es nichts zu
 * zeigen - dafuer gibt es die beiden Apps mit lokalem Speicher.
 */

self.addEventListener("install", () => {
  // Sofort uebernehmen statt auf das Schliessen aller alten Tabs zu warten -
  // ohne Cache gibt es keinen Grund, eine alte Fassung weiterlaufen zu lassen.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // Reines Durchreichen. Der Handler MUSS existieren (siehe oben), darf die
  // Anfrage aber nicht veraendern.
  event.respondWith(fetch(event.request));
});
