FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 1000 -m app && mkdir /state && chown app:app /state
COPY orchestrator /app/orchestrator
COPY tests /app/tests
COPY scripts /app/scripts
USER app
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
