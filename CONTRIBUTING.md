# Finances perso — règles de contribution

Système personnel qui centralise des comptes (plusieurs banques ; la crypto est hors périmètre),
suit les dépenses et surveille les loyers d'un bien en location meublée détenu en indivision.
Un seul utilisateur administrateur, hébergé à domicile sur un Raspberry Pi 4. **Vision long terme
(Q24, CDC §10)** : le proposer à d'autres foyers, une instance par client, installée partout
(on-prem ou cloud). Tout ce qui est construit doit le permettre.

## Documents de référence (à lire avant toute tâche structurante)

- `docs/cahier-des-charges.md` : exigences fonctionnelles (IDs COL-, ANA-, LOC-, FIS-, ALR-, UI-, NF-), lots, scénarios d'acceptation.
- `docs/dossier-technique.md` : choix techniques, architecture, modèle de données, plan de réalisation.
- `docs/regles-loyer.md` : machine à états d'une échéance de loyer (le schéma n'est pas dans l'export Markdown).
- `docs/PLAN.md` : plan d'implémentation courant et avancement (tenu à jour à chaque tâche).
- `docs/spec-mcp.md` : pilotage par IA (serveur MCP, pseudonymisation, écritures annulables).
- `docs/conformite.md` : registre de conformité (RGPD, DSP2, fiscalité, droit locatif, IA).
- `docs/feuille-de-route.md` : fonctionnalités proposées et calendrier.
- `docs/charte-ergonomie.md` : charte d'ergonomie et de rédaction (principe de premier rang), glossaire.

En cas de contradiction entre un document et une demande, la signaler avant de coder.

## Invariants — ne jamais enfreindre

1. **Lecture seule financière.** Aucun code ne doit pouvoir initier un paiement, un virement, un ordre ou un retrait.
   N'utiliser que des endpoints de lecture. Tout appel sortant passe par l'unique client HTTP de l'app, qui refuse
   toute requête absente d'une liste blanche explicite par hôte, méthode et chemin (Enable Banking : seuls `POST /auth`,
   `POST /sessions` et `DELETE /sessions/{id}` — révocation du consentement — en écriture).
   Un test échoue si un endpoint de paiement est appelable.
   La crypto est hors périmètre (29/09/2026) : l'app refuse de démarrer si une clé crypto figure
   dans les secrets. Si des connecteurs crypto reviennent, elle vérifiera leurs droits au démarrage
   et refusera toute clé capable de trader ou de retirer.
2. **Montants en centimes entiers** (`int`). Jamais de `float` pour de l'argent. Arrondi unique, au centime, en fin de calcul.
3. **Idempotence.** Toute tâche planifiée ou tout import peut être rejoué sans créer de doublon
   (transactions, allocations, alertes via `dedupe_key` unique, exécutions via `job_run`).
4. **Données brutes intactes.** On ne modifie jamais une transaction importée ; catégories, loyers,
   indivision passent par des tables de liaison.
5. **Pas de secret en clair.** Jamais de clé, jeton, IBAN réel ou donnée personnelle dans le code, les tests,
   les fixtures, les logs ou les commits. Secrets chiffrés avec sops + age (`deploy/secrets.enc.yaml`, dans un dépôt privé à part :
   `docs/exploitation.md`, section Secrets).
   Fixtures et cassettes d'API toujours anonymisées.
6. **Aucun envoi au locataire sans validation** explicite (sauf option quittance auto, désactivée par défaut).
7. **Pas d'exposition Internet.** Rien n'écoute sur une interface publique ; accès via Tailscale uniquement.
8. **Données envoyées à l'IA pseudonymisées** : libellé nettoyé, montant, date. Jamais d'IBAN, de numéro,
   ni de nom de personne en toutes lettres (initiales seulement) ; les noms de commerçants et d'organismes restent.
   Les écritures faites par l'IA passent par les services audités, sont journalisées et annulables (`docs/spec-mcp.md`).
9. **Instants en UTC** en base (`*_at`, datetime avec fuseau), affichage Europe/Paris. Les dates calendaires
   (date comptable, date de valeur, échéance) sont des `DATE` sans fuseau, jamais converties.
