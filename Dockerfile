FROM python:3.12.8-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build
RUN pip install uv==0.11.14

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.8-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 smartfill \
    && useradd --system --uid 10001 --gid smartfill --home-dir /nonexistent smartfill

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/alembic.ini /app/alembic.ini
COPY --from=builder /build/migrations /app/migrations

USER smartfill:smartfill
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"]

CMD ["uvicorn", "smartfill.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
