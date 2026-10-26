#!/usr/bin/env bash
# Déploiement nocturne avec retour arrière automatique (DT §5, T0.11).
#
# 1. Vise l'image du dernier commit de main (ghcr.io/…:sha-<court>), publiée seulement si la CI
#    est verte. Si elle n'existe pas, rien n'est changé et l'échec est signalé.
# 2. Met à jour le dépôt des secrets s'il est à part, puis les fichiers de déploiement
#    (fast-forward), et se relance.
# 3. Si l'image a changé : arrêt de web et worker, sauvegarde restic, instantané local,
#    démarrage (migrations comprises), attente de /health.
# 4. En cas d'échec : base et image précédentes restaurées, alerte.
#
# Usage : update.sh [--no-git] [IMAGE]    (IMAGE : référence explicite, pour tests ou manuel)
# Lancé chaque nuit par finances-update.timer (root).
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

dir="${FINANCES_DIR:-/srv/finances}"
state="${FINANCES_STATE_DIR:-$dir/state}"
export FINANCES_DATA_DIR="${FINANCES_DATA_DIR:-$dir/data}"
data="$FINANCES_DATA_DIR"
repository="${FINANCES_IMAGE_REPOSITORY:-ghcr.io/helloiamromain/finances-app}"
web_port="${FINANCES_WEB_PORT:-8000}"
health_timeout="${FINANCES_HEALTH_TIMEOUT:-120}"
pre_deploy_backup="${FINANCES_PRE_DEPLOY_BACKUP:-systemctl start finances-backup.service}"
render_secrets="${FINANCES_RENDER_SECRETS:-$dir/deploy/render-secrets.sh $(encrypted_secrets_file "$dir")}"
compose=(docker compose -f "$dir/deploy/compose.yml")

use_git=1
if [[ "${1:-}" == --no-git ]]; then
	use_git=0
	shift
fi
[[ "${FINANCES_NO_GIT:-0}" == 1 ]] && use_git=0
explicit_image="${1:-}"

fail() {
	echo "update: $*" >&2
	healthcheck_ping healthchecks_update_url fail
	exit 1
}

# Référence épinglée : condensat du registre, ou identifiant local (images construites sur place).
pinned_ref() {
	docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$1"
}

# Exécute une commande dans un conteneur jetable de l'image, avec le volume de données,
# sous l'utilisateur applicatif : les fichiers de la base gardent le bon propriétaire.
in_container() {
	local image="$1"
	shift
	docker run --rm --network none --user 10001:10001 -v "$data:/data" --entrypoint "$1" "$image" "${@:2}"
}

# Attend /health ; échoue tôt si les migrations de l'image donnée ont échoué.
healthy() {
	local deadline=$((SECONDS + health_timeout)) migrate_status
	local -a ps=(env FINANCES_IMAGE="$1" "${compose[@]}" ps -a)
	while ((SECONDS < deadline)); do
		migrate_status="$("${ps[@]}" --format '{{.ExitCode}}' migrate 2>/dev/null || true)"
		if [[ -n "$migrate_status" && "$migrate_status" != 0 ]] &&
			[[ "$("${ps[@]}" --format '{{.State}}' migrate)" == exited ]]; then
			echo "update: migrations en échec (code $migrate_status)" >&2
			return 1
		fi
		curl -fsS -m 5 -o /dev/null "http://127.0.0.1:$web_port/health" 2>/dev/null && return 0
		sleep 2
	done
	return 1
}

# Écrit l'entrée standard dans un fichier d'état, de façon atomique (pas de fichier tronqué
# si la machine s'arrête pendant l'écriture).
write_state() {
	cat >"$state/$1.partial"
	mv "$state/$1.partial" "$state/$1"
}

# Retire les images du dépôt autres que celles données (actuelle et précédente) : chaque nuit
# ajoute une étiquette sha-…, que `docker image prune` ne retire jamais.
remove_old_images() {
	local keep id
	keep="$(docker image inspect --format '{{.Id}}' "$@" 2>/dev/null || true)"
	[[ -n "$keep" ]] || return 0
	while read -r id; do
		grep -qxF "$id" <<<"$keep" || docker image rm -f "$id" >/dev/null 2>&1 || true
	done < <(docker image ls --quiet --no-trunc "$repository" | sort -u)
	docker image prune -f >/dev/null
}

# Une seule mise à jour à la fois (minuterie et lancement manuel). Le verrou suit la relance.
mkdir -p "$state"
exec 9>"$state/update.lock"
flock -n 9 || {
	echo "update: une mise à jour est déjà en cours" >&2
	exit 1
}

# Bascule vers les secrets de l'hôte séparés : quand les fichiers ont été mis à jour par une
# version antérieure de ce script, elle a relancé celui-ci avec --no-git sans redéchiffrer, et
# la sauvegarde avant déploiement ne trouvait pas ses secrets (16/10/2026). Rattrapage ici ; les
# conteneurs ne lisent leurs secrets qu'au démarrage.
if [[ ! -d "$FINANCES_HOST_SECRETS_DIR" ]]; then
	bash -c "$render_secrets" >/dev/null ||
		fail "secrets de l'hôte absents et non déchiffrables : voir journalctl -u finances-update"
	log "secrets de l'hôte déchiffrés dans $FINANCES_HOST_SECRETS_DIR"
fi

