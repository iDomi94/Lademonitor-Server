# Einzelcontainer-Build fuer Unraid: Postgres + FastAPI-Backend im selben
# Container (bewusst KEIN s6-overlay/Supervisor - fuer eine Ein-Nutzer-
# Heimnetz-App reicht ein simples Entrypoint-Skript, siehe entrypoint.sh).
# Fuer lokale Entwicklung mit getrennten Containern bleibt docker-compose.yml
# weiterhin nutzbar.
FROM postgres:16

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fest verdrahtete Zugangsdaten fuer die interne, nicht nach aussen
# exponierte Postgres-Instanz (nur via localhost im selben Container
# erreichbar) - kein Unraid-Template-Parameter noetig
ENV POSTGRES_USER=charging \
    POSTGRES_PASSWORD=charging \
    POSTGRES_DB=charging \
    DATABASE_URL=postgresql+psycopg://charging:charging@localhost:5432/charging \
    PATH="/venv/bin:${PATH}"

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
