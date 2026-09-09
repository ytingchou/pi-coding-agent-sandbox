FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv bubblewrap ca-certificates procps git ripgrep \
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
