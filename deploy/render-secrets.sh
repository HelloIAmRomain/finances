#!/usr/bin/env bash
# Déchiffre deploy/secrets.enc.yaml vers un tmpfs, un fichier par clé de premier niveau
# (convention des secrets Docker, lus par app/config.py). Le clair n'est jamais écrit sur disque.
#
# Deux répertoires, pour qu'un conteneur compromis n'obtienne que ce dont l'application a besoin :
#   - FINANCES_SECRETS_DIR (application) : monté dans les conteneurs (deploy/compose.yml) ;
#   - FINANCES_HOST_SECRETS_DIR (hôte) : sauvegarde restic et B2, registre d'images, pings des
#     scripts de l'hôte ; lu par deploy/lib.sh et bootstrap.sh, jamais monté dans un conteneur.
# Toute clé absente de HOST_SECRETS va à l'application (la garde de démarrage la voit).
#
# Usage : render-secrets.sh [FICHIER_CHIFFRÉ]
# Variables :
#   SOPS_AGE_KEY_FILE          clé age de la machine (défaut /etc/finances/age/keys.txt)
#   FINANCES_SECRETS_DIR       secrets de l'application (défaut /run/finances/secrets, en tmpfs)
#   FINANCES_HOST_SECRETS_DIR  secrets de l'hôte (défaut /run/finances/host-secrets, en tmpfs)
#   FINANCES_SECRETS_OWNER     propriétaire des fichiers si lancé en root (défaut 10001:10001)
#   FINANCES_SECRETS_ALLOW_DISK=1  autorise une destination hors tmpfs (tests uniquement)
set -euo pipefail

src="${1:-/srv/finances/deploy/secrets.enc.yaml}"
app_dest="${FINANCES_SECRETS_DIR:-/run/finances/secrets}"
host_dest="${FINANCES_HOST_SECRETS_DIR:-/run/finances/host-secrets}"
owner="${FINANCES_SECRETS_OWNER:-10001:10001}"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-/etc/finances/age/keys.txt}"

HOST_SECRETS=(
	restic_repository restic_password b2_key_id b2_application_key ghcr_token
	healthchecks_backup_url healthchecks_maintenance_url healthchecks_restore_test_url
	healthchecks_update_url
)

die() {
	echo "render-secrets: $*" >&2
	exit 1
}

[[ -r "$src" ]] || die "fichier chiffré illisible : $src"
[[ -r "$SOPS_AGE_KEY_FILE" ]] || die "clé age illisible : $SOPS_AGE_KEY_FILE"
command -v sops >/dev/null || die "sops introuvable"

# Clés de premier niveau : en clair dans un fichier sops (seules les valeurs sont chiffrées).
mapfile -t keys < <(grep -oE '^[A-Za-z0-9_.-]+:' "$src" | tr -d ':' | grep -vx 'sops')
[[ ${#keys[@]} -gt 0 ]] || die "aucun secret dans $src"

app_keys=()
host_keys=()
for key in "${keys[@]}"; do
	[[ "$key" =~ ^[a-z][a-z0-9_]*$ ]] || die "nom de secret invalide : $key"
	if [[ " ${HOST_SECRETS[*]} " == *" $key "* ]]; then
		host_keys+=("$key")
	else
		app_keys+=("$key")
	fi
done

umask 077
stagings=()
trap 'rm -rf "${stagings[@]}"' EXIT

# Déchiffre les clés données dans un répertoire temporaire voisin de la destination, puis le
# met en place d'un coup (l'ancien répertoire n'est jamais à moitié remplacé).
render_into() {
	local dest="$1" parent staging old key
	shift
	parent="$(dirname "$dest")"
	staging="$(mktemp -d "$parent/.secrets.XXXXXX")"
	stagings+=("$staging")
	for key in "$@"; do
		sops decrypt --extract "[\"$key\"]" "$src" >"$staging/$key"
		chmod 0400 "$staging/$key"
	done
	chmod 0500 "$staging"
	if [[ "$(id -u)" == 0 ]]; then
		chown -R "$owner" "$staging"
	fi
	old="$parent/.secrets.old.$$.$(basename "$dest")"
	if [[ -e "$dest" ]]; then
		chmod -R u+w "$dest"
		mv "$dest" "$old"
	fi
	mv "$staging" "$dest"
	rm -rf "$old"
}

# Les deux destinations sont vérifiées avant toute écriture : pas de mise en place partielle.
for dest in "$app_dest" "$host_dest"; do
	mkdir -p "$(dirname "$dest")"
	# Traversée sans lecture : les scripts de l'hôte (utilisateur finances, pas root) atteignent
	# leurs secrets. Sous umask 077, ou resté en 0700, le parent les en empêchait (sauvegardes
	# arrêtées du 19 au 26/10/2026).
	if [[ -O "$(dirname "$dest")" ]]; then
		chmod go+x,go-rw "$(dirname "$dest")"
	fi
	if [[ "${FINANCES_SECRETS_ALLOW_DISK:-0}" != 1 ]]; then
		fstype="$(findmnt -no FSTYPE --target "$(dirname "$dest")")"
		[[ "$fstype" == tmpfs || "$fstype" == ramfs ]] ||
			die "$(dirname "$dest") n'est pas en mémoire ($fstype) : refus d'y écrire des secrets"
	fi
done

render_into "$app_dest" "${app_keys[@]}"
render_into "$host_dest" "${host_keys[@]}"
trap - EXIT

echo "render-secrets: ${#app_keys[@]} secret(s) de l'application dans $app_dest," \
	"${#host_keys[@]} de l'hôte dans $host_dest"
