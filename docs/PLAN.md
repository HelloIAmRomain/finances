# PLAN — Étape 0 (infrastructure) et Lot 1 (socle)

> **Statut : v1 — 27/09/2026.** Questions Q1 à Q10 tranchées (section 2).
> Les choix marqués « *(défaut proposé)* » s'appliquent sauf décision contraire.
> Les modifications de documents de la section 1.4 sont validées et appliquées.

Légende d'avancement : `[ ]` à faire · `[~]` en cours · `[x]` fait · `[-]` abandonné.

---

## 1. Incohérences, manques et risques relevés

### 1.1 Bloquants ou structurants pour l'étape 0 / le lot 1

| # | Constat | Documents | Impact | Proposition |
|---|---|---|---|---|
| I-01 | Le découpage en lots du CDC (§6) est un schéma non exporté. Le contenu des lots n'existe que dans le DT §10. | CDC §6, DT §10 | Pas de référence unique pour le périmètre du lot 1 | Reprendre ici le périmètre du lot 1 (section 3.2) et l'ajouter au CDC sous forme de tableau (comme `regles-loyer.md`) |
| I-02 | Le CDC dit que « les lots 1 et 2 couvrent toutes les exigences M ». Le DT place pourtant **FIS-06** (M, exports) au lot 4. **LOC-10** (M, charges du bien), **ANA-11** (M, recherche), **ALR-02** (M, deux canaux dont l'e-mail) et **UI-01/UI-05** (M) ne figurent dans aucun lot. | CDC §4 et §6, DT §10 | Exigences M orphelines | Lot 1 : ANA-11, ALR-02 (ntfy + e-mail), UI-01, UI-05, export CSV des transactions (volet minimal de FIS-06). Lot 2 : LOC-10 |
| I-03 | **L'authentification (NF-SEC-04, mot de passe + 2FA) n'apparaît dans aucun lot**, alors que le lot 1 livre une interface web. | CDC §7, DT §10 | Interface sans authentification, même derrière Tailscale | L'ajouter au lot 1 (T1.22) |
| I-04 | **NF-SEC-03 (« base chiffrée ») n'est pas tenu** : SQLite est stocké en clair sur le SSD. Et la clé age est « présente uniquement sur la machine » : voler le SSD revient à voler la clé qui déchiffre les secrets. La mesure « Vol de la machine → sops + age » du DT §6 ne protège donc que le dépôt Git, pas la machine. | CDC NF-SEC-03, DT §1 et §6 | Promesse de sécurité non tenue | **Tranché (Q3)** : pas de LUKS ; NF-SEC-03 reformulé dans le CDC (risque de vol physique accepté) |
| I-05 | Le chiffrement de la **réplique Litestream** vers B2 n'est pas prévu (seuls les instantanés restic sont « chiffrés côté client »). Selon la version, Litestream propose ou non le chiffrement age des répliques : **à vérifier sur la version retenue**. | DT §1, §6 et §7 | Base en clair chez un tiers, contraire à NF-SEC-03 | **Clos (Q11)** : Litestream abandonné ; restic chiffre côté client |
| I-06 | Chiffrer au niveau applicatif les IBAN et les noms (DT §6 et §8) est **contredit par** la table `raw_payload`, qui garde le JSON brut en clair avec ces mêmes IBAN et noms, et par les colonnes contrepartie de `transaction`. Le rapprochement du lot 2 par IBAN émetteur demande en plus un index de recherche sur un champ chiffré. | DT §6 et §8, CDC COL-08 et §5 | Chiffrement de façade | **Tranché (Q3)** : chiffrement applicatif de `raw_payload.content`, des IBAN et des noms, avec un blind index HMAC pour la recherche. Les libellés restent en clair pour la recherche (ANA-11), même s'ils contiennent parfois un nom : risque accepté |
| I-07 | **Dédoublonnage par empreinte (date + montant + libellé) sous index unique** : deux achats identiques le même jour (deux cafés à 2,50 €) sont des transactions distinctes, et la seconde serait perdue. | CDC COL-03, DT §8 | Perte silencieuse de données | Clé `dedupe_key` = `ext:{external_id}`, sinon `fp:{empreinte}:{rang d'occurrence dans le lot source}` ; index unique (`account_id`, `dedupe_key`) |
| I-08 | **Dédoublonnage entre sources** : la même opération reçue par l'API puis par un import CSV (historique au-delà des 90 jours DSP2, ou secours) n'a ni le même identifiant ni le même libellé. L'empreinte ne la reconnaît pas. | CDC COL-02, COL-03, §3 ; DT §3 | Doublons entre API et fichiers | **Tranché (Q5)** : approche hybride, voir T1.16a et T1.16b. Clés d'ingestion propres à chaque source (`ext:<source>:…`, `fp:<source>:…`) pour que deux sources ne se confondent jamais avant `duplicate_link` |
| I-09 | **Transactions « en attente » (pending) des banques** : elles changent ou disparaissent avant la comptabilisation. Les conserver contredit l'invariant 4 (on ne modifie jamais une transaction importée). | CONTRIBUTING.md inv. 4, DT §3 | Doublons ou montants faux | *(défaut proposé)* On n'ingère que les transactions comptabilisées (`BOOK`). Les « en attente » sont gardées dans `raw_payload` et ne sont pas affichées |
| I-10 | **Plafond DSP2 et reprises** : les reprises de tenacity et les rattrapages après coupure sont de vrais appels à la banque. Compter les synchros *réussies* ne protège pas le plafond de 4 par 24 h. | CONTRIBUTING.md inv. 10, DT §3 et §4 | Dépassement, blocage ou résiliation côté banque | Compter chaque **requête** envoyée à la banque, par compte, sur 24 h glissantes, dans une table `provider_call`. Refus au-delà du budget, test de propriété (T1.6) |
| I-11 | Le CDC (§3) prévoit une synchro **quotidienne** pour les comptes et **4 par jour** pour le compte des loyers. Le DT (§3) dit « 4 synchros par jour » sans préciser pour quels comptes. | CDC §3, DT §3 | Ambiguïté | *(défaut proposé)* Fréquence réglable par compte : 4 par jour pour les **deux** comptes d'encaissement (SG perso et Boursorama joint, Q6), 2 par jour pour les autres |
| I-12 | La **synchro manuelle** (bouton « Synchroniser ») est une action de l'utilisateur. La DSP2 ne la plafonne pas si les en-têtes PSU sont transmis. L'invariant 10 ne dit pas si elle compte dans le plafond. | CONTRIBUTING.md inv. 10 | Règle à préciser | ✅ **Révisé le 28/09/2026** : les en-têtes PSU ne sont pas fiables derrière Tailscale (IP privée), la synchro manuelle **consomme le budget** (toujours marquée `unattended=false`). Une toutes les 15 min par compte au plus, refusée s'il ne reste que de quoi faire la prochaine synchro automatique |
| I-13 | **Dates** : l'invariant 9 (« dates en UTC ») ne vaut que pour les *instants*. Une date comptable ou une échéance est une date calendaire : la convertir en UTC décalerait le jour. | CONTRIBUTING.md inv. 9 | Bogues de décalage d'un jour | Colonnes `DATE` naïves pour les dates calendaires, `DATETIME` UTC pour les instants. Nuance à ajouter à CONTRIBUTING.md |
| I-14 | **APScheduler « avec stockage en base » + `job_run`** : le jobstore SQLAlchemy d'APScheduler 3 sérialise les tâches avec pickle et fait doublon avec `job_run`. APScheduler 4 est encore en pré-version. | DT §4 | Deux sources de vérité | ✅ Planificateur maison (Q13), tâches déclarées dans le code. `job_run` est la **seule** source de vérité pour l'idempotence et le rattrapage |
| I-15 | **Modèle de données incomplet pour le lot 1** : il manque l'historique des soldes (O1, patrimoine net), le mapping d'import par banque (COL-02), les lots d'import, l'état de synchro par compte, le budget DSP2, la file d'envoi des notifications et les utilisateurs et sessions (NF-SEC-04). | DT §8 | Migration 0001 incomplète | Tables ajoutées en section 5 |
| I-16 | La table `transaction` porte un nom réservé en SQL. | DT §8 | Guillemets partout, source d'erreurs | La nommer `ledger_transaction` |
| I-17 | **COL-02 exige l'import QIF**, mais le DT ne prévoit que `csv` et `ofxparse`. `ofxparse` est par ailleurs peu maintenu et sans types (mypy strict). | CDC COL-02, DT §4 | Exigence non couverte | **Tranché (Q7)** : QIF retiré de COL-02 (CDC modifié) |
| I-18 | **Ports Docker** : publier `8000:8000` expose le port sur toutes les interfaces, et Docker contourne le pare-feu (ufw). | CONTRIBUTING.md inv. 7 | Exposition sur le réseau local | Publier uniquement sur `127.0.0.1`, et `tailscale serve` pointe vers le port local. Un test CI vérifie `compose.yml` |
| I-19 | **Notifications ntfy « uniquement via Tailscale »** : sans Firebase, le téléphone doit garder Tailscale actif en permanence. Sur iOS, il faut en plus passer par le relais ntfy.sh. Si Tailscale est coupé sur le téléphone, les alertes critiques sont perdues en silence. | DT §1 et §5 | O6 compromis | **Tranché (Q8)** : Android, Tailscale toujours actif sur le téléphone. Une alerte critique non vue sous 30 min est renvoyée par e-mail |
| I-20 | **Canal e-mail** (ALR-02, M) : ni le fournisseur SMTP ni l'expéditeur ne sont définis. | CDC ALR-02, DT | Canal non implémentable | **Tranché (Q8)** : SMTP de la messagerie de l'utilisateur avec mot de passe d'application, stocké dans sops |
| I-21 | **Déploiement nocturne et rollback** : restaurer un « instantané » SQLite pendant que Litestream réplique crée une rupture de génération côté réplique. Une migration qui supprime une colonne empêche aussi de revenir à l'image précédente sans restaurer la base. | DT §5 | Rollback cassé ou perte de données | **Simplifié (Q11)** : sans Litestream, plus de rupture de génération. Instantané local avant migration, restauration en cas d'échec, migrations expand/contract (T0.11) |
| I-22 | **Automerge Renovate + déploiement nocturne automatique** : une dépendance compromise publiée en version mineure atteindrait la machine en moins de 24 h. | DT §5, NF-SEC-08 | Risque de chaîne d'approvisionnement sur des données financières | Délai `minimumReleaseAge` de 7 jours avant tout automerge ; actions GitHub et images épinglées par condensat (digest) |
| I-23 | **Build arm64 en CI** : la disponibilité et la gratuité des runners arm64 hébergés par GitHub pour un **dépôt privé** sont à vérifier. Le DT le signale (« à surveiller »). | DT §11 | Coût ou lenteur | Runner arm64 natif s'il est disponible, sinon buildx + QEMU sur runner x86 (lent mais gratuit). Image construite seulement sur `main` et sur les étiquettes |
| I-24 | **Litestream et deux processus** (web et worker) sur le même fichier SQLite dans deux conteneurs : c'est possible (même noyau, volume partagé), à condition qu'aucun des deux ne force de checkpoint `TRUNCATE`. | DT §2 | Corruption ou réplique incohérente | **Sans objet (Q11)** : plus de Litestream ; WAL conservé pour web + worker, l'instantané passe par l'API backup de SQLite |
| I-25 | **Les secrets déchiffrés sur disque** contredisent NF-SEC-02 (« chiffrés au repos »). `bootstrap.sh` « déchiffre les secrets » sans dire où. | DT §5, NF-SEC-02 | Secrets en clair sur le SSD | Déchiffrement au démarrage vers un tmpfs (`/run/finances/`, 0600), jamais dans le dépôt ni sur le SSD |
| I-26 | Le contrôle des droits des clés crypto au démarrage (invariant 1) concerne des connecteurs du lot 4. | CONTRIBUTING.md inv. 1, DT §10 | — | Lot 1 : un « garde de démarrage » générique qui refuse de démarrer si une clé crypto est configurée sans vérificateur. Les vérifications propres à chaque plateforme crypto se branchent au lot 4 |
| I-27 | L'invariant 1 dit « n'utilise que des endpoints de lecture ». Or Enable Banking impose des `POST` non financiers (`/auth`, `/sessions`). | CONTRIBUTING.md inv. 1 | Règle impossible à appliquer au pied de la lettre | **Validé** : liste blanche explicite **par hôte, méthode et chemin**. Enable Banking : `POST` limité à `/auth` et `/sessions`, `DELETE` limité à `/sessions/{id}` (révocation du consentement, runbook de vol) ; `GET` limité aux chemins de lecture listés en T1.7. Un client HTTP unique filtre chaque requête avant l'envoi et refuse tout le reste. Un test échoue si un endpoint de paiement (`/payments…`) est appelable (T1.7) |
| I-29 | **Stockage du Pi : clé USB SanDisk Cruzer Switch (64 Go, USB 2.0), pas un SSD.** Endurance d'écriture comparable à une carte SD, exactement ce que le DT exclut (§1, §5) et le risque « Panne matérielle » du CDC §8. SQLite en WAL + Litestream écrivent en continu. Environ 360 Mo écrits dans les 23 premières minutes après le démarrage | DT §1, §5 ; CDC §8 | Usure et corruption, perte de la machine | ✅ **Décision du 28/09/2026 : SSD recommandé, pas obligatoire** (CDC, DT mis à jour). Risque accepté : sauvegarde toutes les 15 min, reconstruction en une heure ; la reconstruction de T0.15 se fait sur un support vierge quelconque. *Analyse initiale :* la clé sert de banc d'essai pour T0.13 ; un vrai SSD USB 3 sur un port bleu est à acheter |
| I-30 | **Le Pi tourne Raspberry Pi OS avec bureau** (Debian 13 « trixie », `graphical.target`, lightdm, labwc) alors que le DT prévoit la version Lite. `rpcbind` et `nfs-blkmap` écoutent sur toutes les interfaces (port 111) ; aucun pare-feu ; SSH accepte les mots de passe ; `sudo` sans mot de passe pour `raspi` | DT §5 ; inv. 7 | RAM et surface d'attaque inutiles | Réinstaller **Pi OS Lite 64 bits** sur le SSD, puis tout durcir par `bootstrap.sh` (T0.13), jamais à la main : SSH par clé seulement, `rpcbind` désactivé, pare-feu (tout refusé en entrée sauf SSH depuis le LAN et `tailscale0`), chien de garde, `unattended-upgrades` |
| I-31 | **Litestream 0.5.x (version maintenue, 0.5.17 au 31/08/2026) refuse le chiffrement age des répliques** : « age encryption is not currently supported, if you need encryption please revert back to Litestream v0.3.x » (`cmd/litestream/main.go`). Une réplique à jour serait en clair chez B2 | DT §1, §7 ; CDC NF-SEC-03 (reformulé : répliques chiffrées côté client) | Contradiction bloquante pour T0.9, et structurante pour T0.11 et T0.12 | **Tranché (Q11)** : restic seul, toutes les 15 min (section 1.5) |
| I-32 | **Le Pi est connecté en Wi-Fi** (`wlan0`) | O6, NF-RES-04 | Coupures silencieuses plus probables qu'en filaire | ✅ **Facultatif** (28/09/2026) : le Wi-Fi suffit pour quelques synchros par jour ; Healthchecks détecte un silence |
| I-28 | Git : la branche courante est `master`, mais la branche principale attendue est `main`. Il n'y a encore aucun commit. | — | — | Premier commit directement sur `main` (T0.1) |

### 1.2 Relevés pour le lot 2 (non bloquants, à trancher avant le lot 2)

- **L-01** — `regles-loyer.md` dit que l'état est *dérivé* des allocations et de la date. Pourtant la transition « PAYE → ATTENDU sur virement rejeté » et le scénario du CDC « retour à Attendu » sont faux si la date limite est passée : l'état dérivé sera `EN_RETARD` ou `IMPAYE`. Il faut reformuler : « l'échéance est recalculée ; alerte critique ».
- **L-02** — La fenêtre de rapprochement va du 25 du mois précédent à la fin du mois. Un paiement tardif le 5 de M+1 pour un loyer M impayé tombe **hors fenêtre**, et pourrait être affecté à tort à M+1. Proposition : affecter d'abord à l'échéance impayée la plus ancienne, la fenêtre ne servant qu'aux paiements anticipés.
- **L-03** — La `dedupe_key` `rent:{due_id}:EN_RETARD` bloque toute nouvelle alerte si l'échéance repasse en retard après un rejet. Il faut y ajouter un numéro d'épisode.
- **L-04** — Les incidents (LOC-14, retards répétés sur 6 mois) sont « tracés dans `lease` », mais aucune table ne les porte. Il faut une table `rent_incident`.
- **L-05** — Rapprochement faible « en attente de validation » : `rent_allocation` a besoin d'un statut (`proposed` / `confirmed`). Seules les allocations confirmées comptent dans l'état.
- **L-06** — Trop-perçu : il faut plafonner l'allocation au montant dû et porter l'excédent sur une ligne distincte (report ou remboursement).
- **L-07** — Le CDC dit « J+10 après l'échéance », `regles-loyer.md` dit « J+10 après la date limite ». Ces deux termes doivent désigner la même date ; à écrire explicitement.
- **L-08** — Rôle d'un co-indivisaire (UI-04) : les règles parlent d'« un seul utilisateur administrateur ». S'il a un accès, il devra être sur le tailnet (partage de nœud Tailscale). La table `app_user` prévoit un rôle dès le lot 1, ce qui ne coûte rien.
- **L-09** — Crypto (lot 4) : les quantités (8 à 18 décimales) ne sont pas des montants en centimes. Il faudra un type « quantité en plus petite unité » (entier) distinct de `Cents`.
- **L-10** — **Deux loyers sur deux comptes** (Q6) : un sur un compte personnel, un sur un compte joint, dans deux banques différentes. Or le CDC (§1, LOC-01) décrit **un** bien avec **un** compte d'encaissement. À clarifier avant le lot 2 : s'agit-il de deux biens, de deux baux sur le même bien, ou d'un même loyer qui a changé de compte ? **Principe validé (27/09/2026)** : le compte d'encaissement est porté par le **bail**, pas par le bien (CDC LOC-01/LOC-02 et DT §8 mis à jour). La question des deux loyers reste ouverte pour le lot 2.
- **L-11** — Compte joint : le mode restreint d'Enable Banking le couvre si l'utilisateur en est cotitulaire (DT §3). Si l'autre titulaire est un tiers (un co-indivisaire ou une autre personne), ses opérations personnelles entreront dans le système : à prendre en compte pour UI-04 et NF-DATA-01.
- **L-12** — Report lors d'un dédoublonnage (décision T1.16b) : la même règle s'appliquera aux `rent_allocation` du lot 2. Les allocations de la transaction doublon passent sur la canonique ; en cas d'allocations manuelles différentes, on garde celles de la canonique et on ouvre un conflit ; l'annulation restaure l'état exact.
  - *Mise en œuvre retenue (T2.5)* : pour les loyers, l'affectation **reste** sur le virement d'origine, même reconnu ensuite comme doublon ; la ligne canonique n'est pas réaffectée. Toute lecture des loyers encaissés compte donc les affectations confirmées **sans** filtre « effective » (le récapitulatif fiscal l'appliquait et perdait ces loyers, corrigé le 07/10/2026).

