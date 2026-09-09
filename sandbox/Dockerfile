FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap ca-certificates procps git ripgrep \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/pi
COPY sandbox/package*.json ./
RUN npm ci --omit=dev && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/server && /opt/server/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && mkdir /sessions /state && chmod 711 /sessions && chmod 700 /state
COPY sandbox/resources /opt/pi-resources
WORKDIR /app
COPY sandbox /app/sandbox
COPY tests /app/tests
CMD ["/opt/server/bin/uvicorn", "sandbox.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
