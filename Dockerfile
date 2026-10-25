# syntax=docker/dockerfile:1
# Image unique pour les services web et worker (linux/arm64 pour le Pi, linux/amd64 pour la CI).

FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.12-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

FROM python:3.12-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS runtime
# Correctifs de sécurité Debian appliqués à chaque construction : l'image de base épinglée peut
# avoir du retard (Trivy bloque la CI sur toute vulnérabilité haute corrigible, CDC QA-03).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 finances \
    && useradd --system --uid 10001 --gid finances --no-create-home --shell /usr/sbin/nologin finances
COPY --from=build /opt/venv /opt/venv
COPY --from=build /src/app /srv/finances/app
COPY --from=build /src/alembic.ini /srv/finances/alembic.ini
COPY --from=build /src/migrations /srv/finances/migrations
WORKDIR /srv/finances
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER finances
EXPOSE 8000
# Le port n'est publié que sur 127.0.0.1 par compose (inv. 7).
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status != 200)"]
CMD ["uvicorn", "--factory", "app.web.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
