#!/usr/bin/env bash
# Tests de bout en bout dans l'image officielle Playwright (Chromium et ses bibliothèques),
# sans rien installer sur le poste. Usage : scripts/e2e.sh [options pytest]
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
version="$(cd "$root" && uv run python -c 'import importlib.metadata as m; print(m.version("playwright"))')"
exec docker run --rm --ipc=host -v "$root:/src" -w /src \
	-e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache -e PYTHONDONTWRITEBYTECODE=1 \
	"mcr.microsoft.com/playwright/python:v${version}-noble" \
	bash -c 'pip install --quiet --break-system-packages uv && uv sync --frozen --quiet && uv run pytest tests/e2e --e2e -p no:cacheprovider "$@"' bash "$@"
