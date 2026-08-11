#!/usr/bin/env bash
set -e

# Persistentes Datenverzeichnis - liegt auf dem einzigen Unraid-Pfad-Mapping
# (Container-Pfad /config), damit Ladevorgaenge etc. einen Container-Rebuild
# ueberleben. Eigenes Unterverzeichnis statt /config direkt, damit Postgres
# nicht ueber unerwartete Dateien im PGDATA-Wurzelverzeichnis stolpert, falls
# spaeter mal weitere Config-Dateien unter /config dazukommen.
export PGDATA=/config/postgres
mkdir -p "$PGDATA"

# Delegiert Initialisierung (initdb beim allerersten Start) und den
# root->postgres-Nutzerwechsel an das offizielle, gut getestete
# Postgres-Entrypoint-Skript - laeuft im Hintergrund, damit uvicorn im
# selben Container als zweiter Prozess starten kann.
docker-entrypoint.sh postgres &
PG_PID=$!

echo "Warte auf Postgres..."
until pg_isready -h localhost -U "$POSTGRES_USER" >/dev/null 2>&1; do
  sleep 1
done
echo "Postgres bereit, starte Backend."

/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &
APP_PID=$!

# Stirbt einer der beiden Prozesse, soll der ganze Container stoppen - Docker/
# Unraid startet ihn dank restart-Policy dann komplett neu, statt dass die
# App weiterlaeuft, obwohl z.B. Postgres abgestuerzt ist.
wait -n "$PG_PID" "$APP_PID"
exit $?
