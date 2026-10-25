# Cahier des charges — Système de gestion des finances personnelles

Sep 27, 2026

## 1. Contexte et objectifs

Le système centralise toutes les finances personnelles en un seul endroit et alerte automatiquement sur les écarts, en priorité les loyers absents ou incorrects. Il reste en lecture seule : il observe, analyse et prévient, mais ne déplace jamais d'argent.

**Situation de départ**

- Plusieurs comptes dans plusieurs banques, sans vue consolidée.
- Un appartement meublé détenu en indivision (par exemple 50/50 entre deux indivisaires), actuellement loué.
- Loyer payé par virement mensuel, avec une caution solidaire.
- D'éventuels actifs crypto, voire un bot de trading sur une machine distincte : **hors périmètre** depuis le 29/09/2026, non suivis par ce système.
- Des dépenses courantes non suivies ni catégorisées.

**Objectifs et indicateurs de réussite**

| Objectif | Indicateur | Cible |
| --- | --- | --- |
| O1 · Vue consolidée | Fraîcheur des soldes de tous les comptes | Moins de 24 h |
| O2 · Aucun loyer oublié | Délai entre fin de tolérance et alerte | Moins de 1 h après la synchro |
| O3 · Dépenses lisibles sans effort | Transactions catégorisées sans intervention | 90 % ou plus |
| O4 · Déclaration fiscale simplifiée | Temps de préparation annuelle (locatif) | Moins de 1 h |
| O5 · Sécurité | Capacité du système à initier un paiement | Nulle |
| O6 · Fiabilité | Jours sans synchro non signalés | Zéro |

## 2. Périmètre et utilisateurs

Le périmètre couvre la collecte, l'analyse, la gestion locative, la préparation fiscale et les alertes. Tout ce qui déplace de l'argent ou engage juridiquement reste hors système.

**Inclus**

- Agrégation en lecture seule des comptes bancaires et des actifs saisis à la main.
- Catégorisation, budgets, récurrents, anomalies, tableaux de bord et prévisions.
- Gestion locative : baux, échéancier, rapprochement des loyers, quittances, révision IRL, comptes d'indivision.
- Préparation fiscale : récapitulatifs et calculs indicatifs, exports pour un comptable.
- Alertes multicanal, assistant IA en langage naturel, accès mobile.
- Un modèle de données prêt pour plusieurs biens et plusieurs baux.

**Exclu**

