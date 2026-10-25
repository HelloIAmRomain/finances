#!/usr/bin/env bash
# `docker compose` sur l'image actuellement déployée (épinglée par condensat dans state/).
# Exemples (root) : deploy/compose.sh ps ; deploy/compose.sh restart web worker ;
#   deploy/compose.sh run --rm web python -m app.cli create-admin
set -euo pipefail
dir="$(cd "$(dirname "$0")/.." && pwd)"
[[ -s "$dir/state/current-image" ]] || {
	echo "aucune image déployée (state/current-image absent) : lancer deploy/update.sh" >&2
	exit 1
}
FINANCES_IMAGE="$(cat "$dir/state/current-image")"
export FINANCES_IMAGE
exec docker compose -f "$dir/deploy/compose.yml" "$@"
