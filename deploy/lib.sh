# shellcheck shell=bash
# Fonctions communes aux scripts d'exploitation (sauvegarde, restauration, maintenance).
# Les secrets de l'hôte sont lus dans le tmpfs produit par render-secrets.sh (répertoire jamais
# monté dans les conteneurs) ; une variable d'environnement déjà définie a priorité (tests,
# opérations manuelles).

FINANCES_HOST_SECRETS_DIR="${FINANCES_HOST_SECRETS_DIR:-/run/finances/host-secrets}"
# Dépôt privé des secrets chiffrés, à côté du dépôt de code (public). Absent : les secrets sont
# dans le dépôt de code lui-même (installation en un seul dépôt privé).
FINANCES_SECRETS_REPO_DIR="${FINANCES_SECRETS_REPO_DIR:-/srv/finances-secrets}"

die() {
	echo "$(basename "$0"): $*" >&2
	exit 1
}

log() {
	echo "$(basename "$0"): $*"
}

# Fichier des secrets chiffrés : dans le dépôt des secrets s'il existe, sinon dans le dépôt de code.
encrypted_secrets_file() {
	local code_dir="$1"
	if [[ -f "$FINANCES_SECRETS_REPO_DIR/deploy/secrets.enc.yaml" ]]; then
		echo "$FINANCES_SECRETS_REPO_DIR/deploy/secrets.enc.yaml"
	else
		echo "$code_dir/deploy/secrets.enc.yaml"
	fi
}

read_secret() {
	local file="$FINANCES_HOST_SECRETS_DIR/$1"
	[[ -r "$file" ]] && cat "$file"
}

# Refuse un répertoire de travail hors mémoire : le clair de la base ne doit pas toucher le SSD.
require_memory_fs() {
	local dir="$1" fstype
	[[ "${FINANCES_ALLOW_DISK:-0}" == 1 ]] && return 0
	fstype="$(findmnt -no FSTYPE --target "$dir")"
	[[ "$fstype" == tmpfs || "$fstype" == ramfs ]] ||
		die "$dir n'est pas en mémoire ($fstype) : refus d'y copier la base"
}

# Configure restic (dépôt B2 chiffré) depuis les secrets, sans jamais les afficher.
setup_restic() {
	command -v restic >/dev/null || die "restic introuvable"
	if [[ -z "${RESTIC_REPOSITORY:-}" ]]; then
		RESTIC_REPOSITORY="$(read_secret restic_repository)" || die "secret restic_repository absent"
	fi
	export RESTIC_REPOSITORY
	if [[ -z "${RESTIC_PASSWORD_FILE:-}" && -z "${RESTIC_PASSWORD:-}" ]]; then
		export RESTIC_PASSWORD_FILE="$FINANCES_HOST_SECRETS_DIR/restic_password"
	fi
	if [[ -z "${AWS_ACCESS_KEY_ID:-}" && -r "$FINANCES_HOST_SECRETS_DIR/b2_key_id" ]]; then
		AWS_ACCESS_KEY_ID="$(read_secret b2_key_id)"
		AWS_SECRET_ACCESS_KEY="$(read_secret b2_application_key)" || die "secret b2_application_key absent"
		export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
	fi
}

# Ping Healthchecks (succès, ou « fail » en argument). Silencieux si aucune URL n'est configurée.
healthcheck_ping() {
	local secret_name="$1" suffix="${2:-}" url
	url="${FINANCES_HEALTHCHECK_URL:-$(read_secret "$secret_name" || true)}"
	[[ -n "$url" ]] || return 0
	curl -fsS -m 10 --retry 3 -o /dev/null "${url}${suffix:+/$suffix}" ||
		echo "$(basename "$0"): ping Healthchecks en échec" >&2
}

# Vérifie l'intégrité d'une base SQLite (python3 : présent sur l'hôte, sans dépendance).
sqlite_integrity_ok() {
	python3 - "$1" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = connection.execute("PRAGMA integrity_check").fetchone()[0]
sys.exit(0 if result == "ok" else 1)
PY
}