# --- 1 et 2 : image visée et fichiers de déploiement --------------------------------------
# Dépôt des secrets à part (dépôt de code public) : mis à jour d'abord ; les conteneurs lisent les
# nouveaux secrets à leur prochain démarrage.
if ((use_git)) && [[ -z "$explicit_image" ]]; then
	synced="$("$dir/deploy/sync-secrets.sh")" || fail "dépôt des secrets non mis à jour : rien n'est changé"
	if [[ -n "$synced" ]]; then
		bash -c "$render_secrets" >/dev/null ||
			fail "secrets non déchiffrés après leur mise à jour : voir journalctl -u finances-update"
		echo "$synced"
	fi
fi
if [[ -n "$explicit_image" ]]; then
	target="$explicit_image"
else
	if ((use_git)); then
		git -C "$dir" fetch --quiet origin main
		commit="$(git -C "$dir" rev-parse --short=7 origin/main)"
	else
		commit="$(git -C "$dir" rev-parse --short=7 HEAD)"
	fi
	target="$repository:sha-$commit"
fi

if [[ "${FINANCES_NO_PULL:-0}" != 1 ]]; then
	docker pull --quiet "$target" >/dev/null ||
		fail "image $target indisponible (CI rouge ou en cours ?) : rien n'est changé"
fi

if ((use_git)) && [[ -z "$explicit_image" ]] &&
	[[ "$(git -C "$dir" rev-parse HEAD)" != "$(git -C "$dir" rev-parse origin/main)" ]]; then
	git -C "$dir" merge --quiet --ff-only origin/main
	# Secrets redéchiffrés avec les nouveaux fichiers : une nouvelle répartition entre application
	# et hôte (render-secrets.sh) s'applique sans intervention.
	bash -c "$render_secrets" >/dev/null ||
		fail "secrets non déchiffrés après la mise à jour des fichiers : voir journalctl -u finances-update"
	log "fichiers de déploiement mis à jour ($commit), relance"
	exec "$dir/deploy/update.sh" --no-git
fi

new="$(pinned_ref "$target")"
current="$(cat "$state/current-image" 2>/dev/null || true)"

# Fichier compose avec lequel tourne la version actuelle : le retour arrière le réutilise, car
# celui de main peut exiger ce que l'ancienne image n'a pas (service, variable, commande).
running_compose=("${compose[@]}")
if [[ -f "$state/current-compose.yml" ]]; then
	running_compose=(docker compose --project-directory "$dir/deploy" -f "$state/current-compose.yml")
fi

if [[ "$new" == "$current" ]]; then
	FINANCES_IMAGE="$current" "${compose[@]}" up -d --remove-orphans --quiet-pull
	write_state current-compose.yml <"$dir/deploy/compose.yml"
	log "déjà à jour ($current)"
	healthcheck_ping healthchecks_update_url
	exit 0
fi

# --- 3 : déploiement ---------------------------------------------------------------------
log "déploiement de $new (actuel : ${current:-aucun})"
if [[ -n "$current" ]]; then
	FINANCES_IMAGE="$current" "${compose[@]}" stop web worker
fi

has_database=0
[[ -e "$data/finances.db" ]] && has_database=1
if ((has_database)); then
	if ! bash -c "$pre_deploy_backup"; then
		if [[ -n "$current" ]]; then
			FINANCES_IMAGE="$current" "${running_compose[@]}" up -d
			healthy "$current" || echo "update: la version actuelle ne redémarre pas" >&2
		fi
		fail "sauvegarde avant déploiement en échec : déploiement annulé"
	fi
	in_container "${current:-$new}" python -c '
import os, sqlite3
source = sqlite3.connect("/data/finances.db")
target = sqlite3.connect("/data/pre-deploy.db.partial")
source.backup(target)
target.close()
source.close()
os.replace("/data/pre-deploy.db.partial", "/data/pre-deploy.db")
'
fi

if FINANCES_IMAGE="$new" "${compose[@]}" up -d --remove-orphans && healthy "$new"; then
	[[ -n "$current" ]] && echo "$current" | write_state previous-image
	echo "$new" | write_state current-image
	write_state current-compose.yml <"$dir/deploy/compose.yml"
	((has_database)) && in_container "$new" rm -f /data/pre-deploy.db
	remove_old_images "$new" "$current"
	log "déploiement réussi : $new"
	healthcheck_ping healthchecks_update_url
	exit 0
fi

# --- 4 : retour arrière ------------------------------------------------------------------
echo "update: échec du déploiement de $new" >&2
FINANCES_IMAGE="$new" "${compose[@]}" logs --no-color --tail 50 >&2 || true
FINANCES_IMAGE="$new" "${compose[@]}" stop web worker || true
[[ -n "$current" ]] || fail "premier déploiement en échec : aucune version précédente"

if ((has_database)); then
	failed="echec-$(date -u +%Y%m%dT%H%M%SZ)"
	# shellcheck disable=SC2016 # script évalué dans le conteneur ($1 = nom du répertoire)
	in_container "$current" sh -c '
set -eu
mkdir "/data/$1"
for file in /data/finances.db /data/finances.db-wal /data/finances.db-shm; do
	if [ -e "$file" ]; then mv "$file" "/data/$1/"; fi
done
mv /data/pre-deploy.db /data/finances.db
' sh "$failed"
	log "base restaurée depuis l'instantané ; base en échec conservée dans $data/$failed"
fi

FINANCES_IMAGE="$current" "${running_compose[@]}" up -d --remove-orphans
if healthy "$current"; then
	fail "déploiement de $new en échec : retour à $current réussi"
fi
echo "update: le retour arrière a aussi échoué" >&2
healthcheck_ping healthchecks_update_url fail
exit 2
