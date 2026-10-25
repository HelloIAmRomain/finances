# Finances perso

Application personnelle de suivi financier. Elle regroupe mes comptes de plusieurs banques,
catégorise les dépenses, surveille les loyers d'un bien en location meublée détenu en indivision
50/50 et un remboursement de prêt, et prévient quand quelque chose cloche. On l'interroge en
langage naturel depuis le client MCP, l'application Android ou le navigateur.

Elle tourne sur un **Raspberry Pi 4 dédié**, à domicile. Elle n'est joignable que par
**Tailscale** et **ne peut déplacer aucun argent**.

## Principes

| Principe | En pratique |
| --- | --- |
| Lecture seule financière | Un client HTTP unique refuse toute requête absente d'une liste blanche par hôte, méthode et chemin ; un test échoue si un endpoint de paiement devient appelable |
| Aucune exposition Internet | Rien n'écoute sur une interface publique ; interface, API, serveur MCP et ntfy passent par Tailscale |
| Données brutes intactes | Une transaction importée n'est jamais modifiée (déclencheur SQL) ; catégories, loyers et liens passent par des tables de liaison |
| Montants exacts | Centimes entiers partout, arrondi unique en fin de calcul |
| Secrets chiffrés | sops + age ; données sensibles (IBAN, noms, données brutes) chiffrées en base (AES-GCM) |
| IA sous contrôle | Données pseudonymisées (noms en initiales, références masquées) ; écritures journalisées et annulables ; interrupteurs pour tout couper |
| Rejouable | Imports, tâches et alertes idempotents ; budget DSP2 de 4 synchros par compte et par 24 h respecté |

Les règles complètes sont dans [`CONTRIBUTING.md`](CONTRIBUTING.md) (invariants 1 à 11).

## Fonctionnalités

### Comptes et collecte
- Liaison bancaire DSP2 via **Enable Banking** (Boursorama, Société Générale…), consentement de
  90 jours, relance avant expiration, révocation en un clic.
- Synchro automatique (2 ou 4 fois par jour selon le compte) et manuelle, dans le budget DSP2.
- Import **CSV / OFX** de secours, avec aperçu avant import.
- Pour chaque compte : **nom libre**, statut **compté** (patrimoine, recettes, dépenses),
  **consultation seule** (compte joint, cartes, gestion pilotée) ou **ignoré**.
- Dédoublonnage entre sources, détection des **virements internes**, file **« À valider »** pour
  les cas douteux.

### Dépenses et analyse
- Catégories sur deux niveaux, **règles** de catégorisation (libellé, expression régulière,
  montant, sens, compte), règle proposée après chaque correction, ventilation d'une opération.
- **Tableau de bord** : patrimoine, recettes et dépenses du mois, taux d'épargne, comparaison
  N-1, dépenses par catégorie, flux sur 12 mois (Chart.js embarqué).
- Recherche multicritère et **export CSV**.

