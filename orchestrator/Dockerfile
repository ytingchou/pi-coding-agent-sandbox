FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/server \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/server/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    && useradd --no-log-init -u 1000 -m app \
    && mkdir /state && chown app:app /state
COPY orchestrator /app/orchestrator
COPY scripts /app/scripts
USER app
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
