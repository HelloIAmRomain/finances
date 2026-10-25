#!/usr/bin/env bash
# Installation et durcissement idempotents du Raspberry Pi (DT §5, T0.13).
# Relancer le script ne change rien si tout est déjà en place.
#
# Usage (root) : bootstrap.sh
# Étapes qui demandent une action humaine (le script s'arrête proprement et dit quoi faire) :
#   - clé de déploiement GitHub (lecture seule) à ajouter au dépôt privé des secrets ;
#   - connexion Tailscale (lien à ouvrir dans un navigateur) ;
#   - certificats HTTPS du tailnet (une case à cocher dans la console Tailscale) ;
#   - secrets chiffrés (deploy/secrets.enc.yaml) pour la clé age de la machine.
#
# Code : dépôt public (FINANCES_REPO_URL). Secrets chiffrés : dépôt privé à part
# (FINANCES_SECRETS_REPO_URL, cloné dans /srv/finances-secrets) ; sans lui, ils sont attendus
# dans le dépôt de code (installation en un seul dépôt privé).
set -euo pipefail

REPO_URL="${FINANCES_REPO_URL:-https://github.com/HelloIAmRomain/finances.git}"
SECRETS_REPO_URL="${FINANCES_SECRETS_REPO_URL:-}"
DIR=/srv/finances
SECRETS_REPO_DIR="${FINANCES_SECRETS_REPO_DIR:-/srv/finances-secrets}"
AGE_KEY=/etc/finances/age/keys.txt
DEPLOY_KEY=/root/.ssh/finances_deploy
APP_UID=10001
NTFY_UID=10002
# renovate: datasource=github-releases depName=getsops/sops
SOPS_VERSION="3.13.3"
DOCKER_KEY_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88 # gitleaks:allow (empreinte publique de clé GPG)
TAILSCALE_KEY_FPR=2596A99EAAB33821893C0A79458CA832957F5868 # gitleaks:allow (empreinte publique de clé GPG)

changed=0
step() { echo "── $*"; }
done_() { echo "   ✓ $*"; }
applied() {
	echo "   → $*"
	changed=1
}
die() {
	echo "bootstrap: $*" >&2
	exit 1
}
stop_here() {
	echo
	echo "À FAIRE : $*"
	echo "Puis relancer ce script : sudo bash $(readlink -f "$0")"
	exit 3
}

# Écrit un fichier seulement si son contenu change (idempotence).
install_file() {
	local path="$1" mode="$2" content
	content="$(cat)"
	if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
		return 1
	fi
	install -d "$(dirname "$path")"
	printf '%s\n' "$content" >"$path"
	chmod "$mode" "$path"
	return 0
}

