# Dossier technique — Système de gestion des finances personnelles

Sep 27, 2026

## 1. Synthèse des choix

Le système sera une application sur mesure en Python, sur une seule machine à domicile, branchée à Enable Banking en mode gratuit restreint. Tout est en lecture seule, aucun port n'est ouvert sur Internet, et la base est répliquée en continu hors site.

| Domaine | Choix retenu | Raison principale |
| --- | --- | --- |
| Agrégation bancaire | Enable Banking, mode « Restricted Production » | Seule offre DSP2 gratuite et self-serve en Europe pour ses propres comptes |
| Secours bancaire | Import CSV / OFX | Aucune dépendance unique à un fournisseur |
| Crypto | **Hors périmètre** (29/09/2026) | Le garde de démarrage refuse toute clé crypto présente dans les secrets |
| Socle applicatif | Développement sur mesure, monolithe modulaire | Loyers, indivision et alertes sont le cœur et n'existent dans aucun outil |
| Langage | Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic | Écosystème mûr, bibliothèques financières et PDF |
| Interface | Rendu serveur (Jinja + HTMX), Chart.js embarqué ; application **Android native** (Kotlin, Jetpack Compose) sur une API JSON v1 | Peu de JavaScript à maintenir ; vraie application sur le téléphone (la PWA restait bancale) |
| Base de données | SQLite en mode WAL | Zéro administration, un seul fichier |
| Sauvegarde | restic toutes les 15 min vers Backblaze B2 (région UE), chiffré côté client ; perte maximale 15 min | Chiffrement côté client (Litestream 0.5 ne le permet plus), restauration en une commande |
| Tâches planifiées | Planificateur intégré (tâches déclarées dans le code), état et verrous dans la table `job_run` | Reprise sans doublon après coupure |
| Notifications | ntfy auto-hébergé ; canal e-mail codé mais **non utilisé** (pas de SMTP, 30/09/2026) | Push instantané sur Android sans service tiers |
| Accès distant | Tailscale, HTTPS automatique | Aucun port ouvert, aucun certificat à gérer |
| Supervision | Healthchecks.io (gratuit) | Alerte si la machine se tait |
| IA | Serveur **MCP** pour un client MCP de bureau (sur abonnement) + assistant intégré à fournisseur choisi (API Anthropic, OpenAI, OVH), mêmes outils, données pseudonymisées, écritures annulables (`docs/spec-mcp.md`) | Analyse et pilotage en langage naturel sans exposer IBAN ni noms complets ; interrupteurs pour tout couper |
| Authentification | Mot de passe + second facteur au choix : code TOTP ou clé de sécurité FIDO2 (YubiKey, WebAuthn) | Résistant à l'hameçonnage, code toujours disponible en secours |
| Matériel | Raspberry Pi 4 (4 Go) dédié ; SSD USB **recommandé**, clé USB ou carte SD acceptées | Une clé ou une carte s'use plus vite : risque accepté (28/09/2026) grâce à la sauvegarde toutes les 15 min et à la reconstruction en une heure ; jamais d'hôte partagé avec un bot de trading |
| Déploiement | Docker Compose, images construites par CI | Reproductible, retour arrière automatique |
| Secrets | sops + age, clé présente uniquement sur la machine | Dépôt Git sans secret en clair |

## 2. Architecture

Deux processus partagent une base SQLite : le worker collecte, rapproche et alerte ; le serveur web affiche et notifie. Toutes les connexions vers l'extérieur sont sortantes, et seuls les appareils de l'utilisateur sur Tailscale atteignent l'interface.

&#91;embedded content: architecture · une machine, flux sortants uniquement\]

Le worker ne détient aucun droit d'écriture chez une banque ou une plateforme crypto : une compromission complète de la machine exposerait des données, jamais de l'argent.

**Découpage du code en modules**

| Module | Rôle |
| --- | --- |
| `connectors` | Une interface commune `fetch_accounts` / `fetch_transactions` ; implémentations Enable Banking, import fichier, INSEE |
| `ledger` | Comptes, transactions, dédoublonnage, virements internes |
| `categorize` | Règles utilisateur, puis IA, puis file à valider |
| `rental` | Biens, baux, échéancier, rapprochement, IRL, indivision, quittances |
| `alerts` | Moteur de règles, cycle de vie, envoi ntfy (e-mail possible, non configuré) |
| `assistant` | Outils de l'IA (lecture, écriture annulable), pseudonymisation, serveur MCP, assistant intégré |
| `tax` | Récapitulatifs annuels et exports |
| `web` | Pages, API JSON v1 (application Android), serveur `/mcp`, authentification (TOTP, WebAuthn) |
| `ops` | Planificateur, santé des connecteurs, pings de supervision |
| `net` | Client HTTP unique : liste blanche explicite par hôte, méthode et chemin ; toute autre requête est refusée avant l'envoi |

