#!/usr/bin/env bash
# Avance rapide du dépôt privé des secrets chiffrés sur origin/main, quand il existe
# (FINANCES_SECRETS_REPO_DIR, /srv/finances-secrets par défaut ; le dépôt de code est public).
# Affiche « secrets mis à jour (<commit>) » si de nouveaux secrets sont arrivés, rien sinon.
# Usage : sync-secrets.sh    (appelé par update.sh avant la mise à jour du code)
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

repo="$FINANCES_SECRETS_REPO_DIR"
[[ -d "$repo/.git" ]] || exit 0
git -C "$repo" fetch --quiet origin main || die "dépôt des secrets injoignable : $repo"
[[ "$(git -C "$repo" rev-parse HEAD)" == "$(git -C "$repo" rev-parse origin/main)" ]] && exit 0
git -C "$repo" merge --quiet --ff-only origin/main ||
	die "dépôt des secrets non mis à jour (historique divergent) : $repo"
log "secrets mis à jour ($(git -C "$repo" rev-parse --short=7 HEAD))"