apt_install() {
	local missing=()
	for package in "$@"; do
		dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" ||
			missing+=("$package")
	done
	if ((${#missing[@]})); then
		DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
		applied "paquets installés : ${missing[*]}"
	fi
}

# Dépôt APT tiers, avec vérification de l'empreinte de la clé de signature.
apt_repository() {
	local name="$1" key_url="$2" fingerprint="$3" line="$4"
	local keyring="/etc/apt/keyrings/$name.gpg" tmp
	if [[ ! -f "$keyring" ]]; then
		tmp="$(mktemp)"
		export GNUPGHOME
		GNUPGHOME="$(mktemp -d)"
		curl -fsSL "$key_url" | gpg --dearmor >"$tmp"
		gpg --show-keys --with-colons "$tmp" | awk -F: '/^fpr/{print $10}' | grep -qx "$fingerprint" ||
			die "empreinte inattendue pour la clé $name"
		install -D -m 0644 "$tmp" "$keyring"
		rm -rf "$tmp" "$GNUPGHOME"
		applied "clé APT $name vérifiée ($fingerprint)"
	fi
	if install_file "/etc/apt/sources.list.d/$name.list" 0644 <<<"deb [signed-by=$keyring] $line"; then
		apt-get update -qq
		applied "dépôt APT $name"
	fi
}

[[ $EUID -eq 0 ]] || die "à lancer en root (sudo)"
[[ "$(uname -m)" == aarch64 ]] || die "architecture inattendue : $(uname -m)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "$ID" == debian || "${ID_LIKE:-}" == *debian* ]] || die "système non Debian : $ID"
codename="$VERSION_CODENAME"

step "Paquets de base"
apt_install ca-certificates curl gnupg git nftables unattended-upgrades apt-listchanges \
	age restic python3 jq
done_ "paquets de base présents"

step "sops $SOPS_VERSION (somme de contrôle vérifiée)"
if ! sops --version 2>/dev/null | grep -q "$SOPS_VERSION"; then
	tmp="$(mktemp -d)"
	base="https://github.com/getsops/sops/releases/download/v$SOPS_VERSION"
	curl -fsSL -o "$tmp/sops" "$base/sops-v$SOPS_VERSION.linux.arm64"
	curl -fsSL -o "$tmp/sums" "$base/sops-v$SOPS_VERSION.checksums.txt"
	expected="$(awk -v f="sops-v$SOPS_VERSION.linux.arm64" '$2 == f {print $1}' "$tmp/sums")"
	echo "$expected  $tmp/sops" | sha256sum -c --quiet - || die "somme de contrôle sops invalide"
	install -m 0755 "$tmp/sops" /usr/local/bin/sops
	rm -rf "$tmp"
	applied "sops $SOPS_VERSION installé"
fi
done_ "sops $(sops --version 2>/dev/null | awk 'NR==1{print $2}')"

step "Docker (dépôt officiel)"
apt_repository docker https://download.docker.com/linux/debian/gpg "$DOCKER_KEY_FPR" \
	"https://download.docker.com/linux/debian $codename stable"
apt_install docker-ce docker-ce-cli containerd.io docker-compose-plugin
install_file /etc/docker/daemon.json 0644 <<'JSON' && applied "journalisation Docker bornée" && systemctl restart docker
{
  "log-driver": "journald",
  "live-restore": true,
  "no-new-privileges": true
}
JSON
systemctl enable --now docker >/dev/null 2>&1
done_ "$(docker --version)"

step "Tailscale (dépôt officiel)"
apt_repository tailscale "https://pkgs.tailscale.com/stable/debian/$codename.noarmor.gpg" \
	"$TAILSCALE_KEY_FPR" "https://pkgs.tailscale.com/stable/debian $codename main"
apt_install tailscale
systemctl enable --now tailscaled >/dev/null 2>&1
done_ "tailscale $(tailscale version | head -1)"

step "Utilisateurs de service"
getent group finances >/dev/null || { groupadd --system --gid "$APP_UID" finances && applied "groupe finances"; }
getent passwd finances >/dev/null || {
	useradd --system --uid "$APP_UID" --gid finances --home-dir /nonexistent --no-create-home \
		--shell /usr/sbin/nologin finances 2>/dev/null
	applied "utilisateur finances ($APP_UID)"
}
getent group finances-ntfy >/dev/null || { groupadd --system --gid "$NTFY_UID" finances-ntfy && applied "groupe finances-ntfy"; }
getent passwd finances-ntfy >/dev/null || {
	useradd --system --uid "$NTFY_UID" --gid finances-ntfy --home-dir /nonexistent \
		--no-create-home --shell /usr/sbin/nologin finances-ntfy 2>/dev/null
	applied "utilisateur finances-ntfy ($NTFY_UID)"
}
done_ "finances ($APP_UID), finances-ntfy ($NTFY_UID)"

step "Clé age de la machine"
if [[ ! -s "$AGE_KEY" ]]; then
	install -d -m 0700 "$(dirname "$AGE_KEY")"
	(umask 077 && age-keygen -o "$AGE_KEY" 2>/dev/null)
	applied "clé age générée ; à copier dans le gestionnaire de mots de passe depuis TON terminal :"
	echo "      sudo cat $AGE_KEY"
fi
chmod 0600 "$AGE_KEY"
done_ "clé publique : $(age-keygen -y "$AGE_KEY")"

step "Dépôt de déploiement ($DIR)"
if [[ ! -f "$DEPLOY_KEY" ]]; then
	install -d -m 0700 /root/.ssh
	ssh-keygen -q -t ed25519 -N '' -C "finances-deploy@$(hostname)" -f "$DEPLOY_KEY"
	applied "clé de déploiement générée"
fi
install_file /root/.ssh/config 0600 <<EOF || true
Host github.com
    IdentityFile $DEPLOY_KEY
    IdentitiesOnly yes
EOF
grep -q '^github.com ssh-ed25519 ' /root/.ssh/known_hosts 2>/dev/null ||
	curl -fsSL https://api.github.com/meta | jq -r '.ssh_keys[] | "github.com " + .' >>/root/.ssh/known_hosts
if [[ ! -d "$DIR/.git" ]]; then
	if ! git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
		stop_here "ajouter cette clé de déploiement (lecture seule) au dépôt GitHub :
      $(cat "$DEPLOY_KEY.pub")"
	fi
	git clone --quiet "$REPO_URL" "$DIR"
	applied "dépôt cloné"
fi
if [[ -n "$SECRETS_REPO_URL" && ! -d "$SECRETS_REPO_DIR/.git" ]]; then
	if ! git ls-remote "$SECRETS_REPO_URL" HEAD >/dev/null 2>&1; then
		stop_here "ajouter cette clé de déploiement (lecture seule) au dépôt privé des secrets :
      $(cat "$DEPLOY_KEY.pub")"
	fi
	git clone --quiet "$SECRETS_REPO_URL" "$SECRETS_REPO_DIR"
	applied "dépôt des secrets cloné"
fi
if [[ -f "$SECRETS_REPO_DIR/deploy/secrets.enc.yaml" ]]; then
	SECRETS_FILE="$SECRETS_REPO_DIR/deploy/secrets.enc.yaml"
else
	SECRETS_FILE="$DIR/deploy/secrets.enc.yaml"
fi
install -d -o finances -g finances -m 0750 "$DIR/data"
install -d -o finances-ntfy -g finances-ntfy -m 0750 "$DIR/ntfy"
install -d -m 0700 "$DIR/state"
done_ "$DIR à $(git -C "$DIR" rev-parse --short HEAD)"

step "Durcissement SSH"
if install_file /etc/ssh/sshd_config.d/10-finances.conf 0644 <<'EOF'; then
# Géré par /srv/finances/deploy/bootstrap.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
EOF
	sshd -t || die "configuration SSH invalide"
	systemctl reload ssh
	applied "SSH par clé uniquement, root interdit"
fi
done_ "SSH durci"

step "Services inutiles"
for unit in rpcbind.service rpcbind.socket nfs-blkmap.service; do
	if systemctl is-enabled --quiet "$unit" 2>/dev/null || systemctl is-active --quiet "$unit"; then
		systemctl disable --now "$unit" >/dev/null 2>&1
		systemctl mask "$unit" >/dev/null 2>&1
		applied "$unit désactivé"
	fi
done
if [[ "$(systemctl get-default)" != multi-user.target ]]; then
	systemctl set-default multi-user.target >/dev/null
	applied "démarrage sans interface graphique (effectif au prochain redémarrage)"
fi
done_ "rpcbind et interface graphique désactivés"

step "Pare-feu (nftables, table dédiée)"
lan="$(ip -4 route show scope link | awk '$1 ~ /\// && $0 !~ /docker|br-|tailscale/ {print $1; exit}')"
[[ -n "$lan" ]] || die "réseau local introuvable"
if install_file /etc/nftables.d/finances.nft 0644 <<EOF; then
# Géré par bootstrap.sh. Table dédiée : ne touche pas aux règles de Docker ni de Tailscale.
table inet finances_filter
delete table inet finances_filter
table inet finances_filter {
	chain input {
		type filter hook input priority filter; policy drop;
		iif "lo" accept
		ct state established,related accept
		ct state invalid drop
		meta l4proto { icmp, ipv6-icmp } accept
		iifname "tailscale0" accept
		udp dport 41641 accept comment "Tailscale (connexions directes)"
		ip saddr $lan tcp dport 22 accept comment "SSH depuis le réseau local"
		ip saddr $lan udp dport 5353 accept comment "mDNS (raspi.local)"
		ip6 saddr fe80::/10 udp dport { 546, 5353 } accept
	}
}
EOF
	nft -c -f /etc/nftables.d/finances.nft || die "règles nftables invalides"
	nft -f /etc/nftables.d/finances.nft
	applied "pare-feu appliqué (SSH ouvert pour $lan et tailscale0)"
fi
install_file /etc/systemd/system/finances-firewall.service 0644 <<'EOF' && applied "unité pare-feu" && systemctl daemon-reload
[Unit]
Description=Finances : pare-feu (table nftables dédiée)
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d/finances.nft

[Install]
WantedBy=multi-user.target
EOF
systemctl is-enabled --quiet finances-firewall.service || {
	systemctl enable finances-firewall.service >/dev/null 2>&1
	applied "pare-feu activé au démarrage"
}
done_ "entrées refusées sauf SSH (LAN), mDNS, Tailscale"

step "Limites mémoire des conteneurs (cgroup memory)"
# Le noyau Raspberry Pi désactive ce contrôleur par défaut : sans lui, les mem_limit de compose
# sont ignorés. Pris en compte au prochain redémarrage.
cmdline=/boot/firmware/cmdline.txt
if [[ -f "$cmdline" ]] && ! grep -qw "cgroup_enable=memory" "$cmdline"; then
	sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' "$cmdline"
	applied "cgroup memory activé : redémarrage nécessaire (sudo reboot)"
fi
done_ "cgroup memory $(grep -qw "cgroup_enable=memory" /proc/cmdline && echo actif || echo "en attente de redémarrage")"

step "Chien de garde matériel, journal, mises à jour automatiques"
install_file /etc/systemd/system.conf.d/10-watchdog.conf 0644 <<'EOF' && applied "chien de garde (15 s)" && systemctl daemon-reexec
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
# Journal conservé après redémarrage (NF-RES-05) : Raspberry Pi OS le garde en mémoire
# (40-rpi-volatile-storage.conf) ; ce fichier, lu après lui (90 > 40), l'emporte.
rm -f /etc/systemd/journald.conf.d/10-finances.conf
install -d -m 2755 -g systemd-journal /var/log/journal
install_file /etc/systemd/journald.conf.d/90-finances.conf 0644 <<'EOF' && applied "journal persistant, borné à 200 Mo" && systemctl restart systemd-journald && journalctl --flush
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF
install_file /etc/apt/apt.conf.d/52finances-unattended 0644 <<'EOF' && applied "mises à jour automatiques"
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename},label=Debian-Security";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
        "origin=Debian,codename=${distro_codename},label=Debian";
        "origin=Raspberry Pi Foundation,codename=${distro_codename}";
        "origin=Docker,codename=${distro_codename}";
        "origin=Tailscale";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
install_file /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF' || true
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
timedatectl show -p NTPSynchronized --value | grep -qx yes || echo "   ! horloge non synchronisée (NTP)"
done_ "chien de garde, journal persistant ($(journalctl --list-boots --no-pager 2>/dev/null | wc -l) démarrages conservés), mises à jour de sécurité quotidiennes"

step "Unités systemd de l'application"
if install_file /etc/systemd/system/finances-secrets.service 0644 <<EOF; then
[Unit]
Description=Finances : déchiffrement des secrets vers un tmpfs
ConditionPathExists=$SECRETS_FILE
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=SOPS_AGE_KEY_FILE=$AGE_KEY
ExecStart=$DIR/deploy/render-secrets.sh $SECRETS_FILE

[Install]
WantedBy=multi-user.target
EOF
	applied "unité finances-secrets"
fi
for unit in "$DIR"/deploy/systemd/*.service "$DIR"/deploy/systemd/*.timer; do
	target="/etc/systemd/system/$(basename "$unit")"
	if ! cmp -s "$unit" "$target"; then
		install -m 0644 "$unit" "$target"
		applied "unité $(basename "$unit")"
	fi
done
systemctl daemon-reload
systemctl enable finances-secrets.service >/dev/null 2>&1
# Disque figé → redémarrage (NF-RES-05). Désactivable en cas de besoin : fichier
# /etc/finances/no-watchdog (docs/exploitation.md).
systemctl is-enabled --quiet finances-watchdog.service || {
	systemctl enable --now finances-watchdog.service >/dev/null 2>&1
	applied "chien de garde du disque activé"
}
done_ "unités installées ; chien de garde du disque : $(systemctl is-active finances-watchdog.service 2>/dev/null || true)"

# --- Prérequis humains, dans l'ordre -------------------------------------------------
step "Connexion Tailscale"
if ! tailscale status >/dev/null 2>&1; then
	echo "   Ouvrir le lien affiché ci-dessous pour autoriser la machine :"
	timeout 5 tailscale up --ssh=false --accept-dns=false --timeout=3s 2>&1 | grep -Eo 'https://login.tailscale.com/[^ ]+' | head -1 || true
	stop_here "autoriser la machine dans Tailscale (lien ci-dessus, ou : sudo tailscale up)"
fi
done_ "Tailscale connecté : $(tailscale status --json | jq -r '.Self.DNSName')"

step "Accès HTTPS par Tailscale et configuration"
host="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
[[ "$host" == *.ts.net ]] || die "nom MagicDNS introuvable ($host) : activer MagicDNS dans la console Tailscale"
# Seul le tailnet voit ces adresses (inv. 7) : `serve`, jamais `funnel`.
if install_file "$DIR/deploy/.env" 0600 <<EOF; then applied "deploy/.env écrit"; fi
FINANCES_PUBLIC_BASE_URL=https://$host
FINANCES_BACKUP_EXPECTED=true
NTFY_BASE_URL=https://$host:8443
EOF
serve_config="$(tailscale serve status --json 2>/dev/null || echo '{}')"
for mapping in "443 8000" "8443 8080"; do
	read -r public local <<<"$mapping"
	if ! jq -e --arg key "$host:$public" '.Web[$key]' <<<"$serve_config" >/dev/null 2>&1; then
		timeout 20 tailscale serve --bg --yes --https="$public" "http://127.0.0.1:$local" >/dev/null 2>&1 ||
			stop_here "activer les certificats HTTPS du tailnet : console Tailscale › DNS › HTTPS Certificates › Enable"
		applied "https://$host:$public → 127.0.0.1:$local"
	fi
done
if jq -e '.AllowFunnel // {} | length > 0' <<<"$serve_config" >/dev/null 2>&1; then
	die "Tailscale Funnel est actif : l'application serait exposée à Internet (inv. 7)"
fi
done_ "interface : https://$host ; ntfy : https://$host:8443"

step "Secrets"
if [[ ! -f "$SECRETS_FILE" ]]; then
	stop_here "créer $SECRETS_FILE chiffré pour la clé publique ci-dessus et la clé de secours
      (procédure : docs/exploitation.md, section Secrets)"
fi
systemctl restart finances-secrets.service
done_ "secrets déchiffrés dans /run/finances/secrets (application) et /run/finances/host-secrets (hôte)"

step "Registre d'images (GHCR, lecture seule)"
if ! grep -q '"ghcr.io"' /root/.docker/config.json 2>/dev/null; then
	[[ -r /run/finances/host-secrets/ghcr_token ]] ||
		stop_here "ajouter le secret ghcr_token (jeton GitHub limité à read:packages) dans secrets.enc.yaml"
	docker login ghcr.io --username "$(git -C "$DIR" remote get-url origin | sed -E 's#.*[:/]([^/]+)/[^/]+$#\1#')" \
		--password-stdin </run/finances/host-secrets/ghcr_token >/dev/null
	applied "connecté à ghcr.io"
fi
done_ "accès au registre"

step "Base de données"
if [[ ! -e "$DIR/data/finances.db" ]] && [[ -r /run/finances/host-secrets/restic_repository ]]; then
	install -d -o finances -g finances -m 0700 /run/finances-restore
	if runuser -u finances -- env FINANCES_BACKUP_TMP=/run/finances-restore \
		RESTIC_CACHE_DIR=/var/cache/finances-restic "$DIR/deploy/restore.sh" "$DIR/data/finances.db"; then
		applied "base restaurée depuis la dernière sauvegarde"
	else
		echo "   ! aucune sauvegarde restaurable : démarrage sur une base neuve"
	fi
	rm -rf /run/finances-restore
fi
done_ "base : $([[ -e "$DIR/data/finances.db" ]] && echo présente || echo "créée au premier démarrage")"

step "Minuteries et premier déploiement"
timers=(finances-update.timer)
[[ -r /run/finances/host-secrets/restic_repository ]] &&
	timers+=(finances-backup.timer finances-backup-maintenance.timer finances-restore-test.timer)
for timer in "${timers[@]}"; do
	systemctl is-enabled --quiet "$timer" || {
		systemctl enable --now "$timer" >/dev/null 2>&1
		applied "$timer activé"
	}
done
if [[ ! -s "$DIR/state/current-image" ]]; then
	"$DIR/deploy/update.sh" --no-git
	applied "premier déploiement"
fi
done_ "application en service : $(cat "$DIR/state/current-image")"

echo
if ((changed)); then echo "bootstrap : modifications appliquées."; else echo "bootstrap : rien à faire, tout est en place."; fi