### 1.3 Risques projet

| Risque | Parade dans ce plan |
|---|---|
| Matériel ou comptes externes (B2, Healthchecks, Enable Banking, GitHub) pas prêts : l'étape 0 bloque tout | Étape 0 scindée : **0A** (dépôt, CI, images, tests locaux, sans matériel) puis **0B** (machine et services) |
| Enable Banking ne couvre pas Boursorama ou la Société Générale, ou pas tous leurs types de comptes | Vérification dans `GET /aspsps` dès la création de l'application (T1.10) ; import de fichier dès le lot 1 (profils CSV des deux banques, T1.14) |
| Cassettes Enable Banking à enregistrer depuis de vrais comptes, donc risque de fuite | Script d'anonymisation + test CI qui cherche des IBAN valides ou des noms dans `tests/cassettes/` (T1.8) |
| Porte du lot 1 (« 14 jours sans intervention ») : 2 semaines calendaires incompressibles | Démarrer l'observation dès que la synchro réelle tourne, et avancer en parallèle sur les tests du lot 2 (sans code de production) |
| **Cohabitation temporaire avec un bot de trading.** Décision Q1 : un tel bot tourne sur une **machine séparée**, et le système finances ne partage jamais un hôte avec des clés capables de trader (CONTRIBUTING.md, inv. 11). Si une cohabitation temporaire devait malgré tout arriver (panne de la machine du bot, transition), la garantie « compromission = données, jamais argent » **ne tient plus** : un root sur l'hôte lit tout | Cohabitation bornée dans le temps, avec une date de fin notée dans ce fichier. **Isolement minimal** : utilisateurs système distincts (`finances` et `bot`, sans groupe commun ni `sudo`) ; secrets du bot illisibles pour l'utilisateur `finances` et ses conteneurs (0600, hors des volumes montés) ; **aucun socket Docker monté** dans aucun conteneur, vérifié par le test de T0.8 ; pas de backtest sur la machine ; projets Compose et réseaux Docker séparés. Ce risque résiduel doit être accepté explicitement avant toute cohabitation |
| Mémoire du Pi 4 (4 Go, dédié) | Plafonds par conteneur (T0.8) ; dépendances lourdes (WeasyPrint, SDK Anthropic) ajoutées seulement aux lots qui les utilisent |

### 1.4 Modifications de documents (validées et appliquées le 27/09/2026)

| Doc | Modification | Justification |
|---|---|---|
| CDC §6 | ✅ Tableau des lots et des portes en texte ; exigences non affectées par le DT placées et marquées d'un astérisque | I-01 |
| CDC §6 / DT §10 | ✅ FIS-06 (volet CSV), ANA-11, ALR-02, UI-01, UI-05 et NF-SEC-04 au lot 1 ; LOC-10 au lot 2 | I-02, I-03 |
| CDC NF-SEC-03 | ✅ Sauvegardes chiffrées, données sensibles chiffrées au niveau applicatif, disque non chiffré, vol physique accepté | I-04, Q3 |
| CDC COL-02 et §3 | ✅ QIF retiré | I-17, Q7 |
| CDC §5 et §9 | ✅ Virement rejeté : échéance « recalculée » au lieu de « repassée à Attendu » ; J+10 après la date limite ; affectation au plus ancien impayé ; questions ouvertes répondues cochées | L-01, L-02, L-07, Q1, Q6 |
| CDC NF-PERF-01 / DT §5 et §6 | ✅ Machine dédiée : jamais sur l’hôte d’un bot de trading | Q1 |
| DT §1 et §4 | ✅ Planificateur maison + `job_run` seule source de vérité | I-14, Q13 |
| DT §5 | ✅ Procédure de rollback + migrations expand/contract ; `minimumReleaseAge` Renovate ; secrets en tmpfs | I-21, I-22, I-25 |
| DT §6 | ✅ Vol de la machine : risque accepté ; liste blanche des requêtes sortantes | I-04, I-27 |
| DT §8 | ✅ Tables ajoutées, `ledger_transaction`, `dedupe_key`, `duplicate_link`, `review_item`, `rent_incident`, statut des allocations | I-07, I-08, I-15, I-16, L-04, L-05 |
| DT §10 / CONTRIBUTING.md | ✅ `tests/scenarios/` étendu au dédoublonnage | Q5 |
| `regles-loyer.md` | ✅ L-01 à L-07, et L-12 | état dérivé |
| CONTRIBUTING.md | ✅ inv. 1 (liste blanche), inv. 9 (DATE / DATETIME), inv. 10 (budget en requêtes), nouvel inv. 11 (hôte dédié) | I-10, I-13, I-27, Q1 |

### 1.5 Sauvegarde hors site : décision Q11 et analyse de la perte maximale

**Décision (27/09/2026)** : plus de Litestream. Toutes les **15 min**, une copie cohérente de la base est faite **en mémoire** (API backup de SQLite vers un tmpfs), puis envoyée par **restic** (chiffrement côté client) vers B2. **Objectif de perte maximale (RPO) : 15 min**, décision explicite. Options écartées : Litestream 0.5 non chiffré (viole NF-SEC-03), Litestream 0.3 (plus maintenu, viole NF-SEC-08), Litestream + rclone crypt (passerelle jeune dans le chemin critique pour un gain de 15 min).

**Ce qui ne se re-synchronise pas, et la parade retenue :**

| Donnée perdue sur 15 min au plus | Conséquence après restauration | Parade |
|---|---|---|
| Transactions, soldes | Aucune : rechargées à la synchro suivante (recouvrement de 7 jours, T1.13) | Idempotence (inv. 3) |
| Catégorisations manuelles, règles, validations de la file, décisions de rapprochement | Travail de moins de 15 min à refaire | **Accepté**. L'écran d'accueil signale « restauration du JJ/MM à HH:MM » |
| Alertes (création, vue, résolution) | Recalculées depuis l'état des données ; une alerte résolue peut réapparaître | État des alertes **recalculable** : les conditions sont réévaluées à chaque passage (T1.19), et les `dedupe_key` évitent les doublons en base |
| Notifications à l'administrateur déjà envoyées | Une notification peut repartir une seconde fois | **Accepté** (doublon sans gravité, contenu sans montant ni nom) |
| **Messages au locataire** (lots 2 et 4) | **Risque de double relance** : l'envoi a eu lieu, mais la base l'ignore | Chaque envoi au locataire **déclenche une sauvegarde immédiate** ; l'administrateur est en copie cachée de chaque envoi ; l'écran de validation affiche la dernière relance connue de la boîte d'envoi. À implémenter en T1.20 et au lot 2 |
| Budget DSP2 (`provider_call`) | Les requêtes des 15 dernières minutes sont oubliées : risque de dépasser 4 requêtes par 24 h (inv. 10) | Après une restauration, **le premier créneau automatique de chaque compte est sauté** (au plus un créneau tombe dans la fenêtre perdue). À implémenter en T1.6 |
| Session Enable Banking créée dans la fenêtre | Consentement donné côté banque mais inconnu de la base | Refaire la liaison ; révoquer l'ancienne session dans le Control Panel (runbook T0.16) |
| `job_run` | Tâches de la fenêtre rejouées | Idempotence (inv. 3) |

