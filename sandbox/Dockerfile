FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim-bookworm AS runtime
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap ca-certificates procps git ripgrep \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/opt/server \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    && mkdir /sessions /state && chmod 711 /sessions && chmod 700 /state
# Export only sandbox application libraries, with hashes, from the same uv lock.
# The supervisor remains isolated in /opt/server.
ARG PYTHON_PACKAGE_INSTALLER=uv
RUN mkdir /opt/python-runtime \
    && uv export --locked --only-group sandbox --no-emit-project -o /opt/python-runtime/requirements.txt \
    && case "$PYTHON_PACKAGE_INSTALLER" in \
      uv) uv pip install --system --no-cache --require-hashes -r /opt/python-runtime/requirements.txt ;; \
      pip) python3.12 -m pip install --no-cache-dir --require-hashes -r /opt/python-runtime/requirements.txt ;; \
      *) echo 'PYTHON_PACKAGE_INSTALLER must be pip or uv' >&2; exit 1 ;; \
    esac && python3.12 -m pip check
COPY sandbox/python_inventory.py /opt/python-runtime/inventory.py
RUN python3.12 /opt/python-runtime/inventory.py --build /opt/python-runtime
WORKDIR /opt/pi
COPY sandbox/package*.json ./
RUN npm ci --omit=dev && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi
COPY sandbox/resources /opt/pi-resources
WORKDIR /app
COPY sandbox /app/sandbox
# Root is required by the supervisor to create session UIDs and drop privileges.
# Each Pi process still runs as its own non-root UID inside Bubblewrap.
CMD ["/opt/server/bin/uvicorn", "sandbox.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

FROM runtime AS test
RUN uv sync --locked --no-dev --group test --no-install-project
COPY orchestrator /app/orchestrator
COPY tests /app/tests
COPY scripts /app/scripts
COPY reports /app/reports

# Default builds never include pytest or test fixtures.
FROM runtime AS final
