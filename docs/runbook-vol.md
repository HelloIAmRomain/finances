# Runbook : vol, perte ou compromission

À suivre dans l'ordre si le Raspberry Pi (ou son SSD) est volé ou perdu, si l'on soupçonne un
accès non autorisé, ou si un secret a fuité (poste de travail compromis, jeton affiché par erreur).

Rappel de ce qui est en jeu : le système ne peut **déplacer aucun argent** (lecture seule, NF-SEC-01).
Un attaquant peut en revanche **lire** les données (transactions, soldes, coordonnées du
locataire) : le disque n'est pas chiffré, risque accepté (NF-SEC-03). Le but est donc de couper
ses accès futurs et de rendre inutilisables les secrets qu'il a pu copier.

## 1. Couper l'accès aux données bancaires (dans les minutes qui suivent)

| Action | Où | Effet |
| --- | --- | --- |
| Révoquer chaque consentement bancaire | Si la machine est accessible : page *Connexions* → *Révoquer* (`DELETE /sessions/{id}`). Sinon : Control Panel Enable Banking → sessions actives → révoquer | Plus aucune lecture de compte possible, même avec la clé privée volée |
| Révoquer l'application Enable Banking | Control Panel → application → supprimer la clé publique (ou l'application) | La clé privée RSA copiée devient inutile |
| Révoquer les clés crypto (lot 4) | Plateforme crypto → API → supprimer les clés | Idem (clés en lecture seule, mais données exposées) |

## 2. Couper l'accès à la machine et aux services

| Action | Où |
| --- | --- |
| Retirer le nœud du tailnet | Console Tailscale → *Machines* → le Pi → *Remove* (et *Disable key expiry* retiré) |
| Supprimer la clé de déploiement GitHub | Dépôt privé des secrets → *Settings → Deploy keys* → « raspi (bootstrap, lecture seule) » |
| Révoquer le jeton `ghcr_token` | GitHub → *Settings → Developer settings → Personal access tokens* |
| Révoquer la clé d'application B2 | Backblaze → *Application Keys* → supprimer. Les sauvegardes restent protégées par Object Lock (30 jours) |
| Changer le mot de passe SMTP d'application | Fournisseur de messagerie |
| Régénérer les URL Healthchecks | Healthchecks.io → chaque check → *Regenerate ping URL* (évite de faux signes de vie) |
| Retirer la clé SSH d'administration | `~/.ssh/authorized_keys` n'existe plus avec la machine ; supprimer `~/.ssh/id_ed25519_raspi` du poste s'il est lui-même compromis |

## 3. Faire tourner les secrets

Tous les secrets de `deploy/secrets.enc.yaml` (dépôt des secrets) sont à considérer comme connus de l'attaquant
(la clé age de la machine était sur le disque) :

1. Générer une **nouvelle** clé age de machine lors de la reconstruction (ne pas réutiliser l'ancienne).
2. Retirer l'ancienne clé publique de `.sops.yaml`, ajouter la nouvelle, puis
   `sops updatekeys deploy/secrets.enc.yaml` avec la clé de secours.
3. Remplacer chaque valeur : `app_secret_key` (scelle les messages et les jetons CSRF ; elle
   **ne ferme pas** les sessions), clé RSA Enable Banking (nouvelle application ou nouvelle
   clé), clé B2, jeton GHCR, mot de passe SMTP, URL Healthchecks.
4. **Mot de passe du dépôt restic** : `restic key add` avec un nouveau mot de passe, puis
   `restic key remove` de l'ancien (les données restent chiffrées avec la clé maîtresse du dépôt ;
   l'attaquant ne pourra plus s'en servir sans l'ancien mot de passe).

## 4. Reconstruire

Nouveau SSD, Raspberry Pi OS Lite, puis `bootstrap.sh` (voir `docs/exploitation.md`) :
restauration automatique de la dernière sauvegarde restic, puis nouvelle liaison des banques
(la DSP2 exige de toute façon une authentification forte).

Puis, la base restaurée contenant les accès d'avant le vol :

1. `sudo /srv/finances/deploy/compose.sh run --rm web python -m app.cli fermer-acces` : ferme
   toutes les sessions, retire tous les téléphones et tous les accès IA (client MCP).
2. Changer le mot de passe et le secret TOTP du compte (recréer le compte avec
   `create-admin` sous un autre identifiant si besoin), retirer puis réinscrire la clé de sécurité.
3. Appairer de nouveau le téléphone, recréer l'accès IA du client MCP.

## 5. Après coup

- Prévenir les co-indivisaires et, si des coordonnées du locataire ont pu fuiter, évaluer
  l'obligation de notification (RGPD : données d'un tiers).
- En cas de vol physique : dépôt de plainte (numéro de série du Pi et du SSD).
- Consigner l'incident dans `docs/PLAN.md` (journal) et relire ce runbook.