### Loyers et remboursements attendus
- Biens (quotes-parts d'indivision), baux et remboursements de prêt : échéances mensuelles,
  **suivi à partir d'un mois** (l'historique bancaire ne remonte qu'à 90 jours), modification
  du montant à partir d'un mois.
- **Rapprochement automatique** des paiements (IBAN ou motif du libellé), propositions à valider,
  paiements partiels, trop-perçus (report ou remboursement), rejets bancaires.
- Affectation manuelle par recherche (compte, période, libellé, montant), même pour une
  échéance ancienne.
- Alertes le **5** (retard) et le **10** (impayé) du mois, réglables par bail ; historique des
  incidents ; charges du bien (copropriété, taxe foncière, assurance…).

### Alertes et supervision
- Moteur d'alertes idempotent (clé de dédoublonnage), cycle ouverte / vue / résolue,
  notifications **ntfy** auto-hébergé sur le téléphone.
- Contrôles techniques : fraîcheur des synchros, consentements, disque, sauvegarde ; signes de
  vie **Healthchecks.io**.

### Intelligence artificielle
- **Client MCP de bureau** (application d'IA du PC, sur abonnement) : serveur **MCP** `/mcp` et extension `.mcpb`
  téléchargeable depuis l'application. **20 outils de lecture** : comptes, synthèse mensuelle,
  dépenses par catégorie avec comparaison, principaux postes, recherche, abonnements récurrents,
  patrimoine, loyers, file à valider… **20 outils d'écriture** : catégoriser, gérer catégories
  et règles, trancher « À valider », affecter les loyers, créer ou modifier un bail…
- **Outils d'analyse** : vue d'ensemble, contenu d'un compte (selon le rôle que je lui décris),
  flux entre comptes, prévision de trésorerie, anomalies, bilan locatif annuel.
- **Assistant intégré** (onglet Android et page web, **texte ou dictée**) : mêmes outils, par
  une API au choix (Anthropic, OpenAI, OVH AI Endpoints), avec un plafond mensuel ; réponses en
  Markdown, **réponse en direct** (étapes puis texte, sans attendre), **conversations mémorisées**
  (renommer, épingler en tête, supprimer), contexte de mes comptes fourni.
- **Garde-fous** :
  - données pseudonymisées, contrôle de sortie ;
  - jetons à portée lecture ou écriture, interrupteurs, quotas ;
  - aperçu puis confirmation au-delà de 20 opérations ;
  - page **« Modifications par l'IA »** avec annulation ;
  - jamais d'argent, d'accès ni d'envoi au locataire.

### Application Android
- Application **native** (Kotlin, Jetpack Compose) : tableau de bord, loyers (valider une
  proposition), comptes (synchro manuelle), opérations (recherche, classement), alertes,
  assistant.
- Appairage par code à usage unique ; jeton chiffré par le magasin de clés Android ;
  verrouillage par empreinte ; captures d'écran interdites ; HTTPS seulement.
- APK signé et publié par la CI à chaque commit de `main`.

### Sécurité
- Mot de passe (argon2) + second facteur **au choix** : code TOTP ou **clé de sécurité FIDO2**
  (YubiKey en USB-C ou NFC, WebAuthn).
- Sessions qui expirent, CSRF, CSP stricte sans script en ligne.
- Anti-force brute ; **journal d'audit** en ajout seul.
- Garde de démarrage : refus si une clé crypto figure dans les secrets.
- Procédure en cas de vol : [`docs/runbook-vol.md`](docs/runbook-vol.md).

### Exploitation
- Docker Compose (migrate, web, worker, ntfy), images arm64 construites par GitHub Actions.
- **Déploiement nocturne** avec retour arrière automatique.
- **Sauvegarde restic** toutes les 15 min vers Backblaze B2, chiffrée côté client, avec test de
  restauration automatique.
- Reconstruction complète de la machine par `deploy/bootstrap.sh`.

## Technologies

| Domaine | Choix |
| --- | --- |
| Serveur | Python 3.12, uv, FastAPI, Jinja2 + HTMX, Chart.js embarqué |
| Données | SQLite (WAL), SQLAlchemy 2, Alembic, Pydantic |
| Tâches | Planificateur maison + table `job_run` (reprise sans doublon) |
| Réseau sortant | httpx + tenacity derrière une liste blanche |
| Banques | Enable Banking (DSP2), import CSV / OFX |
| Sécurité | argon2, pyotp, webauthn (FIDO2), AES-GCM (`cryptography`), sops + age, gitleaks |
| IA | Protocole MCP (implémentation propre, conformité testée avec le SDK officiel), API Anthropic / OpenAI / OVH ; extension MCP en Node sans dépendance |
| Mobile | Kotlin, Jetpack Compose, Material 3, OkHttp, kotlinx.serialization, Robolectric |
| Qualité | pytest, hypothesis, pytest-cov (≥ 90 %, ~97 % aujourd'hui), Playwright (Chromium derrière HTTPS), ruff, mypy strict, pre-commit |
| Hébergement | Raspberry Pi 4, Debian, Docker Compose, Tailscale, ntfy, restic + Backblaze B2, Healthchecks.io, Renovate |

## Architecture

```
 Téléphone (app Android, ntfy)   PC (navigateur, client MCP + extension)
              \                          /
               ───────  Tailscale  ──────            (aucun port ouvert sur Internet)
                          │
        Raspberry Pi ─ Docker Compose
          ├─ web     FastAPI : pages, API v1, /mcp, assistant
          ├─ worker  planificateur : synchros, rapprochement, alertes, contrôles
          ├─ ntfy    notifications
          └─ SQLite (WAL) ── restic ──► Backblaze B2 (chiffré)
                          │
          sorties autorisées seulement : Enable Banking (lecture), Healthchecks,
          fournisseur d'IA choisi (génération de texte)
```

## Arborescence

```
app/          connectors, ledger, categorize, rental, alerts, assistant, ops, net, web, models
migrations/   Alembic (0001 à 0012)
tests/        tests unitaires, scénarios, contrat de l'API, e2e (Chromium), relais MCP (Node)
android/      application Kotlin / Compose
deploy/       compose, bootstrap, secrets chiffrés, unités systemd
docs/         cahier des charges, dossier technique, plan, exploitation, spécifications
```

## Développement

Prérequis : [uv](https://docs.astral.sh/uv/) ; Docker pour les tests de bout en bout et Android.

```sh
uv sync                        # Python 3.12 et dépendances verrouillées
uv run pre-commit install      # une fois : gitleaks, ruff, mypy à chaque commit
uv run pytest                  # tests (HYPOTHESIS_PROFILE=ci pour le profil exhaustif)
uv run pytest --cov=app --cov-branch
uv run ruff check . && uv run ruff format --check . && uv run mypy
scripts/e2e.sh                 # bout en bout dans l'image Playwright
node --test tests/extension/   # relais de l'extension MCP
```

Lancer en local, avec des secrets de test et une base migrée :
`uv run uvicorn --factory app.web.main:create_app --port 8000` et
`uv run python -m app.ops.worker`. Administration : `uv run python -m app.cli --help`.
L'application Android se construit dans l'image SDK (commande dans `CONTRIBUTING.md`).

## Documentation

| Document | Contenu |
| --- | --- |
| [`docs/cahier-des-charges.md`](docs/cahier-des-charges.md) | Exigences (COL, ANA, LOC, FIS, ALR, UI, NF), lots, scénarios d'acceptation |
| [`docs/dossier-technique.md`](docs/dossier-technique.md) | Choix techniques, architecture, sécurité, modèle de données |
| [`docs/PLAN.md`](docs/PLAN.md) | Décisions, tâches, journal d'avancement |
| [`docs/exploitation.md`](docs/exploitation.md) | Secrets, sauvegardes, déploiement, reconstruction, YubiKey, client MCP, assistant, Android, SSD |
| [`docs/spec-mcp.md`](docs/spec-mcp.md) | Pilotage par IA : outils, pseudonymisation, garde-fous |
| [`docs/regles-loyer.md`](docs/regles-loyer.md) | Machine à états d'une échéance de loyer |
| [`docs/runbook-vol.md`](docs/runbook-vol.md) | Que faire si la machine ou le téléphone est volé |
| [`docs/feuille-de-route.md`](docs/feuille-de-route.md) | Fonctionnalités proposées et calendrier |

## Suite prévue

Lot 3 : catégorisation par IA des nouvelles opérations, budgets, anomalies, prévision de
trésorerie. Lot 4 : IRL, quittances PDF, indivision et récapitulatifs fiscaux, exports.