10. **DSP2 : 4 synchros max par compte et par 24 h.** Le budget compte chaque **requête** envoyée à la banque
    (reprises, rattrapages, pages et synchros manuelles compris), par compte et par endpoint, sur 24 h glissantes,
    avec un plafond par banque (`app/connectors/bank_profiles.py` : **hypothèse** à ajuster d'après les limites
    observées, `python -m app.cli limites-observees`). Une synchro manuelle consomme ce budget (en-têtes PSU
    inutilisables derrière Tailscale) : une par 15 min et par compte au plus, refusée s'il ne reste que de quoi
    faire la prochaine synchro automatique. Un 429 ou un refus de quota n'est jamais retenté avant le créneau
    suivant et ne compte pas comme une panne ; l'alerte ne part qu'après 24 h sans synchro réussie.
11. **Hôte dédié.** Le système ne partage jamais une machine avec des clés capables de trader ou de retirer
    (bot de trading compris). Aucun conteneur ne monte le socket Docker ni ne tourne en `privileged`.

## Principes produit (CDC §10) — à appliquer à chaque changement

- **Intuitif avant tout (principe de premier rang)** : tout doit se comprendre et s'utiliser sans
  documentation ni connaissance financière ou technique — écrans, mots, parcours et fonctionnement
  général. Chaque page dit à quoi elle sert ; chaque nombre dit ce qu'il mesure et à quelle date ;
  boutons en verbes précis ; erreurs avec la marche à suivre ; états vides qui guident ; même mot
  pour la même chose (glossaire) ; aucun jargon ni sigle inexpliqué. Règles et contrôle de fin de
  tâche : `docs/charte-ergonomie.md`. Une fonctionnalité juste mais incompréhensible n'est pas
  terminée.
- **Rien de propre à un client dans le code** : banques, catégories, fuseau, échéances, quotas sont
  des données ou de la configuration, avec des valeurs par défaut sûres (PROD-01).
- **Architecture en couches** : domaine pur (sans base ni réseau) → services → adaptateurs
  (banques, IA, notifications, stockage) → interfaces (web, API, MCP). Pas de dépendance à rebours.
- **Mise en place et mise à jour sans friction** : toute nouvelle option a une valeur par défaut,
  est validée au démarrage avec un message clair, et apparaît dans la documentation d'exploitation.
- **Interface intuitive** : messages d'erreur en français clair avec la marche à suivre, états
  vides utiles, pas de jargon technique dans l'interface.
- **Conformité** : toute donnée personnelle nouvelle → durée de conservation, export, effacement,
  entrée dans `docs/conformite.md` ; tout calcul fiscal → source citée et mention « indicatif ».
- **Qualité** : chaque bug corrigé laisse un test ; couverture des branches ≥ 95 % ; CI verte
  (sécurité comprise) avant tout déploiement.

## Stack

