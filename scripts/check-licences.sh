#!/usr/bin/env bash
# Licences des dépendances livrées (hors outils de développement) : refus des licences
# contaminantes, incompatibles avec une offre commerciale fermée (CDC QUA-05, LEG-06).
set -euo pipefail
cd "$(dirname "$0")/.."
mapfile -t packages < <(
	uv export --frozen --no-dev --no-emit-project --format requirements-txt |
		grep -oE '^[A-Za-z0-9_.-]+==' | tr -d '='
)
uv run pip-licenses --packages "${packages[@]}" --format=markdown --order=license \
	--partial-match --fail-on "GPL;AGPL;SSPL;EUPL;Commons Clause"
echo "licences : ${#packages[@]} paquets vérifiés, aucune licence contaminante"
