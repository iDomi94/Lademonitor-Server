# Lademonitor

**Language:** English | [Deutsch](README.md)

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

Self-hosted charging session tracking app for an EV (Škoda Enyaq via
MySkoda/Home Assistant integration), inspired by Spritmonitor. Runs
entirely self-hosted, no cloud service.

Companion iOS app (SwiftUI, pure REST client against this backend):
[Lademonitor-App](https://github.com/iDomi94/Lademonitor-App)

## Features

- **Home Assistant**: [Lademonitor-HA](https://github.com/iDomi94/Lademonitor-HA)
  (HACS integration) pulls statistics sensors (cost/consumption/km) into HA
  and pushes automatically detected charging sessions via a service instead
  of manual `rest_command` token handling – considerably simpler than the
  raw API route below. For HA OS/Supervised there is additionally
  [Lademonitor-HA-Addon](https://github.com/iDomi94/Lademonitor-HA-Addon),
  to run the server directly as an add-on instead of separately (e.g. on
  Unraid).
- Record charging sessions manually or have them pushed automatically via a
  Home Assistant automation (`POST /api/sessions/auto`)
- **MyŠkoda Public API**: alternatively, the server can detect charging
  sessions itself by querying the official Škoda API directly – no Home
  Assistant needed at all. Just enter an API key and VIN, see
  [Automatic charge detection](#automatic-charge-detection-via-the-myškoda-public-api)
- Consumption calculation (kWh/100km) per charging session via a
  prioritized fallback chain (full-charge intervals as the gold standard,
  SoC correction, naive calculation, estimate) – details in `CLAUDE.md`
- Create charging locations via address search (Nominatim), or automatically
  derive a display label for GPS coordinates via offline reverse geocoding
- Spritmonitor CSV import with preview and duplicate detection
- Full backup export/import as a ZIP (all vehicles, providers, charging
  locations, charging sessions), designed for re-setting up the server
- Multi-user capable: registration, login, each user has their own, fully
  isolated dataset
- Server-rendered web UI (no separate frontend build needed), pure REST API
  for Home Assistant and the
  [iOS app](https://github.com/iDomi94/Lademonitor-App) (separate repo)

## Getting started / installation

Four deployment options – depending on where/how the server should run.
Backend, web UI, and Postgres are bundled into the same container in all
four variants (except Docker Compose, see below) – no separate DB container
needed.

### Unraid (Community Applications)

The easiest option for Unraid users: search for "Lademonitor" in **Apps**
and install it. The template (`templates/lademonitor-server.xml`) is
pre-configured (port, `/config` path for the data) and points to the
public GHCR image, which is built automatically via GitHub Actions (see
`.github/workflows/docker-publish.yml`). Via the tag selector in the
container edit dialog, you can also choose `beta` (the latest `main`
build) instead of `latest` (stable).

### Home Assistant Add-on

For HA OS/Supervised, to run the server directly inside Home Assistant
instead of separately: **Settings → Add-ons → Add-on Store → ⋮ →
Repositories**, enter `https://github.com/iDomi94/Lademonitor-HA-Addon`
there, then install "Lademonitor" and start it. Data lives in the
Supervisor `/data` path and is therefore part of regular HA backups.
Details in the [add-on repo](https://github.com/iDomi94/Lademonitor-HA-Addon).

Access (Ingress sidebar button or direct port `8111`) only works locally
within the home network – for external access (e.g. the iOS app on the
go) you additionally need VPN access to your network or your own reverse
proxy/Nginx vhost on port 8111 (details on that in the
[add-on repo](https://github.com/iDomi94/Lademonitor-HA-Addon#netzwerkzugriff-lokal-vs-extern)).

### Docker (single container)

For any other Docker host (Synology, VPS, Unraid without CA, …), a single
container instead of a stack:
```bash
docker run -d -p 8111:8000 -v /pfad/zu/daten:/config ghcr.io/idomi94/lademonitor-server:latest
```
`/config` contains the entire Postgres database – be sure to include it in
your backup plan.

### Docker Compose (local development/testing)

The only option with two separate containers (backend + Postgres), meant
for development/testing rather than production:
```bash
docker compose up -d --build
```
The web UI is then reachable at `http://<host-ip>:8111`. Credentials for
the internal Postgres instance can optionally be overridden via `.env`
(see `.env.example`) – the Compose-internal default is not sensitive, since
Postgres is not exposed externally.

---

API documentation (Swagger) is available at `/docs`.

## First steps

1. Register an account (`/register`) – the first ever registered user
   automatically becomes admin.
2. Under **Settings**, create your vehicle (external ID e.g. `enyaq` – this
   gets reused 1:1 in the Home Assistant automation).
3. Optional: create charging providers with known prices and known
   charging locations.
4. Under **Import**, upload a Spritmonitor CSV, or record entries manually
   under **Charging sessions**.
5. View cost/consumption statistics on the **Dashboard**.

## Home Assistant integration

> **Recommendation:** Instead of the raw API below, use the
> [Lademonitor-HA](https://github.com/iDomi94/Lademonitor-HA) integration
> (via HACS) – it handles login/token management and ships ready-made
> services for the automatic push, including a complete example automation
> in its README.

`POST /api/sessions/auto` accepts automatically detected charging sessions
(expects an `Authorization: Bearer <token>` header – get a token via
`POST /api/auth/login`, see `CLAUDE.md` for an example package with a
`rest_command` automation). Direct API access naturally remains available
for other integrations/scripts as well. Expected JSON:

```json
{
  "vehicle_external_id": "enyaq",
  "external_session_id": "20260809_1432",
  "start_time": "2026-08-09T14:05:00",
  "end_time": "2026-08-09T14:32:00",
  "soc_start": 42,
  "soc_end": 89,
  "odometer_km": 48213,
  "latitude": 52.5200,
  "longitude": 13.4050
}
```

The `external_session_id` prevents duplicates in case the automation fires
more than once. Sessions submitted this way are marked with
`source: automatic` and `needs_review: true`, and if the coordinates match
a known charging location, they are automatically pre-filled with
provider/price.

## Automatic charge detection via the MyŠkoda Public API

A second source of automatically captured charging sessions, independent
of Home Assistant – intended for anyone who doesn't run Home Assistant.
The server queries the
[official MyŠkoda Public API](https://public.api.connect.skoda-auto.cz/docs)
itself, detects the start and end of a charging session from the state
transitions, and creates it just like an HA push, with `source: automatic`
and `needs_review: true`.

**Setup** (Settings → Automatic charge detection):

1. Generate an API key in the MyŠkoda app: [go.skoda.eu/api-keys](https://go.skoda.eu/api-keys)
   (open the link on your phone). The key is bound to the vehicles selected
   when it was generated and expires after some time.
2. Select the vehicle, enter the API key and VIN (17 characters), save.
3. "Test connection" shows the parsed API response – this also serves as
   a check of which fields your own vehicle actually provides.
4. Enable "Poll automatically".

**Good to know:**

- **Only enable one of the two routes per vehicle.** The HA push and this
  detection know nothing about each other and would create the same
  charging session twice.
- **Rate limit: 20 requests per hour per API key**, shared across all
  vehicles and all clients. The default intervals (20 min while idle, 5 min
  during a charging session) stay comfortably below that. If the
  [MySkoda Public API integration in Home Assistant](https://github.com/iDomi94/homeassistant-myskoda-public-api)
  runs in parallel with the same key, both share this quota – in that case
  it's better to generate a second key.
- **Accuracy:** The API has no concept of a push and no energy meter. The
  start of charging is only noticed on the next poll, so the initial SoC
  tends to be too high and the energy tends to be underestimated – for AC
  charging this is negligible, for DC fast charging it's noticeable. Every
  detected session therefore gets a note with the before/after values.
- **Debug log:** Found in the same section; logs every poll and every
  detection decision, including the full raw responses, and can be
  downloaded as JSON. Since the API's response behavior during an actual
  charging session (still in beta) is barely documented, this is the way
  to clarify a session that wasn't detected after the fact.
- The API key is stored in plaintext in the database (like all credentials
  in this app), but is deliberately **not** exported in the backup ZIP.

For now this is only configurable via the web UI; the endpoints
(`/api/myskoda/...`) are nevertheless a regular part of the REST API and
documented at `/docs`.

## Backup

Three levels:
- **Built-in export/import** (Settings → Data backup): a ZIP with CSVs of
  all of your own data, ideal for reinstalling the server.
- **Automatic WebDAV backup** (Settings → Automatic WebDAV backup):
  uploads exactly this ZIP at a configurable frequency
  (daily/weekly/monthly) automatically to a WebDAV server (e.g. Nextcloud)
  and cleans up backups it uploaded itself after the configured retention
  period. Configurable per user, runs in the background without a
  cron/external scheduler.
- **Raw volume backup**: with Compose the DB lives under
  `./data/postgres`, with the single container under the mounted `/config`
  path – for Unraid a regular folder backup job (e.g. CA Backup/Restore
  Appdata) is sufficient.

## Security note

Auth is built in (registration/login, per-user isolated data), but there
is deliberately **no rate limiting yet** on login/registration – if exposed
publicly via a reverse proxy, make sure to use a strong password. Details
and further known limitations in `CLAUDE.md`.

## Unreachable after an update?

If the server becomes briefly unreachable via your own reverse proxy (e.g.
Nginx in front of Unraid) after a rebuild/update and only comes back after
clearing the browser cache, there are typically two possible causes:

- **Browser cache**: The browser is still showing a page loaded before the
  update (old HTML/JS) that no longer matches the new server. Since v0.9.1
  the HTML pages send `Cache-Control: no-store`, so browsers should no
  longer cache them at all – a hard refresh (Ctrl/Cmd+Shift+R) should no
  longer be needed to trigger this since then.
- **Reverse proxy/Docker network**: A container rebuild usually assigns a
  new internal Docker IP. If your Nginx resolves the target server once and
  doesn't re-resolve the IP afterward (common with a static `upstream`
  entry without dynamic DNS resolution), it points to the old, no-longer-
  existing IP after a rebuild – if reloading/restarting the reverse proxy
  container helps rather than clearing the browser cache, this is the more
  likely cause.

A `secure` cookie issue (see `CLAUDE.md`) is **not** the cause on an
already-running HTTPS setup – that only affects direct HTTP access without
your own reverse proxy.

## Further documentation

`CLAUDE.md` contains the full architecture documentation (data model, auth
design, consumption calculation, deployment details) – the starting point
for anything not covered here.