Python 3.12 · uv (lockfile) · FastAPI · Jinja2 + HTMX · Chart.js embarqué (pas de CDN) · SQLAlchemy 2 + Alembic ·
Pydantic · SQLite (WAL, foreign_keys, busy_timeout) · planificateur maison + table `job_run` (pas d'APScheduler) · httpx + tenacity ·
rapidfuzz · PDF maison (`app/pdf.py`) · webauthn · API IA (Anthropic, OpenAI, OVH) par le client HTTP unique · serveur MCP maison ·
pytest + hypothesis · ruff · mypy (strict). Android : Kotlin + Jetpack Compose. Extension MCP pour le PC : Node sans dépendance.
Déploiement : Docker Compose (migrate, web, worker, ntfy), images arm64 construites par GitHub Actions.
Sauvegarde : restic toutes les 15 min vers B2, chiffré côté client (pas de Litestream).

## Arborescence

```
app/
  connectors/   enable_banking, file_import (CSV, OFX), budget DSP2, requêtes bancaires
  ledger/       comptes, transactions, dédoublonnage, virements internes
  categorize/   règles, IA, file à valider
  assistant/    outils IA (MCP) : lecture, écriture annulable, pseudonymisation
  rental/       biens, baux, échéances, rapprochement, IRL, indivision, quittances
  alerts/       moteur de règles, cycle de vie, envoi ntfy / e-mail
  tax/          récapitulatifs, exports
  auth/         administrateur, mots de passe et TOTP, sessions, clés de sécurité, appareils
  web/          pages, API JSON v1 (application Android), CSRF, messages scellés
  ops/          planificateur, santé des connecteurs, pings Healthchecks
  net/          client HTTP unique à liste blanche (hôte, méthode, chemin)
migrations/     Alembic
tests/
  scenarios/    scénarios en YAML (loyer, dédoublonnage)
  cassettes/    réponses d'API enregistrées et anonymisées
  e2e/          bout en bout : Chromium derrière un relais HTTPS (comme tailscale serve)
android/        application native Kotlin + Jetpack Compose (API JSON v1)
deploy/         compose.yml, bootstrap.sh, update.sh, sync-secrets.sh, set-secret.sh, systemd/
scripts/        e2e.sh (tests de bout en bout dans l'image Playwright)
```

## Commandes

- installer : `uv sync`
- tests : `uv run pytest`
- lint / format / types : `uv run ruff check . && uv run ruff format --check . && uv run mypy`
  (mypy strict sur `app` et `tests`, configuré dans `pyproject.toml`)
- tests exhaustifs (profil CI d'hypothesis) : `HYPOTHESIS_PROFILE=ci uv run pytest`
- lancer en local (secrets de test dans un répertoire, `FINANCES_DATABASE_PATH` pointant sur une base migrée) :
  `uv run uvicorn --factory app.web.main:create_app --host 127.0.0.1 --port 8000` (web),
  `uv run python -m app.ops.worker` (worker) ; administration : `uv run python -m app.cli --help`
- couverture (≥ 95 % exigés en CI) : `uv run pytest --cov=app --cov-branch`
- sécurité (comme la CI) : `uv run pip-audit` (vulnérabilités), `uv run pip-licenses` (licences)
- relais de l'extension MCP : `node --test tests/extension/*.test.js`
- bout en bout (Chromium) : `scripts/e2e.sh` (image Playwright), ou `uv run pytest tests/e2e --e2e`
- Android (dans l'image SDK) : `cd android && docker run --rm -u "$(id -u):$(id -g)" -e GRADLE_USER_HOME=/p/.gradle-home
  -e HOME=/p/.gradle-home -v "$PWD:/p" -w /p ghcr.io/cirruslabs/android-sdk:35 ./gradlew testDebugUnitTest lintDebug assembleRelease`
- sur le Pi : `sudo /srv/finances/deploy/compose.sh <commande compose>` (image déployée),
  `sudo systemctl start finances-update.service` (déploiement immédiat)

## Méthode de travail

- Avancer **lot par lot** (voir `docs/PLAN.md`) et par petites tâches livrables, chacune avec ses tests.
- Pour la logique métier (rapprochement, alertes, indivision, IRL) : **tests d'abord**,
  à partir des scénarios du cahier des charges, puis le code.
- Aucun appel réseau réel dans les tests : cassettes anonymisées ou faux connecteurs.
- Avant de terminer une tâche : contrôle de la charte d'ergonomie (§6) ; tests, ruff et mypy au vert ; `docs/PLAN.md` mis à jour ;
  exigences couvertes citées par leur ID dans le message de commit.
- Commits en français, format Conventional Commits (`feat(rental): rapprochement partiel LOC-04`).
- **Jamais de poussée sur `main`** (ni commit direct, ni `git push … main`, ni fusion locale) :
  toute modification passe par une branche, puis une PR fusionnée seulement CI verte. Garde-fous :
  hooks pre-commit `no-commit-to-branch` et refus de pousser vers `main`, job CI « provenance »
  (un commit de `main` sans PR fusionnée ne produit aucune image, donc rien n'est déployé).
- Une décision non couverte par les documents se tranche avant de coder, pas par supposition.
- Ne jamais toucher aux fichiers de secrets déchiffrés ni à `~/.config/sops`.