## 3. Agrégation bancaire

Enable Banking est retenu : son mode « Restricted Production » donne gratuitement accès aux données réelles des comptes que l'on lie soi-même. GoCardless (ex-Nordigen), l'ancienne référence gratuite, n'accepte plus de nouvelles inscriptions ([Open Banking Tracker](https://www.openbankingtracker.com/guides/free-open-banking-apis)).

| Fournisseur | Accès pour un particulier | Verdict |
| --- | --- | --- |
| [Enable Banking](https://enablebanking.com/docs/api/linked-accounts) | Gratuit en mode restreint, limité aux comptes liés par le titulaire ; usage individuel non commercial | Retenu |
| [GoCardless Bank Account Data](https://actualbudget.org/docs/advanced/bank-sync/) | Fermé aux nouveaux comptes | Exclu |
| Bridge, Powens, Tink, Linxo | Offres professionnelles, contrat et tarif sur devis ou abonnement | Exclu : coût et contrat d'entreprise |
| Woob (scraping) | Open source, mais stocke les identifiants bancaires en local | Exclu : contraire à l'exigence de sécurité |
| open-banking.io | Alternative payante récente, réponses chiffrées de bout en bout | Plan B si Enable Banking change ses conditions |

**Fonctionnement retenu**

1. Créer une application « Production » dans le Control Panel d'Enable Banking ; la clé privée RSA reste uniquement sur la machine.
2. Lier chaque banque via le portail : c'est ce qui active le mode restreint.
3. L'application lance ensuite sa propre autorisation par l'API, avec une URL de retour HTTPS servie par Tailscale, joignable seulement depuis les appareils de l'utilisateur.
4. La durée du consentement demandée est le maximum autorisé par chaque banque, lu dans la réponse de l'API.
5. Premier passage : historique disponible (souvent 90 jours) ; ensuite, synchro incrémentale.

**Contraintes réglementaires à respecter**

- Sans action de l'utilisateur, un agrégateur ne peut interroger un compte que 4 fois par 24 h (RTS DSP2). Le planning retenu est 4 synchros par jour pour les comptes d'encaissement des loyers, calées autour de l'heure d'arrivée habituelle des virements, et 2 par jour pour les autres comptes. Le budget compte chaque requête envoyée (reprises, rattrapages, pages et synchros manuelles compris) par compte et par endpoint, sur 24 h glissantes, avec un plafond propre à chaque banque (profils Boursorama et Société Générale, réglables par `FINANCES_BANK_BUDGETS`). Ce plafond est une **hypothèse** (Q14) : chaque banque interprète le compteur à sa façon ; les refus réellement constatés sont journalisés par banque pour l'ajuster. Une synchro manuelle consomme ce budget, car derrière Tailscale les en-têtes PSU (adresse IP de l'utilisateur) ne permettent pas à la banque de la reconnaître : elle est limitée à une par 15 min et par compte, et refusée (bouton désactivé, avec l'explication) s'il ne reste que de quoi faire la prochaine synchro automatique. Une réponse 429 ou un refus de quota n'est pas retenté avant le créneau suivant, n'ouvre pas le disjoncteur et ne compte pas comme une panne ; l'alerte « synchro en échec » ne part qu'après 24 h sans synchro réussie.
- Le renouvellement de l'authentification forte est passé de 90 à 180 jours depuis le 25/07/2023 ([Bridge](https://bridgeapi.io/blog-posts/authentification-forte-dsp2-definition-et-fonctionnement/)) ; certaines banques peuvent encore demander plus souvent.
- Le mode restreint ne couvre que les comptes propres de l'utilisateur : un compte commun d'indivision convient s'il en est cotitulaire, pas les comptes personnels d'un co-indivisaire.

## 4. Socle applicatif et stack

Le développement sur mesure l'emporte : les outils existants gèrent bien un budget, mais aucun ne gère baux, rapprochement de loyers ou indivision. Greffer ces modules sur l'un d'eux imposerait deux systèmes et deux bases à garder cohérents.

| Option | Points forts | Limites | Verdict |
| --- | --- | --- | --- |
| [Firefly III](https://docs.firefly-iii.org/tutorials/data-importer/eb/) + Data Importer | Enable Banking natif, règles, API | Pas de gestion locative ; PHP ; deux systèmes à synchroniser | Source d'inspiration |
| [Actual Budget](https://actualbudget.org/docs/advanced/bank-sync/) | Enable Banking intégré depuis la 26.6 (expérimental) | Orienté budget par enveloppes ; pas de loyers ni d'alertes métier | Non retenu |
| Sur mesure | Couvre tout le cahier des charges, un seul modèle de données | Tout à écrire | Retenu |

**Stack retenue**

| Brique | Choix | Usage |
| --- | --- | --- |
| Langage et dépendances | Python 3.12, `uv` avec fichier de verrouillage | Builds reproductibles |
| Web | FastAPI, Jinja2, HTMX | Pages rendues côté serveur ; API JSON v1 pour l'application Android |
| Graphiques | Chart.js embarqué dans le dépôt | Aucun CDN, fonctionne hors ligne |
| Données | SQLAlchemy 2, Alembic, Pydantic | Modèle typé, migrations versionnées |
| Base | SQLite en WAL, `foreign_keys` et `busy_timeout` activés | Un fichier, zéro administration |
| Appels externes | httpx + tenacity | Délais ; reprise seulement si la requête n'a pas pu partir (une reprise après un délai dépassé ou un 5xx brûlerait le budget DSP2) ; disjoncteur **par connexion bancaire** ; 2 tentatives au plus par créneau de synchro, aucune après un refus 4xx |
| Planification | Planificateur maison (`app/ops/scheduler.py`) : créneaux à l'heure de Paris, décalage déterministe de quelques minutes pour les banques, tâches déclarées dans le code ; table `job_run`, seule source de vérité (une exécution par créneau, pas de chevauchement, rattrapage unique après un arrêt). APScheduler écarté (Q13) | Idempotence et reprise sans dépendance supplémentaire |
| Rapprochement de libellés | rapidfuzz | Tolère les variations de libellé des virements |
| PDF | Générateur maison `app/pdf.py` (texte, polices standard, A4) ; WeasyPrint réservé à une mise en page riche future | Quittances sans bibliothèque système (pango, cairo) sur le Pi ni surface d'attaque supplémentaire ; sortie déterministe, testée |
| Import de fichiers | csv ; lecteur OFX maison, typé (ofxparse, plus maintenue depuis 2021, écartée) ; QIF exclu | Secours et historique |
| IA | Protocole MCP (implémentation propre, testée avec le SDK officiel) ; API Anthropic, OpenAI et OVH par le client HTTP unique | Outils communs derrière une couche de pseudonymisation |
| Authentification forte | `webauthn` (py_webauthn), pyotp, argon2 | Clé de sécurité ou code à 6 chiffres |
| Application mobile | Kotlin, Jetpack Compose, Material 3, OkHttp, kotlinx.serialization | Application native signée, publiée par la CI |
| Qualité | pytest, hypothesis, ruff, mypy | Tests de scénarios et typage strict |

**Règles de code non négociables** (à inscrire dans le `CONTRIBUTING.md` du dépôt)

- Montants stockés en centimes entiers, jamais en flottant.
- Aucun appel réseau capable d'écrire chez une banque ou une plateforme crypto.
- Tout traitement planifié est idempotent : le rejouer ne crée ni doublon ni alerte en double.
- Dates stockées en UTC, affichées en heure de Paris.

## 5. Hébergement, déploiement et mises à jour

Après le setup, la machine se met à jour, redémarre et se répare seule ; une nouvelle machine se reconstruit avec un script et une clé. Les mises à jour applicatives passent par des tests automatiques avant d'atteindre le Raspberry Pi.

**Machine**

- Raspberry Pi 4, alimentation officielle ; démarrage sur SSD USB **recommandé** (une clé USB ou une carte SD s'usent plus vite, risque accepté grâce aux sauvegardes) ; un petit onduleur est recommandé.
- Raspberry Pi OS Lite 64 bits, mises à jour de sécurité automatiques (`unattended-upgrades`) avec redémarrage nocturne si nécessaire.
- Chien de garde matériel activé : un système figé redémarre tout seul.
- Horloge synchronisée par NTP, indispensable pour les jetons signés (Enable Banking, TOTP).
- Machine dédiée : un éventuel bot de trading tourne sur une autre machine. Le système ne partage jamais un hôte avec des clés capables de trader ou de retirer, sinon la garantie « compromission = données, jamais argent » ne tient plus. Une cohabitation temporaire exige une acceptation explicite du risque et un isolement minimal (voir `PLAN.md`, section 1.3).
- Plafond mémoire par conteneur ; aucun conteneur ne monte le socket Docker ni ne tourne en `privileged`.

**Conteneurs (Docker Compose)**

| Service | Rôle | Politique |
| --- | --- | --- |
| `web` | Interface et API | Redémarrage automatique, sonde `/health` |
| `worker` | Synchros, rapprochement, alertes | Redémarrage automatique, ping Healthchecks à chaque cycle |
| `migrate` | Migrations Alembic | Ponctuel, avant `web` et `worker` |
| `ntfy` | Notifications push | Joignable uniquement via Tailscale |

Tailscale tourne sur l'hôte et publie l'interface en HTTPS avec un certificat automatique ([doc](https://docs.openwebui.com/reference/https/tailscale)).

**Chaîne de mise à jour**

1. Renovate ouvre une demande de fusion pour chaque dépendance à mettre à jour ; actions GitHub et images de base sont épinglées par condensat.
2. GitHub Actions lance tests, typage et scénarios, puis construit l'image arm64.
3. Les correctifs et versions mineures fusionnent seuls si tout est vert **et** si la version est publiée depuis au moins 7 jours (`minimumReleaseAge`) ; les versions majeures attendent une validation manuelle.
4. Chaque nuit, un timer systemd sur le Raspberry Pi récupère la nouvelle image. Si elle a changé : arrêt de `web` et `worker`, instantané de la base (`VACUUM INTO`), migrations, démarrage.
5. Si `/health` ne répond pas en 2 minutes : restauration de l'instantané et de l'image précédente, redémarrage, alerte. Une sauvegarde restic étiquetée `pre-deploy` est faite avant chaque migration.
6. Les migrations suivent le schéma « expand/contract » : une colonne n'est supprimée qu'une version après avoir cessé d'être utilisée, pour que l'image précédente reste compatible.

**Setup et reconstruction**

Un script `bootstrap.sh` idempotent installe Docker et Tailscale, déchiffre les secrets avec la clé age vers un tmpfs (`/run/finances/`, jamais sur le SSD), et démarre la pile. Si aucune base n'existe localement, le script restaure le dernier instantané restic depuis Backblaze B2 avant le premier démarrage : remplacer un SSD mort revient à flasher, lancer le script et fournir la clé.

## 6. Sécurité

La défense repose sur trois barrières : aucune capacité de déplacer de l'argent, aucune exposition sur Internet, et des données chiffrées partout où elles quittent la machine.

| Menace | Mesure |
| --- | --- |
| Mouvement de fonds | Toute requête sortante passe par un client HTTP unique qui refuse ce qui n'est pas dans une liste blanche par hôte, méthode et chemin (Enable Banking : seuls `POST /auth`, `POST /sessions` et `DELETE /sessions/{id}` — révocation du consentement — en écriture) ; un test échoue si un endpoint de paiement est appelable ; Enable Banking utilisé en lecture de comptes uniquement  ; crypto hors périmètre : l'application refuse de démarrer si une clé crypto figure dans les secrets |
| Accès distant non autorisé | Aucun port ouvert ; interface et ntfy joignables seulement via Tailscale ; connexion à l'application par mot de passe puis clé de sécurité FIDO2 ou code TOTP |
| Vol de la machine ou du SSD | **Risque accepté** (NF-SEC-03) : le disque n'est pas chiffré et la clé age est sur la machine, donc un vol expose les données. Atténuation : secrets déchiffrés uniquement en tmpfs ; IBAN, noms et données brutes chiffrés au niveau applicatif ; aucun droit de mouvement de fonds ; clé age de secours dans ton gestionnaire de mots de passe |
| Cohabitation avec des clés de trading | Interdite : machine dédiée ; un bot de trading tourne ailleurs |
| Fuite par la sauvegarde | Bucket B2 privé en région UE, clé d'application limitée à ce bucket ; instantanés restic chiffrés côté client ; Object Lock (rétention 30 jours) : une compromission du Pi ne peut pas effacer les sauvegardes |
| Fuite vers l'IA | Seuls libellé nettoyé, montant et date sont envoyés ; IBAN, noms et numéros remplacés par des jetons ; option désactivable |
| Contenu des notifications | Messages courts sans montant ni nom (« Loyer : alerte ouverte ») ; le détail reste dans l'interface |
| Dépendance compromise | Versions verrouillées, Renovate + CI, images construites depuis le dépôt uniquement |
| Erreur humaine d'envoi | Aucun e-mail au locataire sans validation, journal d'audit de chaque envoi |

Les données du locataire (nom, IBAN, e-mail) sont stockées dans des colonnes chiffrées au niveau applicatif, avec une clé distincte de celle des sauvegardes.

## 7. Résilience et supervision

Chaque panne prévisible a une détection et une réaction automatique ; la seule panne qui exige une intervention est la perte physique de la machine, et elle se règle par restauration.

| Panne | Détection | Réaction automatique |
| --- | --- | --- |
| Coupure de courant | Au redémarrage | Conteneurs relancés ; tâches manquées rattrapées grâce à la table `job_run` |
| Système figé | Chien de garde matériel | Redémarrage |
| Conteneur en échec | Sonde de santé Docker | Redémarrage du conteneur |
| API bancaire indisponible | Erreurs du connecteur | Reprises espacées ; alerte si aucune synchro réussie depuis 24 h |
| Consentement bientôt expiré | Date d'expiration en base | Alertes à J-14, J-7 et J-1 avec lien direct de renouvellement |
| Internet ou machine muets | [Healthchecks.io](https://healthchecks.io/about), ping manquant | E-mail d'alerte, indépendant de la machine |
| Mise à jour défectueuse | Sonde `/health` après déploiement | Retour à l'image et à l'instantané précédents |
| Base corrompue | `PRAGMA integrity_check` à chaque sauvegarde | Restauration du dernier instantané restic sain (15 min au plus de perte) |
| SSD détruit | Healthchecks.io | Reconstruction : `bootstrap.sh` + clé age, restauration depuis B2 |

**Sauvegardes**

- Toutes les 15 min : copie cohérente de la base **en mémoire** (API backup de SQLite vers un tmpfs), vérification d'intégrité, puis `restic backup --stdin` vers Backblaze B2, chiffré côté client. Rien ne transite par le SSD. **Perte maximale : 15 min** (analyse dans `PLAN.md`, section 1.5).
- Litestream est écarté : la version maintenue (0.5) ne chiffre plus ses répliques, contrairement à NF-SEC-03.
- Justificatifs (lot 4) : sauvegarde restic quotidienne.
- Rétention : tout sur 24 h, puis quotidien sur 30 jours, puis mensuel sur 12 mois ; `prune` et `restic check` hebdomadaires.
- Bucket B2 avec Object Lock (rétention par défaut de 30 jours) ; mot de passe du dépôt restic aussi conservé dans le gestionnaire de mots de passe.
- Test de restauration automatique chaque semaine sur une copie temporaire, avec vérification d'intégrité ; un échec déclenche une alerte.

**Checks Healthchecks.io prévus** (l'offre gratuite en autorise 20 ([Capterra](https://www.capterra.com/p/249957/Healthchecksio/pricing/)))

| Check | Période attendue |
| --- | --- |
| Cycle du worker | 1 h |
| Synchro bancaire réussie | 24 h |
| Sauvegarde restic | 15 min (grâce 30 min) |
| Maintenance restic (`forget`, `prune`, `check`) | 7 jours |
| Test de restauration | 7 jours |
| Déploiement nocturne | 24 h |

## 8. Modèle de données

Le modèle tourne autour de la transaction : tout le reste (catégories, loyers, indivision, alertes) s'y rattache par des tables de liaison, sans jamais modifier la donnée bancaire brute.

| Entité | Champs clés | Remarques |
| --- | --- | --- |
| `app_user`, `user_session` | identifiant, empreinte du mot de passe, secret TOTP chiffré, rôle ; sessions à expiration | Authentification forte (NF-SEC-04) |
| `connection` | fournisseur, banque, statut, `consent_expires_at` | Une par banque liée |
| `account` | connexion, type, IBAN chiffré + empreinte HMAC, devise, inclus dans le patrimoine, synchros par jour | Comptes bancaires et actifs manuels |
| `account_sync_state` | dernière tentative, dernier succès, dernière date comptable vue | État de synchro, séparé de la donnée brute |
| `provider_call` | compte, horodatage, sans action de l'utilisateur ou non, méthode, chemin, statut HTTP | Budget DSP2 compté en requêtes |
| `import_profile`, `import_batch` | mapping de colonnes par banque ; lot d'import (source, fichier, compteurs) | COL-02, traçabilité |
| `raw_payload` | lot d'import, date de réception, empreinte, contenu chiffré | Données brutes conservées pour rejouer un import |
| `ledger_transaction` | compte, `dedupe_key`, `external_id`, date comptable, date de valeur, `amount_cents`, contrepartie chiffrée, libellé, empreinte + rang d'occurrence | Index unique sur (compte, `dedupe_key`) ; `dedupe_key` = identifiant externe, sinon empreinte + rang (deux achats identiques le même jour restent distincts) ; toute modification est refusée par un trigger |
| `balance_snapshot` | compte, date, type de solde, montant | Fraîcheur des soldes, patrimoine |
| `duplicate_link` | transaction canonique, doublon, mode (auto, manuel), motif, statut | Doublons entre sources (API et fichiers) ; l'API est canonique ; réversible, rien n'est supprimé |
| `transfer_link` | transaction débit, transaction crédit, statut | Virements internes exclus des revenus et dépenses |
| `category`, `rule` | arborescence ; conditions, priorité | Règles utilisateur appliquées avant l'IA |
| `categorization` | transaction, catégorie, source (règle, IA, manuel), confiance, lien de doublon d'origine | Ventilation possible sur plusieurs lignes ; historique par remplacement |
| `review_item` | type (doublon, conflit, virement proposé), objets liés, statut | File de validation commune |
| `property`, `owner_share` | bien ; indivisaire, quote-part | Quote-parts en dix-millièmes |
| `lease`, `party` | bien, compte d'encaissement, dates, loyer, charges, échéance, indice ; locataire, caution, tiers payeur | Coordonnées chiffrées ; le compte d'encaissement est porté par le bail, pas par le bien |
| `rent_due` | bail, période, montant dû, date limite, statut | États du cahier des charges, section 5 |
| `rent_allocation` | transaction, échéance, montant affecté, mode (auto, manuel), statut (proposée, confirmée) | Un virement peut couvrir plusieurs échéances et inversement ; seules les allocations confirmées comptent ; plafonnée au montant dû |
| `rent_incident` | bail, échéance, type (retard, impayé, rejet, caution), date | Historique des incidents (LOC-14), conservé après paiement |
| `expense_share` | transaction, bien, payeur | Base du solde d'indivision |
| `irl_index` | trimestre, valeur | Série INSEE [001515333](https://fgeerolf.com/data/insee/IRL.html) |
| `alert` | type, gravité, statut, `dedupe_key`, objet lié | `dedupe_key` unique : jamais deux alertes pour le même fait |
| `notification` | alerte, canal, type (initiale, rappel), essais, statut | File d'envoi idempotente |
| `job_run` | tâche, créneau, statut, début, fin, erreur | Garantit une exécution par créneau |
| `document` | fichier, type, OCR, transaction liée | Justificatifs et quittances |
| `audit_log` | acteur, action, objet, horodatage | Exports, envois, changements de règles ; ni modification ni suppression (trigger) |

Conventions : instants en UTC (`*_at`), dates calendaires en `DATE` sans fuseau ; la table des transactions s'appelle `ledger_transaction` (`transaction` est un mot réservé SQL).

Les montants restent en centimes entiers dans toutes les tables ; les calculs d'IRL et de quote-parts arrondissent une seule fois, au centime, en fin de calcul.

## 9. Ce qui restera manuel

Zéro intervention absolue est impossible : la DSP2 impose de revalider chaque banque depuis son téléphone environ tous les 180 jours. Tout le reste de l'exploitation est automatique ; ce qui reste relève de décisions, pas de configuration.

| Action | Fréquence | Effort | Ce que fait le système |
| --- | --- | --- | --- |
| Renouveler le consentement d'une banque | Environ tous les 180 jours par banque | 2 minutes | Rappels à J-14, J-7, J-1 avec lien direct ; validation dans l'appli de la banque |
| Trancher un rapprochement incertain | Rare après le premier mois | Quelques secondes | Proposition pré-remplie, un clic pour valider |
| Valider une catégorie peu sûre | Décroît avec les règles apprises | Quelques secondes | Proposition de règle après chaque correction |
| Valider une relance ou une quittance | À chaque échéance concernée | Un clic | Brouillon prêt ; envoi automatique des quittances activable |
| Saisir un nouveau bail ou un changement | À chaque événement | 5 minutes | Échéancier et rappels générés seuls |
| Approuver une mise à jour majeure | Quelques fois par an | 5 minutes | Demande de fusion déjà testée par la CI |

## 10. Plan de réalisation

L'infrastructure et la chaîne de mise à jour se montent avant la première ligne de métier : c'est elle qui garantit le fonctionnement sans intervention. Les lots suivent ensuite ceux du cahier des charges.

| Étape | Travaux techniques | Critère de sortie |
| --- | --- | --- |
| 0 · Infrastructure | Support de démarrage (SSD recommandé), OS, Tailscale, Docker ; dépôt privé, CI, Renovate ; sops + age ; bucket B2, restic ; checks Healthchecks ; `bootstrap.sh` | Reconstruction complète réussie sur un support vierge |
| 1 · Socle | Modèle `connection` / `account` / `ledger_transaction` ; connecteur Enable Banking ; import CSV / OFX ; dédoublonnage (y compris entre sources), virements internes ; règles de catégorie ; recherche et export CSV ; tableau de bord ; authentification TOTP ; PWA ; ntfy et e-mail ; alertes techniques | 14 jours de synchro sans intervention |
| 2 · Loyers | Biens, baux, parties ; génération des échéances ; moteur de rapprochement ; alertes de loyer ; rapprochement manuel ; charges du bien (LOC-10) ; incidents | Scénarios de la section 9 du cahier des charges au vert, puis 2 échéances réelles |
| 3 · Dépenses | Catégorisation IA avec pseudonymisation ; budgets ; récurrents ; anomalies ; digest hebdomadaire | 90 % des transactions d'un mois catégorisées seules |
| 4 · Fiscalité | IRL INSEE ; quittances PDF ; indivision ; récapitulatifs fiscaux ; exports Excel et PDF ; OCR ; assistant IA | Récapitulatif annuel produit sans retouche manuelle |

**Structure du dépôt**

```text
finances/
  CONTRIBUTING.md            règles de contribution et invariants
  app/
    connectors/        enable_banking, file_import, insee
    ledger/  categorize/  rental/  alerts/  tax/  web/  ops/  net/
  migrations/          Alembic
  tests/
    scenarios/         cas de loyer et de dédoublonnage en YAML (entrées, états, alertes attendues)
    cassettes/         réponses d'API enregistrées, anonymisées
  deploy/
    compose.yml  bootstrap.sh  secrets.enc.yaml  systemd/
```

**Stratégie de tests**

- Scénarios de loyer décrits en YAML et joués par un moteur de test : chaque règle de la section 5 du cahier des charges a son scénario.
- Tests de propriété (hypothesis) sur le dédoublonnage et la répartition des montants : aucune somme ne se crée ni ne se perd.
- Connecteurs testés contre des réponses d'API enregistrées et anonymisées, sans appel réseau en CI.
- Test de reprise : interruption forcée au milieu d'une synchro, puis vérification qu'aucune transaction ni alerte n'est dupliquée.

## 11. Coûts récurrents

Le fonctionnement courant coûte pratiquement zéro : tous les services retenus ont une offre gratuite adaptée à un usage personnel, hors achat éventuel d'un SSD.

| Poste | Coût annuel estimé | Note |
| --- | --- | --- |
| Enable Banking, mode restreint | 0 € | Comptes personnels uniquement |
| Tailscale | 0 € | Gratuit pour un usage personnel |
| Healthchecks.io | 0 € | Offre gratuite jusqu'à 20 checks |
| Backblaze B2 | 0 € tant que moins de 10 Go | Au-delà, environ 7 $ par To et par mois ([source](https://dev.to/ethan-carter/backblaze-b2-vs-self-hosted-s3-which-saves-more-money-4gp8)) |
| API IA | Quelques euros (approximatif) | Quelques centaines de transactions par mois, textes courts |
| GitHub (dépôt privé, CI) | 0 € | Dans les quotas de l'offre gratuite, à surveiller pour les builds arm64 |
| SSD USB + boîtier | Achat unique, facultatif | Recommandé : plus endurant qu'une clé USB ou une carte SD |

## 12. Sources

- [Open Banking Tracker — Free & Indie Open Banking APIs (revu le 08/06/2026)](https://www.openbankingtracker.com/guides/free-open-banking-apis)
- [Enable Banking — Whitelisting own accounts for restricted API usage](https://enablebanking.com/docs/api/linked-accounts)
- [Enable Banking — FAQ](https://enablebanking.com/docs/faq/)
- [Firefly III — Import from Enable Banking](https://docs.firefly-iii.org/tutorials/data-importer/eb/)
- [Actual Budget — Connecting Your Bank](https://actualbudget.org/docs/advanced/bank-sync/)
- [Actual Budget — Release 26.6.0](https://actualbudget.org/blog/release-26.6.0/)
- [Bridge — Authentification forte DSP2](https://bridgeapi.io/blog-posts/authentification-forte-dsp2-definition-et-fonctionnement/)
- [Coinbase — Get API Key Permissions](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions)
- [Coinbase — API key scopes](https://docs.cdp.coinbase.com/coinbase-business/authentication-authorization/api-key-authentication)
- [OKX — API key security features](https://www.fintechfutures.com/press-releases/okx-announces-comprehensive-api-key-security-protection-features-further-enhances-user-protection-against-external-threats)
- [Open WebUI — HTTPS using Tailscale](https://docs.openwebui.com/reference/https/tailscale)
- [Healthchecks.io — About](https://healthchecks.io/about)
- [Healthchecks.io — Pricing (Capterra)](https://www.capterra.com/p/249957/Healthchecksio/pricing/)
- [Backblaze B2 vs self-hosted S3 (tarifs vérifiés 08/2026)](https://dev.to/ethan-carter/backblaze-b2-vs-self-hosted-s3-which-saves-more-money-4gp8)
- [restic](https://restic.net/) ; [Litestream](https://github.com/benbjohnson/litestream) (écarté : pas de chiffrement en 0.5)
- [Séries INSEE de l'IRL](https://fgeerolf.com/data/insee/IRL.html)

## 13. Architecture cible pour un produit (décision du 06/10/2026, CDC §10)

Principe : **une instance par foyer**, identique partout ; seule la configuration change.

| Sujet | Cible | Aujourd'hui | Écart à combler |
| --- | --- | --- | --- |
| Isolation | Une instance = une base, des secrets, des sauvegardes, des clés propres | ✅ (une seule instance) | Rien : le modèle tient tel quel |
| Utilisateurs | Plusieurs par instance, rôles administrateur / co-indivisaire / expert-comptable | Un administrateur | Rôles et droits par écran et par API |
| Packaging | Images multi-arch signées (x86-64, ARM64), Docker Compose ; Kubernetes (Helm) plus tard pour l'hébergement mutualisé | Images ARM64 et AMD64 publiées sur GHCR, Compose | Signature (cosign), SBOM, provenance |
| Installation | Un script d'installation générique (Debian, Ubuntu, Raspberry Pi OS ; x86-64 et ARM64) + assistant web de premier lancement | `bootstrap.sh` taillé pour un Raspberry Pi | Généraliser ; assistant de premier lancement ; secrets générés |
| Configuration | Un fichier `finances.toml` (ou variables), validé au démarrage par Pydantic, valeurs par défaut sûres ; réglages métier dans l'interface | Variables `FINANCES_*`, secrets sops | Fichier unique documenté ; écran « Réglages » |
| Secrets | sops + age, ou gestionnaire de secrets de l'hébergeur ; génération à l'installation | sops + age | Génération automatique, rotation guidée |
| Accès distant | Tailscale par défaut ; mode « exposé » durci et explicite (proxy TLS, WebAuthn obligatoire, limitation de débit) | Tailscale seul (inv. 7) | Mode exposé, désactivé par défaut |
| Mises à jour | Canaux stable / test, retour arrière automatique, migrations testées montée et descente | Déploiement nocturne avec retour arrière | Canaux, notes de version, version affichée |
| Supervision | Santé exposée (`/health` détaillé, protégé), signes de vie externes, paquet de diagnostic expurgé | Healthchecks, `/health` simple | Journal persistant, chien de garde, diagnostic exportable |
| Base de données | SQLite par instance ; couche d'accès portable vers PostgreSQL | SQLite (WAL) | Isoler les rares requêtes propres à SQLite |
| Fournisseurs | Banque (Enable Banking ou autre AISP), IA, notifications, sauvegarde : derrière des interfaces, choisis par configuration | Interfaces en place pour banque et IA | Notifications et sauvegarde derrière une interface |

### Industrialisation : tests et CI/CD (CDC §10.5)

| Contrôle | Outil | État |
| --- | --- | --- |
| Lint, format, types stricts | ruff, mypy strict | ✅ bloquant |
| Tests unitaires, propriétés, contrat, migrations | pytest, hypothesis, Alembic montée/descente | ✅ bloquant ; couverture des branches ≥ 95 % (relevée de 90 %) |
| Bout en bout | Playwright (Chromium derrière HTTPS), émulateur Android | ✅ bloquant |
| Secrets | gitleaks (historique complet), pre-commit | ✅ bloquant |
| Vulnérabilités des dépendances Python | pip-audit sur le lockfile | ✅ bloquant (ajouté le 06/10/2026) |
| Analyse statique de sécurité | ruff (règles bandit `S`), Semgrep (règles Python et sécurité) | ✅ bloquant (Semgrep ajouté le 06/10/2026) |
| Analyse des images | Trivy (vulnérabilités corrigibles hautes et critiques) | ✅ bloquant (ajouté le 06/10/2026) |
| Licences | pip-licenses (refus des licences contaminantes) | ✅ bloquant (ajouté le 06/10/2026) |
| Signature et nomenclature des images | cosign, SBOM (syft) | ⬜ |
| Tests de mutation | mutmut sur `rental`, `ledger`, `assistant` | ⬜ tâche périodique |
| Fuzzing de l'API | schemathesis sur l'API v1 | ⬜ |
| Restauration de bout en bout | sauvegarde → instance neuve → contrôles | 🟡 test hebdomadaire sur le Pi ; à ajouter en CI |
| Branche protégée | CI verte obligatoire avant fusion sur `main` | ⬜ réglage GitHub |