**Mise en œuvre** (T0.9 et T0.12) :
- copie cohérente en tmpfs, puis `restic backup --stdin --stdin-filename finances.db` : **rien ne transite par le SSD** ;
- mot de passe du dépôt restic dans sops **et** dans le gestionnaire de mots de passe ;
- clé B2 limitée au bucket, **Object Lock** (rétention par défaut de 30 jours) et règle de cycle de vie qui purge les versions masquées : une compromission du Pi ne peut pas effacer les sauvegardes ;
- rétention `forget --keep-within 24h --keep-daily 30 --keep-monthly 12`, puis `prune` hebdomadaire ;
- `restic check` hebdomadaire (avec lecture d'un sous-ensemble des données) et **test de restauration automatisé hebdomadaire** : restaurer, ouvrir la base, `PRAGMA integrity_check` ;
- supervision : check Healthchecks de période 15 min et de grâce 30 min, donc une alerte si la dernière sauvegarde réussie a plus de 45 min environ.

---

## 2. Questions ouvertes

| # | Question | Bloque | Réponse / décision |
|---|---|---|---|
| Q1 | Matériel | 0B | ✅ Pi 4 Model B **4 Go**, **dédié** (inv. 11), joignable en SSH par clé. ⚠️ Le « SSD » est une clé USB 2.0 (I-29) : un SSD est à acheter avant la mise en service |
| Q2 | Comptes externes | 0A (GHCR), 0B, T1.10 | ✅ Dépôt GitHub créé. ❌ B2, Healthchecks, Enable Banking : voir 2.1 |
| Q3 | Chiffrement au repos | migration 0001 | ✅ Pas de LUKS ; NF-SEC-03 reformulé ; chiffrement applicatif + blind index (T1.3) |
| Q4 | restic dans l'étape 0 | 0B | ✅ Oui (T0.12) |
| Q5 | Doublons entre API et fichiers | T1.16 | ✅ Approche hybride : lien automatique si certain, file « doublon probable » sinon, liaison réversible, l'API est canonique (T1.16a et T1.16b) |
| Q6 | Banques, comptes, compte d'arrivée du loyer | T1.10, T1.14 | ✅ Deux banques (profils CSV Boursorama et Société Générale fournis). Un loyer arrive sur un compte personnel, un autre sur un compte joint (voir L-10 et L-11 pour le lot 2) |
| Q7 | QIF | T1.15 | ✅ Retiré de COL-02 |
| Q8 | Téléphone et e-mail | T1.20 | ✅ Android ; SMTP personnel avec mot de passe d'application |
| Q9 | Second facteur | T1.22 | ✅ TOTP au lot 1 ; passkey plus tard |
| Q10 | Périmètre du lot 1 | lot 1 | ✅ Périmètre de la section 3.2 ; démarrage en parallèle de 0B, mise en service après la porte de l'étape 0 |
| Q11 | Sauvegarde hors site sans Litestream chiffré (I-31) | T0.9, T0.11, T0.12 | ✅ restic seul toutes les 15 min, RPO 15 min (section 1.5) |
| Q12 | Tester `bootstrap.sh` sur le Pi actuel | T0.13 | ✅ Oui, sur le Pi actuel (banc d'essai jetable) |
| Q14 | **Plafond DSP2 : 4 requêtes par compte, ou par compte et par endpoint ?** Une synchro complète envoie au moins 2 requêtes (soldes, transactions). Compter 4 requêtes par compte au total ne permettrait que 2 synchros par jour | inv. 10, T1.6, T1.13 | ✅ **Oui, comme hypothèse** (28/09/2026) : 4 par compte et par endpoint, profils par banque (Boursorama, SG) réglables par `FINANCES_BANK_BUDGETS` ; un 429 ou un refus de quota n'est pas retenté avant le créneau suivant et n'est pas une panne ; alerte après 24 h sans succès seulement ; limites observées journalisées (`app.cli limites-observees`). **La synchro manuelle consomme le budget** (en-têtes PSU peu fiables derrière Tailscale) ; bouton désactivé s'il ne reste que de quoi faire la prochaine synchro automatique |
| Q13 | **Retirer APScheduler de la stack ?** Le planificateur (T1.5) est une boucle maison de 100 lignes : créneaux à l'heure de Paris et rattrapage reposent sur `job_run` et `run_once` ; APScheduler n'apporterait qu'un minuteur | DT §4, CONTRIBUTING.md (stack) | ✅ **Oui** (28/09/2026) : APScheduler retiré (DT §4, CONTRIBUTING.md). Tests : rattrapage après arrêt, pas de chevauchement, changements d'heure sans créneau sauté ni doublé, décalage des créneaux bancaires |

### 2.0 Décisions du 28/09/2026 (après la première liaison réelle)

| # | Sujet | Décision |
|---|---|---|
| Q15 | **Comptes comptés ou en consultation seule** | Un compte « compté » entre dans le patrimoine, les recettes et les dépenses ; un compte en **consultation seule** reste visible sans être additionné. En consultation seule : un **compte joint**, un **compte en gestion pilotée** détenu en indivision, les **cartes** à débit immédiat (leurs paiements répètent ceux du compte courant, constaté sur des données réelles). Un virement vers un compte en consultation seule reste une dépense. Réglage par compte sur `/comptes`, audité ; cartes en consultation seule par défaut à la liaison |
| Q16 | **Alertes de loyer** | Alerte 1 le **5** du mois (avertissement, notification simple) ; alerte 2 le **10** du mois (critique). Jours réglables par bail. CDC §5 et `regles-loyer.md` mis à jour (remplacent « fin de tolérance » et « J+10 après la date limite ») |
| Q17 | **Encaissements attendus** | Deux loyers, sur deux comptes ; comptes d'encaissement à 4 synchros par jour. Un **remboursement mensuel de prêt** consenti à un particulier : à suivre au lot 2 comme un **encaissement attendu hors bail**, avec **les mêmes alertes que les loyers** (le 5 et le 10 ; décision du 29/09/2026) |

| Q18 | **Crypto** | **Hors périmètre** (29/09/2026) : COL-04 et FIS-03 passent en W, 3916-bis retiré de FIS-04 ; le garde de démarrage refuse toute clé crypto (CONTRIBUTING.md inv. 1, CDC, DT) |
| Q19 | **Compte ignoré** | Troisième état d'un compte, après « compté » et « consultation seule » : **ignoré** = plus de synchro, invisible partout (transactions, recherche, exports, tableau de bord, alertes, IA), sauf dans une section « Comptes ignorés » de `/comptes` d'où on le réactive. Données déjà importées conservées et cachées (inv. 4), réversible. Cas d'usage : compte SG en gestion pilotée (indivision) |
| Q21 | **E-mail et second facteur** | Pas de SMTP (30/09/2026) : alertes par ntfy uniquement. Second facteur au choix : code TOTP ou **clé de sécurité** FIDO2 (YubiKey) |
| Q23 | **Cas de référence** (06/10/2026) | Gestion **en direct** (pas d'agence) ; LMNP au **réel** ; taxe foncière **annuelle** ; factures en **PDF**. L'outil doit néanmoins tout gérer (agence, micro-BIC, mensualisation, photos) pour d'autres foyers |
| Q24 | **Vision produit** (06/10/2026) | Commercialisation possible à terme : une instance par foyer, installable partout (on-prem ou cloud), mise en place et mises à jour sans friction, administration à distance, résilience maximale, code propre et maintenable, conformité légale dès maintenant, CI/CD sans faille. CDC §10 (PROD, DEP, RES, QUA, QA, LEG), `docs/conformite.md`, DT §13, principes dans `CONTRIBUTING.md` |
| Q25 | **Intuitif avant tout** (07/10/2026) | Principe de premier rang : compréhension, utilisation, vocabulaire et fonctionnement général évidents sans documentation. Charte `docs/charte-ergonomie.md` (règles, glossaire, contrôle de fin de tâche), `CONTRIBUTING.md` et PROD-04 mis à jour. Première passe : navigation regroupée par besoin, vocabulaire unifié (opération, mise à jour, accès à la banque, compte suivi), boutons en verbes précis, sigles développés, renouvellement de l'accès bancaire en un clic, majuscule des messages corrigée |
| Q22 | **IA : écriture et noms** (30/09/2026) | L'IA peut **modifier** l'application (catégories, règles, file à valider, baux et loyers) via les services audités, avec portée du jeton, interrupteur, aperçu et confirmation des écritures de masse, journal des modifications **annulables** ; jamais d'argent, d'accès ni d'envoi au locataire. Noms de personnes **en initiales** ; noms de commerçants conservés ; tous les comptes non ignorés visibles, totaux sur les comptes comptés, cartes exclues par défaut. Spécification : `docs/spec-mcp.md` (UI-03 et invariant 8 révisés) |
| Q20 | **Ordre et analyse par IA** | Ordre : A (remise d'aplomb) → **lot 2 loyers** → **B (analyse IA)** → lot 3 → lot 4. B : une couche d'outils d'analyse en lecture seule, sur données pseudonymisées (inv. 8), exposée (1) en **MCP** pour le client MCP de bureau, sur abonnement (voie prioritaire ; l'abonnement ne s'utilise pas par API) via Tailscale et un relais local sur le PC, (2) par un **assistant intégré** à fournisseur choisi : API Anthropic, OpenAI ou OVH AI Endpoints (compatibles OpenAI). Remplace l'assistant intégré prévu au lot 4 (UI-03) |

### 2.1 Actions manuelles (hors code)

| Action | Nécessaire avant |
|---|---|
| ~~Créer le dépôt GitHub~~ ✅ fait le 27/09/2026. Ajouter le scope `workflow` au jeton `gh` (`gh auth refresh -s workflow`, flux navigateur) | T0.5 |
| Installer l'application Renovate (Mend) sur le dépôt `finances` uniquement : <https://github.com/apps/renovate> | T0.6 |
| ~~Clé age de la machine~~ ✅ générée par bootstrap : la copier dans le gestionnaire de mots de passe (`sudo cat /etc/finances/age/keys.txt`). Générer une **clé de secours** et ajouter **sa clé publique** à `.sops.yaml` | T0.7 |
| Autoriser le Pi dans Tailscale (lien donné par `bootstrap.sh`, ou `sudo tailscale up` sur le Pi) | T0.13 |
| Créer un jeton GitHub limité à `read:packages` pour que le Pi tire l'image (secret `ghcr_token`) | T0.13 |
| *(facultatif)* Brancher le Pi en Ethernet (I-32) | — |
| *(recommandé, facultatif)* SSD USB 3 sur un port bleu (I-29) ; pour T0.15, un support vierge quelconque (clé ou carte) suffit, avec Raspberry Pi OS **Lite** 64 bits (I-30) | T0.15 |
| Créer le compte Backblaze B2 : bucket privé en région UE, clé d'application limitée à ce bucket | T0.13 |
| Créer le compte Healthchecks.io et les 5 checks | T0.14 |
| Créer un mot de passe d'application SMTP | T1.20 (essai réel) |
| Créer l'application Enable Banking « Production », lier les banques dans le Control Panel, déclarer l'URL de retour Tailscale | T1.10 (cassettes), T1.12 |
| Fournir, pour chaque banque, un export CSV **anonymisé** (ou seulement l'en-tête et 3 lignes inventées au bon format) et un export OFX si la banque le propose | T1.14, T1.15 |

---

## 3. Découpage des tâches

Chaque tâche prend moins d'une demi-journée. Elle n'est terminée que si `uv run pytest`, `ruff` et `mypy --strict` passent, si ce fichier est à jour et si le commit cite les exigences couvertes.

### 3.1 Étape 0 — Infrastructure

#### 0A — Dépôt et chaîne logicielle (sans matériel)

**[x] T0.1 — Squelette du dépôt** *(27/09/2026)*
- Exigences : NF-EXP-01
- Fichiers : `pyproject.toml`, `uv.lock`, `.python-version` (3.12), `.gitignore` (`data/`, `*.dec.yaml`, `.env*`, `exports-bancaires/`, `*.pem`, `*.key`), `.gitattributes`, `README.md`, `app/__init__.py` et les sous-paquets vides de l'arborescence, `tests/__init__.py`, `tests/test_smoke.py`
- Tests : import de `app` et de chaque sous-paquet
- Fin : `uv sync && uv run pytest` au vert ; premier commit sur `main`

**[x] T0.2 — Qualité : ruff, mypy strict, pytest sans réseau** *(27/09/2026)*
- Exigences : NF-EXP-02, inv. 5
- Fichiers : `pyproject.toml` (sections `ruff`, `mypy`, `pytest`), `tests/conftest.py`
- Détails : ruff avec les règles `E,F,I,B,UP,S,DTZ,PL,RUF` (`DTZ` interdit les datetimes naïfs) ; mypy `strict = true` ; `pytest-socket` avec `--disable-socket` ; profils hypothesis `ci` et `dev`
- Tests : un test qui ouvre une socket doit échouer ; un fichier avec `datetime.now()` sans fuseau est signalé par ruff
- Fin : `uv run ruff check . && uv run mypy app && uv run pytest` au vert ; section « Commandes » de CONTRIBUTING.md à jour

**[x] T0.3 — Garde-fous contre les secrets** *(27/09/2026)*
- Exigences : NF-SEC-02, inv. 5
- Fichiers : `.pre-commit-config.yaml` (gitleaks, ruff), `.gitleaks.toml`
- Tests : un commit contenant un faux jeton de test est bloqué localement
- Fin : pre-commit installé et documenté dans le README

**[x] T0.4 — Application minimale + Dockerfile** *(27/09/2026 ; image de 128 Mo, construite et testée en CI)*
- Exigences : NF-EXP-01, NF-PERF-01
- Fichiers : `app/web/main.py` (FastAPI, `/health` seulement), `app/ops/worker.py` (boucle vide qui journalise), `Dockerfile` (multi-étapes, `python:3.12-slim` épinglé par condensat, uv, utilisateur non root, `HEALTHCHECK`), `.dockerignore`
- Tests : `tests/web/test_health.py` (TestClient)
- Fin : `docker build` local réussi ; image de moins de 200 Mo ; conteneur non root

**[x] T0.5 — CI GitHub Actions (qualité + image arm64)** *(27/09/2026)*
- Exigences : NF-EXP-01, NF-EXP-02, NF-SEC-08
- Fichiers : `.github/workflows/ci.yml` (lint, types, tests, gitleaks, shellcheck), `.github/workflows/image.yml` (buildx `linux/arm64` + `linux/amd64` pour les tests, push GHCR sur `main`, étiquettes `sha-<court>` et `main`)
- Tests : la CI elle-même ; `docker manifest inspect` montre `linux/arm64`
- Fin : exécution verte sur une PR et sur `main` ; image présente dans GHCR ; durée notée ici (I-23)
- Dépend de : T0.2, T0.4

**[x] T0.6 — Renovate** *(configuration le 27/09/2026 ; application installée le 28/09/2026, « Dependency Dashboard » ouvert)*
- Exigences : NF-SEC-08
- Fichiers : `renovate.json` (lockfile uv, actions et images épinglées par condensat, automerge patch et mineures avec `minimumReleaseAge: "7 days"`, majeures manuelles, regroupement hebdomadaire)
- Tests : validation de configuration (`renovate-config-validator`) en CI
- Fin : l'application Renovate est installée (action manuelle) et la première PR « Configure Renovate » est fusionnée
- Dépend de : T0.5

**[~] T0.7 — sops + age et chargement des secrets** *(28/09/2026 : `.sops.yaml` (machine + secours), clés internes générées chiffrées, déchiffrement vérifié sur le Pi, saisie par `deploy/set-secret.sh` ; restent les secrets externes à saisir par toi)*
- Exigences : NF-SEC-02, inv. 5
- Fichiers : `.sops.yaml` (destinataires : clé publique de la machine + la clé de secours), `deploy/secrets.enc.yaml` (clés **factices** au départ), `deploy/render-secrets.sh` (déchiffrement vers `/run/finances/secrets.env`, 0600, tmpfs), `app/config.py` (pydantic-settings avec prise en charge de `*_FILE`)
- Tests : `tests/test_config.py` (chargement depuis un fichier, erreur explicite si un secret manque, aucun secret dans le `repr`) ; un test CI vérifie que toutes les valeurs de `secrets.enc.yaml` commencent par `ENC[`
- Fin : un aller-retour chiffrement / déchiffrement documenté fonctionne sans jamais écrire le clair dans le dépôt

**[x] T0.8 — docker compose** *(27/09/2026, sans les services de réplication, qui attendent la décision I-31)*
- Exigences : NF-SEC-05, inv. 7, NF-RES-01, NF-PERF-01
- Fichiers : `deploy/compose.yml` avec les services `migrate` (ponctuel : `alembic upgrade head`), `web`, `worker` et `ntfy` ; ports **uniquement** sur `127.0.0.1` ; `restart: unless-stopped` ; `mem_limit` pour un Pi 4 Go dédié (web 512 Mo, worker 768 Mo, litestream 128 Mo, ntfy 128 Mo ; le reste pour l'OS, restic et le cache disque) ; volumes `data/` ; `env_file` pointant vers le tmpfs
- Tests : `tests/deploy/test_compose.py` lit le YAML et échoue si un port n'est pas lié à `127.0.0.1`, si une image n'est pas épinglée, si un conteneur monte `/var/run/docker.sock` ou tourne en `privileged`
- Fin : `docker compose config` valide en CI ; la pile démarre en local avec des secrets factices
- Dépend de : T0.4, T0.7

**[x] T0.9 — Sauvegarde restic toutes les 15 min** *(Q11 ; 27/09/2026)*
- Exigences : NF-RES-02, NF-SEC-03, NF-RES-04
- Fichiers : `deploy/backup.sh` (copie cohérente par l'API backup de SQLite vers `/run/finances/backup/` (tmpfs), `PRAGMA integrity_check` sur la copie, `restic backup --stdin --stdin-filename finances.db --tag db`, suppression de la copie, ping Healthchecks), `deploy/systemd/finances-backup.{service,timer}` (toutes les 15 min, utilisateur `finances` uid 10001)
- Tests : `tests/deploy/test_backup.py` avec un dépôt restic local jetable : sauvegarde, restauration, contenu identique, `integrity_check` ; base vide ou absente → échec explicite ; aucune écriture hors du tmpfs indiqué ; refus d'une destination sur disque ; mot de passe restic jamais affiché. shellcheck
- Fin : tests verts en local et en CI
- Dépend de : T0.7, T0.10

**[x] T0.10 — Alembic et moteur SQLite (sans table)** *(27/09/2026 ; le service `migrate` sera vérifié avec compose en T0.8)*
- Exigences : NF-EXP-01, NF-RES-01
- Fichiers : `alembic.ini`, `migrations/env.py` (`render_as_batch=True`, convention de nommage des contraintes), `app/db.py` (moteur, pragmas : `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`)
- Tests : `tests/test_db.py` vérifie les pragmas actifs sur chaque nouvelle connexion ; `alembic upgrade head` réussit sur une base vide
- Fin : le service `migrate` passe (sans effet) dans la pile locale

**[x] T0.11 — Script de déploiement nocturne et rollback** *(27/09/2026)*
- Exigences : NF-EXP-01, NF-RES-01, NF-RES-04
- Fichiers : `deploy/update.sh`, `deploy/systemd/finances-update.{service,timer}`
- Étapes : récupérer l'image ; si le condensat a changé, arrêter web et worker, faire une sauvegarde restic (tag `pre-deploy`) et un instantané local (API backup), migrer, démarrer, attendre `/health` au plus 2 min. En cas d'échec : restaurer l'instantané et l'image précédente, relancer, ping Healthchecks `/fail`. En cas de succès : supprimer l'instantané local, ping OK
- Tests : shellcheck ; test CI avec compose qui déploie une image volontairement cassée et vérifie le retour à l'image et à la base précédentes
- Fin : le test de rollback est vert en CI
- Dépend de : T0.8, T0.9, T0.10

**[x] T0.12 — Rétention, vérification et test de restauration restic** *(Q4, Q11 ; 27/09/2026 ; création du bucket B2 : action manuelle)*
- Exigences : NF-RES-02, NF-SEC-03
- Fichiers : `deploy/backup-maintenance.sh` (`forget --keep-within 24h --keep-daily 30 --keep-monthly 12 --prune`, `check --read-data-subset=5%`), `deploy/restore-test.sh` (restaurer le dernier instantané dans un tmpfs, ouvrir la base, `integrity_check`, comparer au nombre de tables attendu, ping Healthchecks), unités systemd hebdomadaires, `docs/exploitation.md` (bucket B2 avec Object Lock et cycle de vie, clé limitée au bucket, restauration manuelle)
- Tests : dépôt restic local jetable : rétention appliquée (instantanés horodatés simulés), test de restauration vert puis rouge sur une sauvegarde corrompue ; shellcheck
- Fin : tests verts ; procédure B2 documentée

#### 0B — Machine et services (nécessite le matériel et les comptes)

**[~] T0.13 — bootstrap.sh idempotent** *(à compléter : écrire `deploy/.env` sur le Pi avec `FINANCES_PUBLIC_BASE_URL` (nom Tailscale) et `FINANCES_BACKUP_EXPECTED=true`)* *(27/09/2026 : exécuté sur le Pi jusqu'à l'étape Tailscale ; reste Tailscale, secrets, GHCR et premier déploiement)*
- Exigences : NF-EXP-01, NF-SEC-05, UI-02, NF-SEC-08
- Fichiers : `deploy/bootstrap.sh`, `docs/exploitation.md` (procédure)
- Étapes : vérifier architecture et OS ; installer Docker (dépôt officiel), Tailscale, sops et age (versions et sommes de contrôle épinglées) ; vérifier la présence de la clé age ; activer le chien de garde, `unattended-upgrades` et NTP ; installer les unités systemd ; `tailscale serve` (HTTPS vers `127.0.0.1`) pour l'interface et ntfy ; connexion à GHCR ; lancer la pile
- Tests : shellcheck ; exécution **deux fois de suite** dans un conteneur Debian arm64, en simulation sans systemd, sans aucun changement au second passage
- Fin : exécuté deux fois sur le Pi, avec une sortie identique et aucune erreur
- Dépend de : T0.7 à T0.12

**[ ] T0.14 — Supervision Healthchecks**
- Exigences : NF-RES-04, ALR-08
- Fichiers : `deploy/secrets.enc.yaml` (URL de ping), `docs/exploitation.md`
- Tests : un ping manqué volontairement déclenche l'e-mail de Healthchecks
- Fin : les 5 checks du DT §7 sont créés et au moins 3 reçoivent des pings (déploiement, sauvegarde, restauration)

**[~] T0.16 — Runbook de vol ou de compromission** *(rédigé le 27/09/2026 ; reste la relecture et un exercice à blanc de révocation)*
- Exigences : NF-SEC-01, NF-SEC-02, NF-SEC-05
- Fichiers : `docs/runbook-vol.md` (n'existe pas encore) : révoquer les consentements bancaires (bouton de T1.12 ou Control Panel Enable Banking si la machine est perdue), retirer le nœud du tailnet, faire tourner les secrets (clé RSA Enable Banking, clés B2, SMTP, Healthchecks, clé age), révoquer les clés crypto (lot 4), prévenir les banques si besoin
- Tests : relecture ; exercice à blanc de révocation d'un consentement de test
- Fin : runbook relu et daté ; lien depuis `docs/exploitation.md`

**[ ] T0.15 — Porte de l'étape 0 : reconstruction sur un support vierge (SSD recommandé)**
- Exigences : NF-RES-02, NF-EXP-01
- Tests : manuel. Flasher, lancer `bootstrap.sh`, fournir la clé age : la base est restaurée depuis B2, `/health` est OK via Tailscale, et aucun port n'écoute hors `127.0.0.1` et `tailscale0` (`ss -tlnp`)
- Fin : procédure horodatée et consignée ici

### 3.2 Lot 1 — Socle

**Périmètre proposé (Q10)** : COL-01, COL-02, COL-03, COL-06, COL-07, COL-08 · ANA-01, ANA-09 (patrimoine = soldes des comptes synchronisés ; COL-05 plus tard), ANA-11 · FIS-06 (export CSV des transactions) · ALR-01 (moteur minimal), ALR-02, ALR-06 (cycle de vie), ALR-08 · UI-01, UI-02, UI-05 · NF-SEC-04, NF-SEC-06 (connexions, exports, règles), NF-RES-01, NF-RES-03, NF-EXP-03, NF-PERF-02.
Scénarios d'acceptation du CDC §9 couverts : « Coupure électrique pendant une synchro », « Même fichier CSV importé deux fois », « Consentement bancaire qui expire dans 7 jours ».

*Fondations*

**[x] T1.1 — Types monétaires et temporels** *(27/09/2026 ; arrondi au demi supérieur retenu par défaut, à confirmer pour l'IRL au lot 4)*
- Exigences : inv. 2, inv. 9, UI-05
- Fichiers : `app/money.py` (`Cents = NewType`, `parse_amount(str) -> Cents` via `Decimal` pour les formats `12.34`, `-1 234,56` et `1.234,56`, qui rejette `float` ; `round_to_cent` unique, arrondi au demi supérieur (`ROUND_HALF_UP`), à confirmer), `app/timeutil.py` (`utcnow()`, conversions Europe/Paris), `app/db_types.py` (`UTCDateTime` : TypeDecorator qui refuse les dates naïves)
- Tests : hypothesis (aller-retour Decimal ↔ cents ↔ chaîne, `parse_amount(float)` lève une erreur) ; `UTCDateTime` refuse une datetime naïve
- Fin : couverture de 100 % de `money.py`

**[x] T1.2 — Modèle ORM et migration 0001** *(27/09/2026)*
- Exigences : COL-01, COL-02, COL-03, COL-06, COL-08, ANA-01, ALR-01, NF-SEC-04, NF-SEC-06, inv. 3, inv. 4
- Fichiers : `app/models/*.py` (base déclarative, un module par domaine), `migrations/versions/0001_socle.py`
- Tests : upgrade puis downgrade sur base vide ; `alembic check` (aucun écart entre modèle et migration) ; contraintes (unicités, CHECK, trigger d'immuabilité : un `UPDATE ledger_transaction` lève une erreur)
- Fin : schéma conforme à la section 5
- Dépend de : T0.10, T1.1, **Q3**

**[x] T1.3 — Chiffrement de colonnes et blind index** *(Q3 ; 27/09/2026)*
- Exigences : NF-SEC-02, NF-SEC-03, NF-DATA-01
- Fichiers : `app/crypto.py` (AES-GCM, clé depuis les secrets ; HMAC-SHA256 pour l'index), `app/db_types.py` (`EncryptedString`)
- Tests : aller-retour, clé fausse → erreur, même IBAN → même empreinte, IBAN normalisé (espaces, casse) avant l'empreinte
- Fin : IBAN, noms et `raw_payload.content` illisibles dans `sqlite3 data/finances.db` ; clé de chiffrement distincte de celles des sauvegardes (DT §6)

*Exécution fiable*

**[x] T1.4 — `job_run` : exécution unique par créneau** *(27/09/2026)*
- Exigences : inv. 3, NF-RES-01
- Fichiers : `app/ops/jobs.py` (`run_once(job, slot)` : réservation par `INSERT` unique, battement de cœur, reprise d'un `running` périmé après plantage)
- Tests : deux appels concurrents → une seule exécution ; plantage simulé → reprise au redémarrage ; créneau déjà réussi → rien n'est fait
- Fin : tests verts, y compris en multi-processus

**[x] T1.5 — Planificateur du worker et rattrapage** *(27/09/2026 ; ping Healthchecks branché avec le client HTTP en T1.7)*
- Exigences : NF-RES-01, NF-RES-03, NF-RES-04
- Fichiers : `app/ops/scheduler.py` (planificateur maison, sans APScheduler, créneaux en Europe/Paris convertis en UTC, rattrapage des créneaux manqués au démarrage), `app/ops/healthchecks.py` (ping à chaque cycle)
- Tests : horloge simulée ; changement d'heure été / hiver ; redémarrage après 6 h d'arrêt → une seule exécution de rattrapage par tâche
- Fin : le worker de la pile locale exécute une tâche factice et pinge un faux Healthchecks

**[x] T1.6 — Budget DSP2** *(27/09/2026 ; 28/09/2026 : profils par banque, synchro manuelle comptée, Q14)*
- Exigences : inv. 10, COL-01
- Fichiers : `app/connectors/budget.py` (table `provider_call`, `acquire(account, unattended)`)
- Tests : **propriété hypothesis** — sur toute suite d'appels, de reprises et de redémarrages, jamais plus de 4 requêtes automatiques par compte sur une fenêtre glissante de 24 h ; les synchros manuelles suivent leur propre limite (I-12) ; **après une restauration, le premier créneau automatique de chaque compte est sauté** (section 1.5)
- Fin : tests verts

*Connecteurs*

**[x] T1.7 — Interface commune et transport en lecture seule** *(27/09/2026)*
- Exigences : inv. 1, NF-SEC-01, NF-EXP-03, O5
- Fichiers : `app/connectors/base.py` (`Protocol` avec `fetch_accounts`, `fetch_balances`, `fetch_transactions` ; types normalisés Pydantic), `app/net/allowlist.py` (liste explicite **hôte → méthode → gabarit de chemin**), `app/net/client.py` (**seule** fabrique de clients httpx : un transport vérifie chaque requête avant l'envoi et lève `ForbiddenRequest` sinon ; délais, tenacity avec chaque tentative passant par le budget, disjoncteur), `app/ops/startup_guard.py` (I-26)
- Liste blanche au lot 1 : Enable Banking (`api.enablebanking.com`) → `POST /auth`, `POST /sessions`, `DELETE /sessions/{id}` (révocation), `GET /aspsps`, `GET /sessions/{id}`, `GET /accounts/{uid}/details`, `GET /accounts/{uid}/balances`, `GET /accounts/{uid}/transactions`. Healthchecks → `GET`/`POST /{uuid}[/fail|/start]`. ntfy local → `POST /{topic}`. SMTP hors HTTP (hôte fixé dans la configuration). Tout le reste est refusé, y compris `PUT`, `PATCH` et tout `DELETE` autre que `/sessions/{id}`
- Tests : requête hors liste blanche → `ForbiddenRequest`, sans ouvrir de socket ; **test paramétré sur les endpoints de paiement** (`/payments`, `/payments/{id}`, `/payments/{id}/transactions`, `/bulk-payments`, `/periodic-payments`… avec toutes les méthodes et sur tous les hôtes) : chacun doit être refusé ; test d'architecture qui échoue si `httpx.Client`, `httpx.AsyncClient`, `requests` ou `urllib` sont utilisés hors de `app/net/client.py` ; test qui échoue si un gabarit autorisé contient `payment`, `order`, `withdraw` ou `transfer` ; chaque reprise consomme le budget
- Fin : tests verts

**[x] T1.8 — Faux connecteur, fixtures et anonymisation** *(27/09/2026)*
- Exigences : inv. 5, NF-EXP-02
- Fichiers : `tests/fakes/connector.py`, `tests/factories.py` (IBAN fictifs à clé valide, pris dans une liste autorisée), `scripts/anonymize_cassette.py`, `tests/test_no_pii.py` (cherche des IBAN valides hors liste autorisée, des e-mails et des noms d'une liste locale non versionnée dans `tests/cassettes` et `tests/data`)
- Tests : le scanner détecte un IBAN réel de test injecté
- Fin : le scanner tourne en CI

**[x] T1.9 — Ingestion : données brutes, normalisation, dédoublonnage** *(27/09/2026)*
- Exigences : COL-03, COL-08, inv. 3, inv. 4
- Fichiers : `app/ledger/ingest.py` (stocker `raw_payload` avec son empreinte SHA-256, créer l'`import_batch`, calculer la `dedupe_key` selon I-07, `INSERT … ON CONFLICT DO NOTHING`), `app/ledger/labels.py` (nettoyage du libellé, fonction pure)
- Tests : **hypothesis** — ingérer N fois, dans n'importe quel ordre et avec des chevauchements, donne le même ensemble ; deux achats identiques le même jour sont bien conservés tous les deux ; rejouer depuis `raw_payload` donne le même résultat
- Fin : tests verts

**[~] T1.10 — Client Enable Banking (auth JWT + lecture)** *(28/09/2026 sur réponses **synthétiques** ; reste l'enregistrement de vraies réponses anonymisées, qui demande l'application Enable Banking)*
- Exigences : COL-01, inv. 1
- Fichiers : `app/connectors/enable_banking/client.py` (JWT RS256, clé privée lue dans les secrets ; `GET /aspsps`, `/accounts/{uid}/details`, `/balances`, `/transactions` avec pagination `continuation_key`), `tests/cassettes/enable_banking/*.yaml`
- Tests : lecture des cassettes anonymisées, pagination, erreurs 401 (consentement expiré), 429 et 5xx
- Fin : tests verts sans réseau
- ⛔ **Bloque la porte du lot 1 (T1.29)** tant que les conversions n'ont pas été validées sur de **vraies réponses anonymisées** des banques réelles (cassettes enregistrées puis anonymisées) ; les réponses actuelles sont synthétiques. Le code de refus de quota dans le corps (`ASPSP_RATE_LIMIT_EXCEEDED`) est à confirmer au même moment
- Dépend de : T1.7, T1.8, **Q2** (application EB créée pour enregistrer les cassettes)

**[~] T1.11 — Correspondance Enable Banking → modèle normalisé** *(28/09/2026 ; à revalider sur les réponses réelles des banques réelles)*
- Exigences : COL-01, COL-03
- Fichiers : `app/connectors/enable_banking/mapping.py`
- Tests : cassettes des **deux** banques (Boursorama, SG) ; `BOOK` gardé, `PDNG` ignoré (I-09) ; montant et signe (`credit_debit_indicator`) ; `entry_reference` absent → empreinte ; contrepartie débitrice ou créditrice selon le sens
- Fin : tests verts
- ⛔ **Bloque la porte du lot 1 (T1.29)** tant que la correspondance n'a pas été validée sur de vraies réponses anonymisées des banques réelles

**[~] T1.12 — Liaison de banque (consentement)** *(28/09/2026 ; reste l'essai réel via Tailscale)*
- Exigences : COL-01, COL-07, UI-02
- Fichiers : `app/web/routes/connections.py` (`POST /auth` avec validité maximale lue via `/aspsps`, callback HTTPS Tailscale, `POST /sessions`, stockage de `session_id` (chiffré selon Q3) et de `consent_expires_at` ; **révocation** d'une connexion par `DELETE /sessions/{id}`), audit
- Tests : flux complet sur cassettes ; révocation (connexion passée à `revoked`, auditée) ; `state` anti-CSRF vérifié ; **jeton de retour à usage unique** (nonce `link_nonce`, empreinte stockée, invalidé atomiquement au premier retour, en plus des 15 min) et **test de rejeu** ; renouvellement d'une connexion existante sans doublon de comptes
- Fin : tests verts ; essai réel documenté (liaison d'une banque en local via Tailscale)

**[x] T1.13 — Tâche de synchro incrémentale** *(28/09/2026, sur le faux serveur Enable Banking)*
- Exigences : COL-01, COL-07, NF-RES-01, NF-RES-03, inv. 10
- Fichiers : `app/ledger/sync.py` (par compte : budget, fenêtre `date_from` = dernière date vue − 7 jours de recouvrement, soldes, transactions, mise à jour de `account_sync_state` ; 401 → connexion `expired`)
- Tests : **scénario CDC « coupure pendant une synchro »** (exception à la page 2, rejeu → aucun doublon, aucune page perdue) ; rattrapage après 5 jours d'arrêt
- Fin : tests verts

**[x] T1.14 — Import CSV avec profils par banque** *(27/09/2026 ; formats Boursorama et SG supposés, à valider avec un export réel)*
- Exigences : COL-02, COL-03, COL-08
- Fichiers : `app/connectors/file_import/csv_import.py`, `profiles.py` (encodage, séparateur, format décimal et date, correspondance des colonnes, lignes à ignorer)
- Tests : **scénario CDC « même CSV importé deux fois »** ; fichiers d'exemple anonymisés aux formats **Boursorama** et **Société Générale** (profils fournis par défaut) ; fichier mal formé → erreur claire sans import partiel
- Fin : tests verts

**[x] T1.15 — Import OFX** *(27/09/2026)*
- Exigences : COL-02
- Fichiers : `app/connectors/file_import/ofx_import.py` (enveloppe typée autour d'`ofxparse`, `FITID` → `external_id`)
- Tests : fichier OFX anonymisé ; double import sans doublon
- Fin : tests verts

**[x] T1.16a — Doublons entre sources : appariement automatique** *(Q5 ; 27/09/2026)*
- Exigences : COL-03, inv. 4
- Fichiers : `app/ledger/cross_dedupe.py`, `tests/scenarios/dedupe/*.yaml`, `tests/scenarios/runner.py` (moteur de scénarios partagé avec le lot 2)
- Règles : sur un même compte et entre sources différentes, lien automatique « doublon certain » si (a) même référence bancaire (`entryReference`, `FITID`…), ou (b) même montant + même date comptable **et** correspondance univoque. Pour des opérations identiques le même jour, appariement **par comptage** (multiset) : si les effectifs sont égaux, on apparie ; sinon, tout part en file de validation. La transaction canonique est celle de l'API quand elle existe
- Tests (scénarios YAML) : CSV qui chevauche l'API ; 3 opérations identiques le même jour des deux côtés → 3 liens ; effectifs 3 contre 2 → file ; réimport du même fichier → rien de nouveau ; API arrivée après le CSV → la transaction canonique devient celle de l'API. Propriété hypothesis : une transaction est doublon au plus une fois, et jamais de chaîne (un doublon n'est jamais canonique)
- Fin : tests verts

**[x] T1.16b — Doublons probables, report, conflits et annulation** *(27/09/2026 ; l'écran de la file arrive avec T1.25)*
- Exigences : COL-03, COL-06, ANA-01, NF-SEC-06, inv. 4
- Fichiers : `app/ledger/cross_dedupe.py` (file « doublon probable » : même montant, dates à ±3 jours, candidats multiples ; confirmation, rejet, annulation), `app/ledger/carry_over.py` (report sur la canonique), `app/categorize/review.py` (file de validation commune : doublons, conflits, virements proposés), `app/ledger/views.py` (« transactions effectives » = sans doublon confirmé, utilisé par **tous** les agrégats, la catégorisation et plus tard `rent_allocation`)
- Règles de report (validées) lors de la confirmation d'un lien :
  - Catégorisation : priorité **manuel > règle > IA**. Si la canonique n'en a pas, celle du doublon est reportée. Si le doublon a une source plus prioritaire, elle remplace celle de la canonique. Si les deux sont **manuelles et différentes**, on garde celle de la canonique et on ouvre un élément **« conflit »** dans la file. Si elles sont de même niveau non manuel, on garde celle de la canonique (elle est recalculable)
  - Liens de virement interne : même logique (manuel > automatique ; deux liens manuels différents → celui de la canonique est gardé + conflit)
  - `rent_allocation` : même logique au lot 2 (L-12)
  - Chaque ligne reportée ou remplacée garde la référence du `duplicate_link` qui l'a produite (`carried_by_link_id`, `superseded_by_link_id`). Rien n'est supprimé
- Annulation d'un lien : restaure **exactement** l'état effectif antérieur (catégorisations, liens de virement, conflits ouverts par ce lien refermés). L'historique reste dans `audit_log`
- Tests :
  - **propriété hypothesis** : pour tout état initial généré (catégorisations de toutes sources et liens de virement sur les deux transactions), *confirmer puis annuler* = **identité** sur l'état effectif ; confirmer deux fois = confirmer une fois
  - scénarios YAML : report simple ; manuel contre IA ; deux manuelles différentes → conflit ; annulation ; décalage de ±3 jours → proposition et non lien automatique
  - audit de chaque décision ; aucune ligne supprimée ni modifiée dans `ledger_transaction`
- Fin : tests verts ; la page Transactions (T1.25) affiche la file de validation, conflits compris

**[x] T1.17 — Virements internes** *(27/09/2026)*
- Exigences : COL-06
- Fichiers : `app/ledger/transfers.py`
- Tests : correspondance forte (IBAN contrepartie = IBAN d'un des comptes de l'utilisateur, montants opposés, ±3 jours) → lien automatique ; cas ambigu → proposition, jamais d'association automatique ; propriété : une transaction a au plus un lien
- Fin : tests verts ; les virements internes sont exclus des agrégats revenus et dépenses

*Catégorisation*

**[x] T1.18 — Catégories et règles** *(27/09/2026)*
- Exigences : ANA-01, ANA-04 (structure seulement), NF-SEC-06
- Fichiers : `app/categorize/rules.py` (conditions Pydantic : libellé contient ou regex, contrepartie, plage de montant, compte ; priorité), `app/categorize/seed.py` (arborescence française par défaut, idempotente), `app/categorize/service.py` (catégorisation manuelle, historique par remplacement, sans suppression)
- Tests : ordre de priorité ; règle modifiée → réapplication uniquement aux transactions non manuelles ; somme des lignes = montant de la transaction ; audit à chaque modification de règle
- Fin : tests verts

*Alertes*

**[x] T1.19 — Moteur d'alertes et cycle de vie** *(27/09/2026)*
- Exigences : ALR-01, ALR-03 (niveaux), ALR-06, inv. 3
- Fichiers : `app/alerts/engine.py` (`raise_alert(type, severity, dedupe_key, subject)` idempotent ; transitions ouverte → vue → résolue ; rappel à 48 h)
- Tests : même fait N fois → 1 alerte ; rappel unique à 48 h (horloge simulée) ; une alerte résolue puis un nouveau fait → nouvelle clé d'épisode
- Fin : tests verts

**[~] T1.20 — Canaux ntfy et e-mail (file d'envoi)** *(code fait le 27/09/2026 ; reste l'essai réel sur le téléphone : ntfy, Tailscale et SMTP à configurer)*
- Exigences : ALR-02, inv. 6
- Fichiers : `app/alerts/outbox.py`, `app/alerts/channels/ntfy.py`, `app/alerts/channels/email.py`
- Tests : faux transports ; échec → nouvel essai espacé sans doublon d'envoi ; alerte critique non vue sous 30 min → e-mail de secours (une seule fois) ; **contenu sans montant ni nom** (test qui échoue si un montant ou un nom figure dans le message) ; aucun destinataire autre que l'administrateur n'est possible au lot 1 ; point d'accroche « sauvegarde immédiate après envoi », qui servira aux envois au locataire (section 1.5)
- Fin : notification réelle reçue sur le téléphone via la pile locale

**[x] T1.21 — Alertes techniques** *(27/09/2026)*
- Exigences : ALR-08, COL-07, NF-RES-04
- Fichiers : `app/ops/checks.py` (aucune synchro réussie depuis 24 h, consentement à J-14, J-7 et J-1, disque au-dessus de 85 %, `integrity_check` quotidien, échec de sauvegarde lu dans l'état de `backup.sh`)
- Tests : **scénario CDC « consentement qui expire dans 7 jours »** ; chaque contrôle est idempotent
- Fin : tests verts

*Interface*

**[x] T1.22 — Authentification (TOTP, Q9)** *(27/09/2026 ; limite connue : un même code TOTP reste réutilisable dans sa fenêtre de 90 s)*
- Exigences : NF-SEC-04, NF-SEC-06
- Fichiers : `app/web/auth.py` (aujourd'hui `app/auth/users.py`) (argon2, TOTP avec pyotp, sessions en base à expiration glissante de 12 h et absolue de 7 jours, cookies `Secure`, `HttpOnly` et `SameSite=Strict`, CSRF, limitation des essais), `app/cli.py` (`create-admin`)
- Tests : mauvais TOTP refusé ; session expirée → redirection ; limitation après 5 échecs ; audit des connexions
- Fin : tests verts

**[~] T1.23 — Socle web, PWA et formats français** *(fait le 27/09/2026 ; reste l'installation réelle sur le téléphone via Tailscale)*
- Exigences : UI-01, UI-05
- Fichiers : `app/web/templates/base.html`, `app/web/static/vendor/` (htmx et Chart.js **embarqués** avec versions et sommes de contrôle notées), `manifest.webmanifest`, `sw.js` minimal, filtres Jinja (`1 234,56 €`, JJ/MM/AAAA en Europe/Paris)
- Tests : filtres (négatifs, zéro, grands nombres, changement d'heure) ; aucune URL externe dans les gabarits (test)
- Fin : l'application s'installe sur le téléphone via Tailscale

**[x] T1.24 — Pages Comptes et État des connecteurs** *(27/09/2026 ; synchro manuelle et état des connecteurs le 28/09/2026)*
- Exigences : COL-07, O1
- Fichiers : `app/web/routes/accounts.py`, gabarits
- Tests : TestClient (fraîcheur, statut, date d'expiration du consentement, bouton de synchro manuelle soumis au budget, désactivé avec explication s'il ne reste que de quoi faire la prochaine synchro automatique ; dernière erreur et dernier essai, retard de plus de 24 h signalé, accès consommés sur 24 h par endpoint)
- Fin : tests verts

**[x] T1.25 — Pages Transactions : recherche, catégorisation, règles, export CSV** *(27/09/2026, en trois parties : T1.25a, b et c)*
- Exigences : ANA-11, ANA-01, ANA-03 (proposition de règle), FIS-06 (CSV), NF-SEC-06
- Fichiers : `app/web/routes/transactions.py`, `rules.py`, `export.py`, `review.py` (file de validation T1.16b : doublons, conflits, virements proposés ; un clic)
- Tests : filtres combinés ; export conforme aux filtres et audité ; en-têtes CSV en français
- Fin : tests verts

**[x] T1.26 — Page d'import de fichiers** *(27/09/2026)*
- Exigences : COL-02
- Fichiers : `app/web/routes/imports.py`
- Tests : envoi du fichier, aperçu, confirmation ; réimport → « 0 nouvelle transaction »
- Fin : tests verts

**[x] T1.27 — Tableau de bord** *(27/09/2026)*
- Exigences : ANA-09, NF-PERF-02
- Fichiers : `app/ledger/aggregates.py` (cash-flow mensuel, dépenses par catégorie, taux d'épargne, patrimoine, N / N-1 ; virements internes et doublons exclus), `app/web/routes/dashboard.py`
- Tests : agrégats sur données connues ; **performance** : 10 ans de données synthétiques (environ 60 000 transactions), rendu en moins de 2 s (test marqué `slow`, lancé en CI)
- Fin : tests verts ; index ajoutés si nécessaire (migration 0002)

**[x] T1.28 — Page Alertes** *(27/09/2026)*
- Exigences : ALR-06
- Fichiers : `app/web/routes/alerts.py`
- Tests : marquer une alerte comme vue ou résolue ; filtres
- Fin : tests verts

**[ ] T1.29 — Mise en service et porte du lot 1**
- Exigences : O1, O6, porte du lot 1
- Préalable : T1.10 et T1.11 validées sur de vraies réponses anonymisées des banques réelles (sinon la porte ne peut pas être franchie)
- Tests : manuel. Liaison des banques réelles, imports historiques, puis **14 jours consécutifs** avec toutes les synchros planifiées réussies ou rattrapées, aucune alerte Healthchecks non expliquée, `SELECT` de contrôle sans doublon, et aucune intervention
- Fin : journal des 14 jours consigné ici

---

### 3.3 Lot 2 — Loyers et encaissements attendus

Démarré le 29/09/2026, tests d'abord (CONTRIBUTING.md). État dérivé (`app/rental/status.py`), moteur rejouable (`app/rental/engine.py`), décisions auditées (`app/rental/allocations.py`, `app/rental/expenses.py`).

**[x] T2.1 — Modèle et migrations 0005, 0006** : biens et quote-parts (dix-millièmes), baux ou encaissements attendus (type `loan` pour un prêt consenti à un particulier, Q17), payeurs chiffrés reconnus par IBAN ou **motif de libellé** (les banques réelles ne transmettent que le libellé), échéances, affectations (proposée, confirmée, refusée, annulée par rejet), trop-perçus, incidents, charges du bien et règles.
- Exigences : LOC-01, LOC-02, LOC-10, LOC-14, LOC-15, L-04 à L-06, inv. 2, 4, 5

**[x] T2.2 — État d'une échéance** : fonction pure, jours d'alerte (5) et d'impayé (10) du mois, réglables par bail (Q16) ; test de propriété (le temps n'améliore jamais l'état sans paiement).
- Exigences : LOC-05, regles-loyer.md

**[x] T2.3 — Moteur** : échéances (anticipées dès le 25, bornées par la fin du bail), rejets (écriture inverse mémorisée : un rejet n'annule qu'une affectation, une fois), rapprochement (payeur reconnu : confirmé, au plus ancien d'abord, plafonné ; inconnu au montant exact : proposé), trop-perçu, incidents, alertes (retard, impayé, partiel, caution, rejet, trop-perçu, à valider, retards répétés, impayés cumulés), titres sans montant ni nom ; doublons (L-12) et comptes ignorés exclus.
- Tests : **16 scénarios YAML** (`tests/scenarios/loyers`, CDC §5 et §9) joués deux fois (rejouables) ; 96 % de couverture de `app/rental`
- Exigences : LOC-03, LOC-04, LOC-05, LOC-14, L-01 à L-03, L-12, inv. 3

**[x] T2.4 — Décisions manuelles** : confirmer ou refuser une proposition, affecter à la main avec justification, dissocier, reporter ou rembourser un trop-perçu.
- Exigences : LOC-06, L-05, L-06

**[x] T2.5 — Charges du bien** : rattachement manuel ou par motif, rejouable, jamais repris après retrait manuel ; totaux par nature et par année.
- Exigences : LOC-10, FIS-01 (base)

**[x] T2.6 — Interface et worker** : `/loyers`, `/loyers/nouveau`, `/loyers/{id}`, `/biens/{id}`, tuile du tableau de bord ; tâche `rental.run` toutes les 15 min et après chaque synchro manuelle.
- Tests : TestClient et bout en bout (création dans Chromium)

**[ ] T2.7 — Porte du lot 2** : saisir les deux baux réels et le remboursement du prêt, puis **2 échéances réelles** suivies sans erreur (CDC §6).
- Préalable : noms et motifs de libellé des payeurs tels qu'ils apparaissent sur les relevés

### 3.4 Qualité transverse (29/09/2026)

**[x] Q-1 — Tests de bout en bout** (`tests/e2e`, `scripts/e2e.sh`, tâche CI `e2e`) : application en HTTP derrière un relais TLS comme `tailscale serve`, Chromium, faux Enable Banking. Pages stylées sans erreur de console, de script, de requête ni de CSP ; pas de débordement à 360 px ; graphiques dessinés ; liaison bancaire par un clic normal ; synchro, réglages, classement, export, PWA, déconnexion, création d'un bail et d'un bien. Contre-épreuve : l'ancien bug de contenu mixte fait échouer le test.
**[x] Q-2 — Couverture** mesurée (pytest-cov) : ≥ 90 % exigés en CI (93 % au 29/09/2026).
**[x] Q-3 — mypy dans le hook de pré-commit** (une erreur de typage était passée jusqu'à la CI).

### 3.5 Application Android (demande du 29/09/2026)

Choix : application **native** (Kotlin, Jetpack Compose, Material 3) sur une **API JSON v1** du serveur, plutôt qu'un habillage de la page web (qui en aurait gardé les faiblesses). La même API servira à l'analyse par IA (étape B). Les actions rares (liaison bancaire, import, règles, création de bail) restent dans la version web, ouverte depuis le menu de l'application.

**[x] A-1 — API JSON v1 et appareils** (migration 0007) : appairage par code à usage unique (10 min, anti-force brute), jeton d'appareil révocable (empreinte seule en base), page `/appareils` ; endpoints tableau de bord, comptes, synchro manuelle, transactions, catégories, loyers et propositions, alertes. Tests (TestClient) et **contrat** avec l'application (`tests/web/test_api_contract.py`).
**[x] A-2 — Application** (`android/`) : cinq onglets, verrouillage par empreinte ou code, jeton chiffré par le magasin de clés Android, captures d'écran interdites, HTTPS seul, erreurs lisibles. 16 tests JVM (Robolectric) ; lint strict.
**[x] A-3 — CI** : tests, lint, APK signé (clé dans les secrets GitHub, secours chiffré `android/signing.enc.yaml`), publié sur la release GitHub « android » (`finances.apk`).
**[x] A-4a — Tests sur émulateur Android 14** (`scripts/android-device-tests.sh`, tâche CI) : démarrage, appairage refusé en http, jeton chiffré par le magasin de clés matériel, parcours des cinq onglets et de leurs actions contre un faux serveur sur l'appareil.
**[ ] A-4b — Essai sur un vrai téléphone** : installation, appairage réel (Tailscale), empreinte digitale, ergonomie.
**[~] A-5 — Suites possibles** : ✅ widget « Loyers du mois » (07/10/2026) ; restent notifications natives (aujourd'hui : application ntfy), mode hors ligne en lecture (cache chiffré), création de bail et import depuis l'application.

### 3.6 Phase B1 — Pilotage par IA via MCP (spécification `docs/spec-mcp.md`, décisions Q20, Q22)

**[x] B1.1 — Pseudonymisation** : nettoyage des libellés, initiales, listes de noms et d'émetteurs (chiffrées), identifiants opaques, garde de sortie ; tests par propriétés d'abord.
**[x] B1.2 — Outils de lecture** (14) sur les agrégats et recherches existants.
**[x] B1.3 — Lots de modifications** : instantanés avant / après des tables d'état, annulation avec contrôle de conflit, aperçu puis confirmation des écritures de masse.
**[x] B1.4 — Outils d'écriture** (19) branchés sur les services existants.
**[x] B1.5 — Accès** : jetons à portée, interrupteurs, quotas, audit ; pages Sécurité › Accès IA et « Modifications par l'IA ».
**[x] B1.6 — Route `/mcp`** : JSON-RPC MCP 2025-06-18 en *Streamable HTTP* sans état (implémentation propre, sans dépendance serveur ; conformité vérifiée par le client du SDK officiel `mcp` dans les tests).
**[x] B1.7 — Extension client MCP** `finances.mcpb` (relais Node sans dépendance) : servie par l'application elle-même (`/ia/extension.mcpb`, adresse préremplie), relais testé (`node --test`) et manifeste validé (`mcpb validate`) en CI.
**[~] B1.8 — Déploiement et essai sur le PC** (scénarios 1 à 7 de la spécification) : déployé le 30/09 ; reste l'installation sur le PC (jeton, extension) et les scénarios.

### 3.7 Phase B2 — Assistant dans l'application Android (demande du 30/09/2026)

Onglet « Assistant » : champ texte et dictée (reconnaissance vocale d'Android), mêmes outils que le MCP, boucle d'appels côté serveur. L'abonnement d'une application d'IA ne s'utilise pas par API : fournisseur au choix (API Anthropic, OpenAI, OVH AI Endpoints), clé dans les secrets sops, hôtes ajoutés à la liste blanche du client HTTP, plafond mensuel.

**[x] B2.1 — Serveur** : `app/assistant/providers.py` (trois API, format de messages neutre), `chat.py` (8 tours d'outils au plus, conversations chiffrées — migration 0011, plafond mensuel de jetons, journal), `POST /api/v1/assistant`, page web `/assistant` avec dictée du navigateur, réglages sur la page Sécurité.
**[x] B2.2 — Application Android** : sixième onglet « Assistant » (bulles, champ texte, bouton micro par l'écran de dictée du système, nouvelle conversation), délai de lecture porté à 3 min pour cette requête ; tests JVM et sur émulateur.
**[ ] B2.3 — Essai réel** : clé API à ajouter, choix du fournisseur.

### 3.8 Phase P — Industrialisation (vision produit, Q24)

**[x] P-1 — Exigences et conformité** : CDC §10, registre `docs/conformite.md`, DT §13, principes produit dans `CONTRIBUTING.md`.
**[x] P-2 — CI sécurité** : pip-audit, licences, Semgrep (2 défauts corrigés), Trivy sur l'image, couverture ≥ 95 %.
**[~] P-3 — Fiabilité de l'hôte** : journal système **persistant** (Raspberry Pi OS le gardait en mémoire), **chien de garde du disque** (`finances-watchdog.service` : sans écriture réussie pendant 5 min, redémarrage ; le chien de garde matériel ne voyait pas un disque figé), procédure de vérification de l'alerte « Pi muet » (`docs/exploitation.md`). Reste : relancer `bootstrap.sh` sur le Pi et faire l'essai Healthchecks.
**[ ] P-4 — Configuration unique** : fichier `finances.toml` validé, secrets générés à l'installation, valeurs par défaut sûres ; plus rien de propre à un foyer dans le code (profils de banques et catégories en données).
**[ ] P-5 — Installation générique** : script pour toute machine Linux x86-64/ARM64, vérifications préalables, assistant web de premier lancement.
**[ ] P-6 — Rôles** : co-indivisaires et expert-comptable en lecture ; droits par écran et par API.
**[ ] P-7 — Chaîne de livraison** : images signées (cosign), SBOM, provenance ; canaux stable/test ; notes de version ; branche protégée.
**[ ] P-8 — Exploitation à distance** : santé détaillée protégée, paquet de diagnostic expurgé, accès de support consenti et révocable.
**[ ] P-9 — Réversibilité et RGPD** : export complet et import dans une nouvelle instance ; effacement des données d'un locataire ; durées de conservation.
**[ ] P-10 — Mode exposé** (optionnel, pour l'hébergement) : accès Internet durci, désactivé par défaut (voir contradiction avec l'invariant 7).

### 3.9 Feuille de route (`docs/feuille-de-route.md`, calendrier du 06/10/2026)

**[x] F-1 — Calendrier des échéances et taxe foncière** (FIS-04, LOC-10, LOC-09 et LOC-13 pour les rappels, COL-07) :
table `deadline` (migration 0014, nature de charge « CFE » ajoutée), page `/echeances` et fiche, tuile du tableau de bord,
rappels ntfy J-7 (réglable) puis « dépassée » (critique pour un impôt), paiement reconnu seul (montant exact, une seule
dépense entre J-60 et J+30) et rattaché comme charge du bien, échéance de l'année suivante créée au règlement,
échéances habituelles d'un clic (taxe foncière et CFE par bien, déclaration des revenus), anniversaire et fin des baux,
expiration des consentements bancaires ; export **ICS** à télécharger (sans abonnement par URL : il exigerait un jeton
dans l'adresse).
- Reste : mensualisation de la taxe foncière, import de l'avis PDF (avec l'import de factures), API v1 et Android.

**[x] F-2 — Budgets** (ANA-06, lot 3) : table `budget` (migration 0015), une enveloppe mensuelle par catégorie de
dépenses (une racine compte ses sous-catégories), dépense **nette** du mois sur les lignes du tableau de bord
(comptes comptés, doublons et virements internes exclus, ventilation comprise, remboursements déduits) ; alertes
« 80 % atteint » (info) puis « dépassé » (avertissement), une fois par mois, résolues si la dépense repasse sous le
seuil ou à la fin du mois ; page `/budgets` (mois par mois, jauges, moyenne des 3 mois précédents pour fixer le
montant, suggestions), tuile du tableau de bord, tâche `budgets.check` (15 min).

**[x] F-3 — Abonnements et anomalies** (ANA-07, ANA-08, lot 3) : détection des récurrents extraite de l'outil de
l'assistant vers un domaine pur (`app/ledger/recurring.py`), désormais tolérante à **un changement de prix** (deux
paliers stables, écart ≤ 50 %), et reprise par l'assistant (`recurrents`, `anomalies`) ; page `/abonnements`
(dépenses et recettes régulières, équivalent mensuel, hausses et baisses, attendus non reçus) ; alertes de **hausse de
prix**, de **prélèvement attendu non reçu** et d'**anomalies** des 7 derniers jours (montant inhabituel, nouveau
créancier, frais bancaires, doublon ≥ 20 €), tâche `recurring.check` à 9 h et 18 h. Moteur d'alertes : option
`once` (fait ponctuel jamais relevé après résolution à la main), utilisée par F-1 à F-3.
- Reste : abonnement en double, rappel de résiliation.

**[x] F-4 — Catégorisation automatique par IA** (ANA-02, ANA-03, lot 3) : `app/categorize/ai.py`, désactivée par
défaut (page Sécurité : interrupteur et seuil, 80 %) ; une requête par heure, 40 opérations au plus, libellé
pseudonymisé + montant + date (garde de sortie avant envoi), même fournisseur et même plafond mensuel que
l'assistant ; au-dessus du seuil, ligne de catégorisation « IA » (remplacée par une règle, jamais par-dessus un choix
manuel) ; en dessous, nouvel élément **« Catégorie à confirmer »** dans À valider (migration 0016) : accepter, choisir
une autre catégorie avec création de règle en option (ANA-03), ou laisser sans catégorie ; jamais renvoyée.
- Reste : porte du lot 3 (90 % d'un mois catégorisées sans intervention) à mesurer sur les données réelles.

**[x] F-5 — Heures silencieuses, regroupement, bilan hebdomadaire** (ALR-03, ALR-04, ALR-05, lot 3) : table
`app_setting` (migration 0017) pour les réglages de l'instance modifiables depuis l'interface (PROD-01) ; heures
silencieuses 22:00-08:00 par défaut (page Alertes) : seules les critiques partent, les autres attendent la fin de la
plage ; plusieurs notifications non critiques dues ensemble partent en **un seul message** ; bilan du lundi 8 h 10
(patrimoine, dépenses des 7 jours, budgets à surveiller, loyers du mois, alertes en cours, échéances à 14 jours),
désactivable.

**[x] F-6 — Prévision de trésorerie et solde bas prévu** (ANA-10, ALR-07, lot 3) : `app/ledger/forecast.py`,
page `/prevision` (30 ou 90 jours) : solde projeté **par compte** depuis le dernier solde connu, séries récurrentes
du compte (virements réguliers entre comptes de l'utilisateur compris, qui font varier chaque solde), loyers et remboursements
attendus sur le compte ; total des comptes comptés ; seuil d'alerte par compte (`account.low_balance_cents`,
migration 0018) ; alerte « Solde bas prévu » à 30 jours, matin et soir (`forecast.check`).

**[x] F-7 — Récapitulatif annuel par bien et indivision** (FIS-01, FIS-06, LOC-12, lot 4 avancé) : `app/tax/summary.py`,
section « Récapitulatif » de la page du bien et export CSV : loyers dus et encaissés, charges par nature, part
récupérable de la taxe foncière, résultat ; part de chaque indivisaire (plus forts restes, nature par nature : sommes
exactes au centime) ; mention « indicatif » et sources (CGI art. 50-0, 39, 39 C).
- Reste : solde entre indivisaires (qui a payé quoi), régime réel (amortissements, 2031/2033), comparaison micro-BIC.

**[x] F-8 — Révision annuelle du loyer (IRL)** (LOC-09, LEG-04, lot 4 avancé) : table `irl_index` et trimestre de
référence du bail (migration 0019), `app/rental/irl.py` : proposition 30 jours avant l'anniversaire (ou en retard,
sans rétroactivité : à partir du mois en cours), loyer hors charges × indice du même trimestre un an plus tard ÷
indice de référence, arrondi une fois ; application après validation (bail et échéances à venir via `update_lease`,
l'indice appliqué devient la référence), auditée ; section « Révision annuelle » de la fiche du bail.
- Reste : import automatique de l'indice (INSEE, série 001515333 : SDMX en XML, à lire avec un analyseur durci),
  lettre au locataire, alerte « ancien montant après révision ».

**[x] F-9 — Patrimoine** (COL-05, ANA-09) : tables `manual_asset` et `asset_valuation` (migration 0020), page
`/patrimoine` : livrets, assurance-vie, PEA, PER, titres, bien (quote-part détenue), crédit (capital restant dû, en négatif), prêt
accordé ; une valeur par date, historique et évolution ; patrimoine net = comptes comptés + actifs comptés ; ligne sur
le tableau de bord.
- Reste : tableau d'amortissement d'un crédit (intérêts déductibles au réel), import de relevés d'épargne.

**[x] F-10 — Quittances de loyer** (LOC-07, LEG-04) : `app/rental/receipts.py` et générateur PDF maison `app/pdf.py`
(texte, Helvetica, A4, sans dépendance ni bibliothèque système ; WeasyPrint écarté pour l'instant) ; migration 0021
(part des charges par échéance, reprise des baux existants ; bailleur(s) du bien, chiffré). Quittance proposée sur
chaque mois entièrement payé (lien « Quittance » de la fiche du bail), aperçu puis PDF téléchargé ; ce qui manque
(bailleur, adresse du logement, locataire) est dit avec le lien pour le compléter. Rien n'est envoyé (inv. 6) ;
établissement journalisé sans donnée personnelle.

**[x] F-11 — Dépôt de garantie** (LEG-04) : `app/rental/deposit.py`, table `deposit_event` et remise des clés / état
des lieux de sortie sur le bail (migration 0022). Encaissement, retenues (motif obligatoire) et restitution, chacun
avec son opération bancaire s'il y en a une ; une opération rattachée au dépôt sort du rapprochement des loyers (et
une opération déjà affectée au loyer est refusée avec la marche à suivre). Plafond légal signalé, date limite de
restitution dans le calendrier des échéances et l'export ICS, rappel 7 jours avant, alerte de retard (pénalité
légale de retard non calculée, par choix) ; alertes résolues d'elles-mêmes une fois le dépôt soldé.

## 4. Ordre d'exécution et dépendances

```
0A : T0.1 → T0.2 → T0.3
            T0.2 → T0.4 → T0.5 → T0.6
            T0.1 → T0.7
            T0.4 + T0.7 → T0.8
            T0.7 + T0.10 → T0.9
            T0.2 → T0.10
            T0.8 + T0.9 + T0.10 → T0.11
            T0.9 → T0.12
0B : (0A terminée + matériel + comptes) → T0.13 → T0.14 → T0.16 → T0.15   ◄ porte de l'étape 0

Lot 1 (peut commencer après T0.10, en parallèle de 0B) :
  T1.1 → T1.2 (Q3) → T1.3
  T1.2 → T1.4 → T1.5
  T1.2 → T1.6 → T1.7 → T1.8
  T1.2 + T1.8 → T1.9 → T1.14 → T1.15 → T1.16a → T1.16b
  T1.7 + T1.9 → T1.10 (Q2) → T1.11 → T1.13 (+ T1.5, T1.6)
  T1.10 + T1.22 + T1.23 → T1.12
  T1.9 → T1.17 ; T1.9 → T1.18
  T1.4 → T1.19 → T1.20 → T1.21
  T1.23 → T1.22 → T1.24, T1.25, T1.26, T1.27, T1.28
  Tout + T0.15 → T1.29   ◄ porte du lot 1 (14 jours)
```

Ordre linéaire proposé : T0.1 → T0.2 → T0.3 → T0.4 → T0.5 → T0.6 → T0.7 → T0.10 → T0.8 → T0.9 → T0.11 → T0.12 → T1.1 → T1.2 → T1.3 → T1.4 → T1.5 → T1.6 → T1.7 → T1.8 → T1.9 → T1.14 → T1.15 → T1.16a → T1.16b → T1.17 → T1.18 → T1.19 → T1.20 → T1.21 → T1.23 → T1.22 → T1.24 → T1.25 → T1.26 → T1.27 → T1.28 → T1.10 → T1.11 → T1.13 → T1.12 → (0B : T0.13 → T0.14 → T0.16 → T0.15) → T1.29.
Le connecteur Enable Banking passe en fin de lot pour laisser le temps de créer l'application EB et d'enregistrer les cassettes (Q2, Q6).

> Écart assumé avec le CDC (« un lot ne démarre qu'une fois la porte du précédent franchie ») : le lot 1 démarre avant la porte 0B, mais **sa mise en service (T1.29) attend la porte T0.15**. À valider (Q10).

---

## 5. Modèle de données initial (lot 1) et migration 0001

Conventions : clés primaires `id INTEGER` ; montants `*_cents INTEGER NOT NULL` ; instants `*_at` en DATETIME UTC (`UTCDateTime`) ; dates calendaires en `DATE` ; énumérations en `TEXT` avec `CHECK` ; colonnes `*_enc` chiffrées en AES-GCM et `*_hash` en HMAC-SHA256 (Q3) ; nommage des contraintes `pk_`, `fk_`, `uq_`, `ck_`, `ix_`.

### 5.1 Tables

| Table | Colonnes principales | Contraintes et index | Remarque |
|---|---|---|---|
| `app_user` | username, password_hash, totp_secret_enc, role (`admin`, `co_owner`), created_at, last_login_at | uq(username) | Rôle prévu pour UI-04 (L-08) |
| `user_session` | user_id→app_user, token_hash, created_at, last_seen_at, expires_at, revoked_at | uq(token_hash), ix(expires_at) | NF-SEC-04 |
| `link_nonce` | nonce_hash (PK, empreinte SHA-256 du `state`), created_at, expires_at, used_at | ix(expires_at) | Migration `0002` : retour de liaison bancaire à usage unique (T1.12) |
| `connection` | provider (`enable_banking`, `file`), institution_name, country, status (`pending`, `active`, `expired`, `error`, `revoked`), provider_session_enc, consent_expires_at, created_at, updated_at | ix(consent_expires_at) | COL-07 |
| `account` | connection_id→connection (nullable pour les comptes alimentés seulement par fichier), provider_account_uid, name, type (`checking`, `savings`, `card`, `other`), iban_enc, iban_hash, currency CHAR(3), include_in_net_worth, syncs_per_day (1 à 4), opened_at, closed_at | uq(connection_id, provider_account_uid), ix(iban_hash), ck(syncs_per_day BETWEEN 1 AND 4) | `iban_hash` sert à COL-06 et au lot 2 |
| `account_sync_state` | account_id (PK, FK), last_attempt_at, last_success_at, last_booking_date_seen DATE, last_error | — | État modifiable, séparé de la donnée brute |
| `provider_call` | account_id→account (nullable), connection_id, requested_at, unattended BOOL, method, path_template, http_status, job_run_id | ix(account_id, requested_at) | Budget DSP2 (I-10) |
| `import_profile` | name, institution_name, format (`csv`, `ofx`), encoding, delimiter, decimal_sep, date_format, skip_rows, column_map JSON | uq(name) | COL-02 |
| `import_batch` | account_id→account, source (`enable_banking`, `csv`, `ofx`), profile_id→import_profile, file_name, file_sha256, started_at, finished_at, status, n_seen, n_inserted, n_duplicates, job_run_id | ix(account_id, started_at) | Traçabilité des imports |
| `raw_payload` | import_batch_id→import_batch, source, received_at, content_sha256 (du clair), content_type, content_enc BLOB (compressé puis chiffré) | uq(content_sha256) | COL-08 ; rejouable |
| `ledger_transaction` | account_id→account, dedupe_key, external_id, fingerprint, occurrence, booking_date DATE, value_date DATE, amount_cents, currency, counterparty_name_enc, counterparty_iban_enc, counterparty_iban_hash, label_raw, label_clean, bank_tx_code, raw_payload_id→raw_payload, import_batch_id→import_batch, created_at | **uq(account_id, dedupe_key)**, ix(account_id, booking_date), ix(booking_date), ix(counterparty_iban_hash) — *montant nul autorisé (écritures à 0 € de certaines banques)* | **Trigger `BEFORE UPDATE` → `RAISE(ABORT)`** (inv. 4). `label_clean` est calculé une seule fois à l'insertion |
| `balance_snapshot` | account_id→account, as_of_date DATE, balance_type (`booked`, `available`, `other`), amount_cents, currency, fetched_at, raw_payload_id | uq(account_id, as_of_date, balance_type) | O1, patrimoine |
| `transfer_link` | debit_tx_id→ledger_transaction, credit_tx_id→ledger_transaction, method (`auto`, `manual`), status (`proposed`, `confirmed`, `rejected`, `superseded`), carried_by_link_id→duplicate_link, superseded_by_link_id→duplicate_link, created_at, decided_at | uq(debit_tx_id) et uq(credit_tx_id) partiels `WHERE status IN ('proposed','confirmed')`, ck(debit_tx_id <> credit_tx_id) | COL-06 ; seul `confirmed` est exclu des agrégats |
| `duplicate_link` | canonical_tx_id→ledger_transaction, duplicate_tx_id→ledger_transaction, mode (`auto`, `manual`), reason (`reference`, `exact`, `multiset`, `probable`), status (`proposed`, `confirmed`, `rejected`, `reverted`), created_at, decided_at | uq(duplicate_tx_id) partiel `WHERE status IN ('proposed','confirmed')`, ck(canonical_tx_id <> duplicate_tx_id) | Q5 ; réversible, audité ; rien n'est supprimé |
| `category` | parent_id→category, name, kind (`expense`, `income`, `transfer`, `neutral`), sort_order, archived_at | uq(parent_id, name) | Arborescence |
| `rule` | name, priority, is_active, conditions JSON, category_id→category, created_at, updated_at | ix(is_active, priority) | ANA-01 |
| `categorization` | transaction_id→ledger_transaction, category_id→category, amount_cents, source (`rule`, `ai`, `manual`), rule_id→rule, confidence_permille (0 à 1000, nullable), carried_by_link_id→duplicate_link, created_at, superseded_at, superseded_by_link_id→duplicate_link | ix(transaction_id) partiel `WHERE superseded_at IS NULL` | Ventilation (ANA-04) ; somme des lignes = montant (vérifiée par le service) |
| `review_item` | kind (`duplicate`, `category_conflict`, `transfer_conflict`, `transfer_candidate`), dedupe_key, duplicate_link_id→duplicate_link, transaction_id→ledger_transaction, other_transaction_id→ledger_transaction, status (`open`, `resolved`, `dismissed`), details JSON, created_at, resolved_at | uq(dedupe_key), ix(status, kind) | File de validation commune (T1.16b) ; idempotente |
| `alert` | type, severity (`info`, `warning`, `critical`), status (`open`, `seen`, `resolved`), dedupe_key, subject_type, subject_id, title (sans montant ni nom), details JSON, opened_at, seen_at, resolved_at, last_reminded_at | **uq(dedupe_key)**, ix(status, severity) | ALR-01, ALR-06 |
| `notification` | alert_id→alert, channel (`ntfy`, `email`), kind (`initial`, `reminder`, `fallback`), sequence, attempt_count, next_attempt_at, status (`pending`, `sent`, `failed`), sent_at, last_error | uq(alert_id, channel, kind, sequence) | File d'envoi idempotente |
| `job_run` | job_name, slot_start (UTC), status (`running`, `success`, `failed`, `skipped`), started_at, heartbeat_at, finished_at, attempt, error, stats JSON | **uq(job_name, slot_start)**, ix(status) | inv. 3 |
| `audit_log` | at, actor_user_id, action, object_type, object_id, details JSON | ix(at) | **Trigger `BEFORE UPDATE`/`DELETE` → `RAISE(ABORT)`** ; NF-SEC-06 |

Reportées aux lots suivants : `property`, `owner_share`, `lease`, `party`, `rent_due`, `rent_allocation`, `rent_incident`, `expense_share`, `irl_index`, `document`, table des actifs manuels (COL-05), `tag`, `budget`.

### 5.2 Migration `0001_socle` (description, non écrite)

1. `upgrade()` crée, dans l'ordre des dépendances : `app_user`, `user_session`, `connection`, `account`, `account_sync_state`, `import_profile`, `import_batch`, `raw_payload`, `ledger_transaction`, `balance_snapshot`, `duplicate_link`, `transfer_link`, `category`, `rule`, `categorization`, `review_item`, `alert`, `notification`, `job_run`, `provider_call`, `audit_log`.
2. Crée les index, y compris les index partiels (`sqlite_where=`).
3. Crée par `op.execute` les triggers d'immuabilité sur `ledger_transaction` (UPDATE) et `audit_log` (UPDATE et DELETE).
4. Ne contient **aucune donnée** : l'arborescence de catégories est posée par `finances seed`, une commande idempotente, pour garder la migration purement structurelle.
5. `downgrade()` supprime triggers et tables dans l'ordre inverse (développement seulement ; en production on ne fait qu'avancer, en expand/contract).
6. Vérifications : `alembic upgrade head` puis `alembic check` sans écart ; `downgrade base` puis `upgrade head` idempotents en CI.

---