- Initiation de virements, de paiements ou d'ordres de bourse et crypto.
- Télédéclaration et liasse fiscale certifiée (LMNP au réel : rôle de l'expert-comptable).
- Conseil en investissement.
- Pilotage d'un bot de trading : le système lit ses résultats, il ne le commande pas.

**Utilisateurs et rôles**

| Rôle | Qui | Droits |
| --- | --- | --- |
| Administrateur | Utilisateur principal | Tout : comptes, règles, paramètres, exports, envois |
| Co-indivisaire | Un autre indivisaire | Lecture du bien indivis : loyers, charges, quittances, solde d'indivision ; aucun accès aux comptes personnels |
| Locataire | Destinataire externe | Reçoit quittances et relances par e-mail ; aucun accès au système |
| Assistant IA | Composant du système | Lecture des données pseudonymisées ; écritures (catégories, règles, loyers) seulement si autorisées, journalisées et annulables ; aucune action financière ni envoi sans validation |
| Expert-comptable *(produit)* | Externe | Lecture des exports et récapitulatifs fiscaux d'un exercice ; aucun accès aux comptes |

## 3. Sources de données

Chaque source passe par un connecteur interchangeable, avec l'import de fichier comme solution de secours permanente. Le choix des fournisseurs relève du dossier technique.

| Source | Mode de collecte | Fréquence | Données récupérées |
| --- | --- | --- | --- |
| Comptes courants et livrets (plusieurs banques) | API DSP2 via un agrégateur, lecture seule | Quotidienne | Soldes, transactions, contrepartie, IBAN émetteur si fourni |
| Compte d'encaissement des loyers | API DSP2 | 4 fois par jour, plafond DSP2 sans action de l'utilisateur | Idem, avec priorité sur les virements entrants |
| ~~Plateformes crypto~~ | Hors périmètre (29/09/2026) | — | — |
| Bot de trading | Lecture de sa base ou de ses exports | Quotidienne | Positions et résultat réalisé |
| Assurance-vie, PEA, PER éventuels | Saisie manuelle ou import | Mensuelle | Valeur, versements |
| Bien immobilier | Saisie manuelle | Annuelle | Valeur estimée, capital restant dû si crédit |
| Relevés CSV, OFX | Import manuel | À la demande | Transactions (secours ou historique ancien) |
| Justificatifs (factures, tickets) | Photo ou PDF, OCR | À la demande | Date, montant, marchand, rattachés à une transaction |
| Indice IRL de l'INSEE | Source publique | Trimestrielle | Valeur de l'indice pour la révision des loyers |

**Exigences transverses de collecte**

- Une même transaction importée deux fois n'apparaît qu'une fois.
- Les données brutes reçues sont conservées pour pouvoir rejouer un import.
- Une synchro manquée récupère automatiquement la période manquante au passage suivant.
- L'expiration d'un consentement bancaire est anticipée et signalée avant la coupure.

## 4. Exigences fonctionnelles

Les 53 exigences sont réparties en six modules, chacune avec un identifiant et une priorité MoSCoW : M (indispensable), S (important), C (souhaitable).

### 4.1 Collecte et centralisation (COL)

| ID | Exigence | Priorité |
| --- | --- | --- |
| COL-01 | Connecter plusieurs banques en lecture seule via DSP2, avec synchro quotidienne automatique | M |
| COL-02 | Importer des relevés CSV et OFX, avec un mapping de colonnes mémorisé par banque | M |
| COL-03 | Dédoublonner les transactions (identifiant externe, sinon empreinte date + montant + libellé) | M |
| COL-04 | ~~Connecter des plateformes crypto en lecture seule~~ : hors périmètre (29/09/2026) | W |
| COL-05 | Saisir les actifs non connectés (bien, assurance-vie, PEA) avec historique de valeur | S |
| COL-06 | Détecter les virements entre comptes du périmètre et les exclure des revenus et dépenses | M |
| COL-07 | Afficher l'état de chaque connecteur et alerter avant l'expiration d'un consentement | M |
| COL-08 | Conserver les données brutes pour rejouer un import | S |

### 4.2 Catégorisation et analyse (ANA)

| ID | Exigence | Priorité |
| --- | --- | --- |
| ANA-01 | Catégoriser par règles utilisateur (libellé, contrepartie, montant, compte), prioritaires sur l'IA | M |
| ANA-02 | Catégoriser par IA les transactions sans règle, avec un score de confiance ; sous le seuil, file « à valider » | S |
| ANA-03 | Proposer de créer une règle après chaque correction manuelle | S |
| ANA-04 | Ventiler une transaction sur plusieurs catégories | S |
| ANA-05 | Ajouter des étiquettes libres (projet, voyage, travaux) | C |
| ANA-06 | Définir des budgets mensuels par catégorie, alerte à 80 % et 100 % | S |
| ANA-07 | Détecter les récurrents (fréquence, montant, prochaine date) ; alerter sur hausse de prix ou récurrent disparu | S |
| ANA-08 | Détecter les anomalies : doublon probable, montant inhabituel, frais bancaires, nouveau créancier | S |
| ANA-09 | Tableaux de bord : cash-flow mensuel, dépenses par catégorie, taux d'épargne, patrimoine net, comparaison N / N-1 | M |
| ANA-10 | Prévoir la trésorerie à 30 et 90 jours à partir des récurrents et loyers attendus | C |
| ANA-11 | Rechercher et filtrer l'historique sur tous les critères | M |

### 4.3 Gestion locative (LOC)

| ID | Exigence | Priorité |
| --- | --- | --- |
| LOC-01 | Fiche bien : adresse, type de location, quote-parts des indivisaires | M |
| LOC-02 | Fiche bail : locataire, compte d'encaissement, date d'effet, loyer HC, charges, échéance, dépôt de garantie, caution, date de révision, indice de référence | M |
| LOC-03 | Générer l'échéancier mensuel des loyers attendus à partir du bail | M |
| LOC-04 | Rapprocher automatiquement encaissements et échéances selon les règles de la section 5 | M |
| LOC-05 | Déclencher les alertes de loyer de la section 5 | M |
| LOC-06 | Associer ou dissocier manuellement un virement et une échéance, et justifier un écart | M |
| LOC-07 | Générer une quittance PDF au paiement complet, envoyée au locataire après validation | S |
| LOC-08 | Préparer avis d'échéance et relances à partir de modèles, envoyés uniquement après confirmation | S |
| LOC-09 | Révision IRL : rappel 30 jours avant la date anniversaire, calcul du nouveau loyer, mise à jour de l'échéancier après validation | S |
| LOC-10 | Rattacher au bien ses charges : copropriété, taxe foncière, assurance PNO, travaux, crédit éventuel | M |
| LOC-11 | Calculer la rentabilité brute et nette de charges par bien | S |
| LOC-12 | Tenir le compte d'indivision : quote-parts, qui a payé quoi, solde entre indivisaires, export | S |
| LOC-13 | Rappels administratifs : attestation d'assurance du locataire, échéance du bail, restitution du dépôt | C |
| LOC-14 | Historiser les incidents de paiement par bail | S |
| LOC-15 | Gérer plusieurs biens et plusieurs baux successifs | S |

### 4.4 Fiscalité et justificatifs (FIS)

| ID | Exigence | Priorité |
| --- | --- | --- |
| FIS-01 | Récapituler par année civile les recettes et charges de chaque bien meublé | S |
| FIS-02 | Comparer de façon indicative micro-BIC et réel, avec des paramètres fiscaux modifiables par année | C |
| FIS-03 | ~~Plus-values crypto (formulaire 2086)~~ : hors périmètre (29/09/2026) | W |
| FIS-04 | Calendrier fiscal avec rappels : déclaration de revenus, taxe foncière, CFE | S |
| FIS-05 | Stocker les justificatifs avec OCR et rattachement automatique à la transaction | S |
| FIS-06 | Exporter en CSV, Excel ou PDF par période, catégorie ou bien | M |

### 4.5 Alertes et notifications (ALR)

| ID | Exigence | Priorité |
| --- | --- | --- |
| ALR-01 | Moteur d'alertes à règles configurables : condition, seuil, canal, criticité | M |
| ALR-02 | Au moins deux canaux : notification push sur téléphone et e-mail | M |
| ALR-03 | Trois niveaux : info, avertissement, critique ; le critique ignore les heures silencieuses | S |
| ALR-04 | Heures silencieuses et regroupement des alertes non urgentes | S |
| ALR-05 | Digest hebdomadaire : soldes, dépenses vs budget, loyers, alertes ouvertes | S |
| ALR-06 | Cycle de vie d'une alerte (ouverte, vue, résolue), rappel si non traitée sous 48 h, pas de doublon | S |
| ALR-07 | Alerter sur solde bas ou découvert prévisible, seuil par compte | S |
| ALR-08 | Alertes techniques : synchro en échec, sauvegarde en échec, disque presque plein | M |

**Cadence des notifications (décision du 17/10/2026)** : au plus **un message par 24 h et par
canal** (téléphone, e-mail). Tout ce qui est dû part en un seul message regroupé, critiques
comprises ; ce qui arrive ensuite attend la fin des 24 h. Une alerte critique peut donc attendre
jusqu'à 24 h hors de l'interface, qui l'affiche immédiatement. Le critique ignore toujours les
heures silencieuses (ALR-03), dans cette limite.

### 4.6 Interface et assistant (UI)

| ID | Exigence | Priorité |
| --- | --- | --- |
| UI-01 | Application web responsive installable sur téléphone (PWA) | M |
| UI-02 | Accès distant sécurisé depuis l'extérieur du domicile | M |
| UI-03 | Assistant IA en langage naturel (client MCP de bureau, puis assistant intégré), réponses appuyées sur les transactions citées ; peut catégoriser, gérer règles et loyers avec confirmation, journal et annulation (jamais d'argent, d'accès ni d'envoi au locataire) | S |
| UI-04 | Accès restreint pour un co-indivisaire, limité au bien indivis | C |
| UI-05 | Interface en français, montants en euros, dates au format JJ/MM/AAAA | M |

## 5. Règles métier : loyers et alertes

Chaque mois, le bail génère une échéance « Attendu » dont le montant et la date limite viennent du bail. Les alertes partent à des jours fixes du mois, réglables par bail : le 5 (rien reçu) et le 10 (toujours rien) par défaut, ce qui absorbe les délais de virement et les week-ends.

&#91;embedded content: cycle de vie d'une échéance de loyer · 5 états\]

Un paiement tardif reste tracé comme incident même une fois l'échéance soldée ; seul « Payé » clôt une échéance sans alerte ouverte.

**Règles de rapprochement**

- Fenêtre de recherche : du 25 du mois précédent à la fin du mois concerné, pour capter les paiements anticipés.
- Un paiement tardif est affecté en priorité à l'échéance impayée la plus ancienne du bail, même hors de la fenêtre.
- Correspondance forte : émetteur connu (IBAN ou nom du locataire, de la caution ou d'un tiers payeur déclaré) et montant exact.
- Plusieurs virements peuvent solder une même échéance ; un virement unique peut couvrir plusieurs mois.
- Correspondance faible (bon montant, émetteur inconnu, ou l'inverse) : validation manuelle, jamais d'association silencieuse.
- L'affectation à une échéance est plafonnée au montant dû ; le trop-perçu est affecté à l'échéance suivante ou marqué à rembourser, sur décision de l'administrateur.

**Alertes de loyer**

| Situation | Déclencheur | Criticité | Action proposée |
| --- | --- | --- | --- |
| Rien reçu | Le 5 du mois (réglable par bail) | Avertissement | Notification simple |
| Toujours rien | Le 10 du mois (réglable par bail) | Critique | Notification + modèle de relance formelle |
| Paiement partiel | Encaissement inférieur au montant attendu | Avertissement | Notification avec le reste dû |
| Trop-perçu | Encaissement supérieur au montant attendu | Info | Choisir : report ou remboursement |
| Émetteur inattendu | Bon montant, IBAN ou nom inconnu | Info | Validation manuelle du rapprochement |
| Paiement par la caution | Émetteur = caution solidaire | Avertissement | Signal de difficulté du locataire |
| Ancien montant après révision | Montant = loyer d'avant la révision IRL | Info | Rappel au locataire du nouveau montant |
| Virement rejeté | Écriture inverse d'un loyer encaissé | Critique | Échéance recalculée (« Attendu », « En retard » ou « Impayé » selon la date) |
| Retards répétés | 2 retards sur 6 mois glissants | Avertissement | Point de vigilance sur le bail |
| Impayés cumulés | 2 échéances ou plus impayées | Critique | Rappel des recours : caution, assurance éventuelle |

Aucun message n'est envoyé au locataire sans validation explicite ; l'envoi automatique des quittances est une option désactivée par défaut.

## 6. Priorisation et lots

Le projet se livre en quatre lots successifs ; les lots 1 et 2 forment le MVP et couvrent toutes les exigences M. Un lot ne démarre qu'une fois la porte du précédent franchie.

&#91;embedded content: découpage en 4 lots · 3 portes de sortie\]

Version texte du découpage (fait foi ; mise à jour du 27/09/2026). Un astérisque signale une exigence que le dossier technique n'affectait à aucun lot et qui a été placée lors de la planification.

| Lot | Valeur livrée | Exigences | Porte de sortie |
| --- | --- | --- | --- |
| 0 · Infrastructure | Machine, sauvegardes, chaîne de mise à jour | NF-EXP-01, NF-SEC-02, NF-SEC-05, NF-SEC-08, NF-RES-02, NF-RES-04 | Reconstruction complète réussie sur un support vierge (SSD recommandé) |
| 1 · Socle | Vue consolidée des comptes, fiable et sécurisée | COL-01, COL-02, COL-03, COL-06, COL-07, COL-08\*, ANA-01, ANA-03\*, ANA-09, ANA-11, FIS-06 (export CSV), ALR-01, ALR-02, ALR-03 (niveaux)\*, ALR-06\*, ALR-08, UI-01, UI-02, UI-05, NF-SEC-04 | 14 jours de synchro sans intervention |
| 2 · Loyers | Aucun loyer oublié | LOC-01, LOC-02, LOC-03, LOC-04, LOC-05, LOC-06, LOC-10, LOC-14\*, LOC-15\* | Scénarios de la section 9 au vert, puis 2 échéances réelles |
| 3 · Dépenses | Dépenses lisibles sans effort | ANA-02, ANA-04\*, ANA-05\*, ANA-06, ANA-07, ANA-08, ANA-10\*, COL-05\*, ALR-03 (heures silencieuses)\*, ALR-04\*, ALR-05, ALR-07\* | 90 % des transactions d'un mois catégorisées sans intervention |
| 4 · Fiscalité | Déclaration simplifiée et services complets | LOC-07, LOC-08\*, LOC-09, LOC-11\*, LOC-12, LOC-13\*, FIS-01, FIS-02\*, FIS-04\*, FIS-05, FIS-06 (Excel, PDF), UI-03, UI-04\* | Récapitulatif annuel produit sans retouche manuelle |

La priorité de chaque exigence reste celle de la section 4 ; un lot regroupe des exigences par valeur livrée, pas par priorité stricte.

**Hors de cette version (W)** : suivi des actifs crypto (COL-04, FIS-03), initiation de paiements, télédéclaration, devises autres que l'euro hors crypto, application mobile native.

## 7. Exigences non fonctionnelles

La sécurité et la reprise après panne priment sur les fonctionnalités : un système financier qui fuit ou se tait sans prévenir est pire que pas de système.

| ID | Domaine | Exigence |
| --- | --- | --- |
| NF-SEC-01 | Sécurité | Lecture seule stricte : aucun droit d'initiation de paiement demandé, clés crypto sans droit de trading ni de retrait |
| NF-SEC-02 | Sécurité | Secrets (jetons, clés API) chiffrés au repos, jamais dans le code ni dans le dépôt Git |
| NF-SEC-03 | Sécurité | Sauvegardes et répliques chiffrées côté client ; données sensibles (IBAN, noms, coordonnées du locataire, données brutes reçues) chiffrées au niveau applicatif ; disque local non chiffré, protégé par l'absence d'exposition réseau (risque de vol physique de la machine accepté) |
| NF-SEC-04 | Sécurité | Authentification forte : mot de passe + second facteur (TOTP ou passkey), sessions qui expirent |
| NF-SEC-05 | Sécurité | Aucune exposition directe sur Internet : accès distant par VPN ou tunnel authentifié |
| NF-SEC-06 | Sécurité | Journal d'audit : connexions, exports, modifications de règles, messages envoyés au locataire |
| NF-SEC-07 | Sécurité | Données envoyées à une IA externe minimisées et pseudonymisées (IBAN masqués, noms remplacés), ou modèle local au choix |
| NF-SEC-08 | Sécurité | Dépendances et système d'exploitation mis à jour, vulnérabilités suivies |
| NF-RES-01 | Résilience | Reprise automatique après coupure ou redémarrage, sans perte ni doublon (traitements idempotents) |
| NF-RES-02 | Résilience | Sauvegarde automatique toutes les 15 min avec copie chiffrée côté client hors site (perte maximale de données : 15 min) ; restauration testée automatiquement au moins chaque trimestre (chaque semaine en pratique) |
| NF-RES-03 | Résilience | Rattrapage automatique des périodes non synchronisées |
| NF-RES-04 | Résilience | Surveillance externe : alerte si le système n'a donné aucun signe de vie depuis 24 h |
| NF-PERF-01 | Performance | Fonctionne sur un Raspberry Pi 4 (4 Go) dédié ; ne partage jamais d'hôte avec un bot de trading ni avec des clés capables de trader ou de retirer |
| NF-PERF-02 | Performance | Tableaux de bord affichés en moins de 2 s sur 10 ans d'historique |
| NF-DATA-01 | Données | Données du locataire limitées au nécessaire (nom, IBAN, e-mail), supprimables en fin de relation |
| NF-DATA-02 | Données | Justificatifs fiscaux conservés 6 ans par défaut, durée modifiable |
| NF-DATA-03 | Données | Export complet des données dans un format ouvert, pour pouvoir changer d'outil |
| NF-EXP-01 | Exploitation | Déploiement reproductible et configuration versionnée |
| NF-EXP-02 | Exploitation | Tests automatisés sur le rapprochement des loyers et les alertes, à partir de scénarios |
| NF-EXP-03 | Exploitation | Connecteurs isolés derrière une interface commune, remplaçables sans toucher au reste |

## 8. Contraintes, hypothèses et risques

Le risque principal est la dépendance au fournisseur d'agrégation bancaire : l'architecture doit survivre à sa disparition.

**Contraintes**

- Usage personnel, un seul administrateur, hébergement local privilégié.
- L'accès DSP2 passe obligatoirement par un prestataire agréé, souvent payant ou réservé aux professionnels.
- Le consentement bancaire expire périodiquement et exige une réauthentification forte manuelle.
- Matériel partagé ou modeste : consommation mémoire et écritures disque à maîtriser.

**Hypothèses**

- Le loyer arrive par virement sur un compte que le système peut synchroniser.
- Chaque banque utilisée est couverte par au moins un agrégateur, sinon par l'import de fichiers.
- Les résultats fiscaux sont indicatifs et validés par l'utilisateur ou un professionnel.

**Risques**

| Risque | Conséquence | Parade |
| --- | --- | --- |
| L'agrégateur ferme ou change ses conditions | Synchro coupée | Connecteurs interchangeables, import de fichier en secours, alerte de santé |
| Banque non couverte ou API instable | Trous dans les données | Import manuel, rattrapage automatique |
| Libellé ou émetteur du loyer qui change | Fausse alerte ou loyer non détecté | Correspondance montant + émetteur + fenêtre, validation manuelle, tests de scénarios |
| Accès non autorisé au serveur | Fuite de données financières | Pas d'exposition directe, second facteur, chiffrement |
| Message erroné envoyé au locataire | Relation dégradée | Aucun envoi sans validation par défaut |
| Panne matérielle (carte SD, alimentation) | Perte de données, silence du système | SSD recommandé, sauvegarde hors site toutes les 15 min, surveillance externe |
| Calcul fiscal erroné | Redressement | Paramètres par année, résultats indicatifs, validation professionnelle |
| Données transmises à une IA tierce | Atteinte à la confidentialité | Pseudonymisation ou modèle local |
| Périmètre trop large | Projet jamais terminé | Lots avec portes de sortie, MVP limité aux exigences M |

## 9. Critères d'acceptation et questions ouvertes

Le MVP est accepté quand les scénarios ci-dessous passent en test automatisé puis sur deux échéances réelles. L'étape suivante est le dossier technique : agrégateur, stack, hébergement et modèle de données.

**Scénarios d'acceptation**

| Scénario | Résultat attendu |
| --- | --- |
| Loyer complet reçu avant l'échéance depuis l'IBAN du locataire | Échéance « Payé », quittance proposée, aucune alerte |
| Rien reçu à la fin de la tolérance | Alerte avertissement dans l'heure suivant la synchro |
| Paiement inférieur au loyer attendu | Échéance « Partiel », alerte indiquant le reste dû |
| Loyer complet reçu depuis un IBAN inconnu | Rapprochement en attente de validation, alerte info |
| Loyer complet reçu depuis la caution solidaire | Échéance « Payé », alerte avertissement |
| Deux virements partiels dont la somme égale le loyer | Échéance « Payé » après le second |
| Virement du loyer rejeté après encaissement | Échéance recalculée (« Attendu » si la date limite n'est pas passée), alerte critique |
| Coupure électrique pendant une synchro | Reprise au redémarrage, aucune transaction en double |
| Même fichier CSV importé deux fois | Aucune transaction en double |
| Consentement bancaire qui expire dans 7 jours | Alerte de renouvellement |

**Questions ouvertes**

- [x] Le loyer arrive-t-il sur un compte personnel ou sur un compte commun d'indivision, et dans quelle banque ? → Un loyer sur un compte personnel, un loyer sur un compte joint, dans deux banques (lien avec le bien à préciser avant le lot 2).
- [ ] Quelles banques et quels types de comptes exactement (courant, livret, PEA, assurance-vie) ? → Deux banques ; types de comptes à préciser.
- [ ] Un crédit immobilier finance-t-il le bien ?
- [ ] Quel régime fiscal est envisagé pour la location meublée : micro-BIC ou réel ?
- [ ] Le co-indivisaire aura-t-il un accès, et à quel niveau de détail ?
- [ ] Quel budget annuel est acceptable (agrégateur, IA, stockage hors site) ?
- [ ] IA : modèle local ou API externe avec données pseudonymisées ?
- [x] Même machine qu'un éventuel bot de trading, ou machine dédiée ? → Machine dédiée (Pi 4, 4 Go) ; jamais d'hôte partagé avec un bot.
- [ ] Une aide au logement est-elle versée directement au bailleur ?

## 10. Vision produit et exigences de qualité (décision du 06/10/2026, Q24)

À terme, l'outil pourra être **proposé à d'autres foyers** (particuliers bailleurs, LMNP, indivisions).
Chaque client a **sa propre instance** (un foyer, ses données, ses secrets), installée chez lui
(serveur, PC, Raspberry Pi, cloud privé) ou hébergée (serveur en ligne). Ce qui suit s'applique dès
maintenant : rien de ce qui est construit ne doit rendre ce passage difficile.

### 10.1 Produit (PROD)

| ID | Exigence | Priorité |
| --- | --- | --- |
| PROD-01 | **Aucune donnée ni particularité d'un client dans le code** : banques, catégories, fuseau, échéances, profils de quota sont des données ou de la configuration, avec des valeurs par défaut raisonnables | M |
| PROD-02 | **Une instance par foyer** (isolation forte : base, secrets, sauvegardes, clés de chiffrement propres) ; plusieurs utilisateurs par instance avec des rôles (administrateur, co-indivisaire, expert-comptable en lecture) | M |
| PROD-03 | **Mise en service guidée** : un assistant de premier lancement (compte administrateur, second facteur, banques, notifications, sauvegarde, IA) ; aucun fichier à éditer à la main pour un usage standard | M |
| PROD-04 | **Intuitif avant tout** (principe de premier rang, 07/10/2026) : compréhension, utilisation, vocabulaire et fonctionnement général évidents sans documentation ni connaissance financière ou technique ; chaque écran explique ce qu'il attend, chaque nombre dit ce qu'il mesure, erreurs en français clair avec la marche à suivre, états vides utiles, vocabulaire unique (`docs/charte-ergonomie.md`) | M |
| PROD-05 | **Adaptable au client** : biens, baux, régimes fiscaux (micro-BIC, réel), indivision ou non, gestion directe ou par agence, banques ; modules activables | S |
| PROD-06 | **Langue et formats** : français d'abord ; textes isolés pour permettre une traduction future ; dates, montants et fuseau paramétrables | C |
| PROD-07 | **Réversibilité** : export complet dans des formats ouverts et import dans une nouvelle instance (changement d'hébergement sans perte) | M |

### 10.2 Déploiement et exploitation (DEP)

| ID | Exigence | Priorité |
| --- | --- | --- |
| DEP-01 | **Cibles** : toute machine Linux 64 bits (x86-64 et ARM64 : serveur, PC, Raspberry Pi, VM, cloud) avec Docker ; une seule procédure, les écarts étant de la configuration | M |
| DEP-02 | **Installation en une commande** avec vérifications préalables (architecture, disque, mémoire, horloge, ports) et compte rendu clair ; idempotente (relançable sans casse) | M |
| DEP-03 | **Configuration unique et validée** au démarrage (fichier ou variables), valeurs par défaut sûres, message explicite pour toute valeur manquante ou invalide ; secrets générés automatiquement à l'installation | M |
| DEP-04 | **Mises à jour sans intervention** : canaux stable et test, migrations automatiques et réversibles, retour arrière automatique si le contrôle de santé échoue, notes de version lisibles | M |
| DEP-05 | **Administration à distance** sûre : état de santé, versions, sauvegardes, journaux expurgés et paquet de diagnostic exportable ; accès de support uniquement avec l'accord du client, révocable, tracé | S |
| DEP-06 | **Accès distant** : VPN maillé (Tailscale ou équivalent) par défaut ; exposition Internet possible **uniquement** derrière un mode durci explicite (TLS, WebAuthn obligatoire, limitation de débit, en-têtes de sécurité, journalisation) | M |
| DEP-07 | **Base de données** : SQLite pour une instance individuelle ; la couche d'accès reste portable (pas de SQL propre à SQLite hors des modules prévus) pour permettre PostgreSQL en hébergement mutualisé | S |
| DEP-08 | **Observabilité** : journaux structurés et expurgés, conservés ; métriques de santé (synchros, sauvegardes, file d'alertes) ; supervision externe des signes de vie | M |

### 10.3 Résilience (RES, compléments)

| ID | Exigence | Priorité |
| --- | --- | --- |
| NF-RES-05 | Chien de garde : redémarrage automatique d'un service ou de la machine figés ; journal système conservé après redémarrage | M |
| NF-RES-06 | Toute dépendance externe (banque, IA, notifications, sauvegarde) peut tomber sans bloquer le reste ; dégradation explicite et visible | M |
| NF-RES-07 | Restauration complète testée automatiquement (sauvegarde → instance neuve) avant chaque version | M |
| NF-RES-08 | Objectifs : perte de données ≤ 15 min (RPO), remise en service ≤ 1 h sur matériel neuf (RTO), documentés et mesurés | M |

### 10.4 Qualité du code et maintenabilité (QUA)

| ID | Exigence | Priorité |
| --- | --- | --- |
| QUA-01 | Architecture en couches : domaine métier pur (sans base, sans réseau), services, adaptateurs (banques, IA, notifications, stockage), interfaces (web, API, MCP) ; dépendances dans un seul sens, vérifiées par des tests d'architecture | M |
| QUA-02 | Typage strict, lint, formatage, revue de chaque changement ; pas de code mort ; complexité bornée | M |
| QUA-03 | Documentation tenue à jour avec le code (exigences, décisions, exploitation, notes de version) ; chaque décision structurante consignée | M |
| QUA-04 | API v1 versionnée et compatible ; tout changement cassant passe par une v2 et une période de recouvrement | M |
| QUA-05 | Dépendances minimales, épinglées, licences compatibles avec un usage commercial (pas de licence contaminante dans le produit livré) | M |

### 10.5 Tests et intégration continue (QA)

| ID | Exigence | Priorité |
| --- | --- | --- |
| QA-01 | Tests unitaires, par propriétés, de contrat (serveur ↔ applications), de migration (montée et descente sur données), de bout en bout (navigateur, Android), de restauration ; couverture des branches ≥ 95 % | M |
| QA-02 | Logique métier écrite tests d'abord, à partir de scénarios ; chaque bug corrigé laisse un test qui l'aurait détecté | M |
| QA-03 | Analyse de sécurité automatique à chaque changement : secrets, vulnérabilités des dépendances, analyse statique, analyse des images, contrôle des licences | M |
| QA-04 | Chaîne de livraison sûre : branches protégées, CI verte obligatoire, images signées avec leur nomenclature (SBOM) et leur provenance, versions reproductibles | M |
| QA-05 | Tests périodiques renforcés : profil exhaustif des propriétés, tests de mutation sur la logique métier, test d'intrusion avant toute commercialisation | S |

### 10.6 Conformité (LEG)

Registre détaillé : `docs/conformite.md`.

| ID | Exigence | Priorité |
| --- | --- | --- |
| LEG-01 | **RGPD** : minimisation, durées de conservation, droits (accès, export, effacement), registre des traitements, chiffrement ; en offre commerciale, contrat de sous-traitance (art. 28) avec chaque client, notification de violation sous 72 h | M |
| LEG-02 | **DSP2 / agrégation** : accès aux comptes uniquement via un prestataire agréé (AISP) ; l'offre gratuite « restricted » ne couvre que ses propres comptes : contrat de production ou statut adapté avant toute commercialisation | M |
| LEG-03 | **Fiscalité** : calculs indicatifs, sources citées (CGI, BOFiP), mis à jour à chaque loi de finances ; mention claire que l'outil n'est pas un conseil fiscal ; la liasse reste du ressort du contribuable ou de son expert-comptable | M |
| LEG-04 | **Droit locatif** : quittance gratuite sur demande, révision IRL selon le bail, dépôt de garantie et délais de restitution, régularisation des charges ; aucun envoi au locataire sans validation | M |
| LEG-05 | **IA** : transparence (l'utilisateur sait quand il s'adresse à une IA), données minimisées et pseudonymisées, fournisseurs choisis en connaissance de cause (hébergement dans l'UE privilégié), actions de l'IA tracées | M |
| LEG-06 | **Commercialisation** : conditions générales, mentions légales, politique de confidentialité, licences des dépendances, assurance responsabilité civile professionnelle | S |
