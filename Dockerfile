# syntax=docker/dockerfile:1.7
# MRAZ Master - all runtime deps live inside Docker. No Python/uv on host.

ARG PYTHON_VERSION=3.13

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALL_SHIM=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (better layer caching)
ARG INSTALL_DEV=0
COPY pyproject.toml uv.lock* ./
RUN if [ "$INSTALL_DEV" = "1" ]; then \
      uv sync --extra dev --frozen 2>/dev/null || uv sync --extra dev ; \
    else \
      uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev ; \
    fi

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Runtime system deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY . /app/

RUN mkdir -p /app/staticfiles

EXPOSE 8000

# Entrypoint: run migrations then start uvicorn
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && uvicorn mraz.asgi:application --host 0.0.0.0 --port 8000"]