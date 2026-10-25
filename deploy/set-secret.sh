#!/usr/bin/env bash
# Ajoute ou remplace un secret dans deploy/secrets.enc.yaml sans l'afficher ni l'écrire en clair
# (inv. 5). Usage, depuis la racine du dépôt :
#   deploy/set-secret.sh <nom>      saisie masquée ; valeur sur plusieurs lignes acceptée (clé .pem)
#   deploy/set-secret.sh --liste    noms des secrets présents (les valeurs restent chiffrées)
# La clé de secours age est demandée une fois par lancement, jamais enregistrée.
# Secrets dans un dépôt privé à part (dépôt de code public) :
#   FINANCES_SECRETS_FILE=../finances-secrets/deploy/secrets.enc.yaml deploy/set-secret.sh <nom>
set -euo pipefail

file="${FINANCES_SECRETS_FILE:-$(cd "$(dirname "$0")" && pwd)/secrets.enc.yaml}"
[[ -f "$file" ]] || {
	echo "introuvable : $file" >&2
	exit 1
}

if [[ "${1:-}" == "--liste" ]]; then
	grep -oE '^[a-z0-9_]+:' "$file" | tr -d ':' | grep -vx sops
	exit 0
fi

name="${1:?usage : deploy/set-secret.sh <nom_du_secret> | --liste}"
[[ "$name" =~ ^[a-z0-9_]+$ ]] || {
	echo "nom invalide (minuscules, chiffres, _) : $name" >&2
	exit 2
}

if [[ -z "${SOPS_AGE_KEY:-}" && -z "${SOPS_AGE_KEY_FILE:-}" ]]; then
	read -rsp "Clé de secours (la ligne AGE-SECRET-KEY-…) : " SOPS_AGE_KEY
	echo
	export SOPS_AGE_KEY
fi

echo "Coller la valeur de « $name », puis Entrée et Ctrl-D (rien ne s'affiche) :"
restore_tty() { stty echo 2>/dev/null || true; }
trap restore_tty EXIT
stty -echo 2>/dev/null || true
value="$(cat)"
restore_tty
[[ -n "$value" ]] || {
	echo "valeur vide : rien n'est modifié" >&2
	exit 2
}

# sops attend une chaîne JSON ; elle passe par l'entrée standard, jamais par la ligne de commande.
printf '%s' "$value" | python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.stdin.read()))' |
	sops set --value-stdin "$file" "[\"$name\"]"
echo "✓ $name enregistré (chiffré). Penser à commiter $file."
