# Feuille de route — fonctionnalités proposées (rédigée le 06/10/2026)

Propositions à valider, classées par thème puis planifiées ci-dessous selon les **échéances
réelles** (fiscales, bancaires) et la valeur pour moi. Chaque ligne indique l'exigence du cahier
des charges quand elle existe (lots 3 et 4), sinon « nouveau ».

## 1. Documents et pièces justificatives

| Fonctionnalité | Détail | Exigence |
| --- | --- | --- |
| **Import de factures** | Dépôt d'un PDF ou d'une photo (web, et partage depuis l'appli Android) ; lecture automatique (OCR) du montant, de la date et du fournisseur ; rattachement proposé à l'opération bancaire correspondante ; stockage chiffré | LOC-09 (OCR), nouveau |
| **Taxe foncière** | Saisie ou import de l'avis (PDF) : montant, échéance (15 octobre, ou mensualisation), part des ordures ménagères récupérable sur le locataire ; rattachement automatique au bien et à l'opération ; rappel J-7 | LOC-10, FIS |
| **Relevés de gérance** (si le bien passe par une agence) | Import du relevé mensuel (PDF ou CSV) : loyer brut, honoraires, charges, travaux, virement net ; rapprochement du virement net de l'agence avec l'échéance (au lieu du loyer brut) et ventilation automatique des honoraires en charges du bien | nouveau |
| **Relevés bancaires PDF** | Import d'un relevé mensuel pour combler un trou d'historique (au-delà des 90 jours DSP2) ou un compte non relié | COL-05 |
| **Coffre de documents** | Bail, état des lieux, attestations d'assurance, avis d'imposition, quittances : stockés chiffrés, reliés à un bien, un bail, une opération ; recherche | nouveau |

## 2. Location meublée et fiscalité (lot 4)

| Fonctionnalité | Détail | Exigence |
| --- | --- | --- |
| **Calendrier des échéances** | Taxe foncière (15/10), CFE (15/12), déclaration de revenus (mai), acomptes, révision IRL, fin de bail, renouvellement du consentement bancaire, assurances PNO ; page dédiée, rappels ntfy J-7, export **ICS** pour l'agenda du téléphone | nouveau |
| **Révision IRL** | Indice INSEE automatique, nouveau loyer proposé à la date anniversaire, lettre au locataire après validation | LOC-07, LOC-08 |
| **Quittances PDF** | Générées après paiement complet, envoyées **seulement après validation** (inv. 6) | LOC-12, LOC-13 |
| **Régularisation des charges** | Provisions encaissées contre charges réelles de l'année ; décompte pour le locataire | nouveau |
| **Dépôt de garantie** | Suivi de l'encaissement, de la restitution et des retenues | nouveau |
| **LMNP** | Comparaison micro-BIC / réel ; au réel, plan d'amortissement (bien hors terrain, mobilier, travaux) ; aide au remplissage 2031/2033 et 2042-C-PRO ; récapitulatif pour l'expert-comptable | FIS-01, FIS-02, FIS-04 |
| **Indivision 50/50** | Quote-part de chaque indivisaire sur les loyers et les charges ; état annuel par indivisaire | LOC-11 |
| **Récapitulatif annuel** | Revenus, charges par nature, résultat ; exports Excel et PDF | FIS-05, FIS-06 |

## 3. Dépenses et budget (lot 3)

| Fonctionnalité | Détail | Exigence |
| --- | --- | --- |
| **Catégorisation automatique par IA** | Nouvelles opérations classées par le fournisseur choisi, sous un seuil de confiance → « À valider » ; règles proposées | ANA-02, ANA-03 |
| **Budgets** | Par catégorie et par mois, alertes à 80 % et 100 % | ANA-06 |
| **Abonnements** | Liste des récurrents, alerte de hausse de prix ou d'abonnement disparu ou en double ; rappel de résiliation | ANA-07 |
| **Anomalies** | Montant inhabituel, nouveau créancier, frais bancaires, doublon (outil d'analyse déjà disponible pour l'IA) → alertes | ANA-08 |
| **Étiquettes** | Projet, voyage, travaux, transversales aux catégories | ANA-05 |
| **Rapport mensuel** | Bilan automatique le 1er du mois (ntfy + page), rédigé par l'assistant | ALR-07, nouveau |

## 4. Patrimoine et trésorerie

| Fonctionnalité | Détail | Exigence |
| --- | --- | --- |
| **Prévision de trésorerie** | Page et alerte « solde bas prévu » (l'outil IA existe déjà) | ANA-10 |
| **Épargne et placements** | Livrets, PEA, assurance-vie, gestion pilotée : valeur saisie ou importée (relevé), évolution | COL-05 |
| **Crédits** | Tableau d'amortissement, capital restant dû dans le patrimoine net, intérêts déductibles (LMNP) | nouveau |
| **Valeur du bien** | Estimation saisie (ou indicateur DVF), patrimoine net complet | nouveau |
| **Prêt à un particulier** | Échéancier du remboursement, capital restant, historique (le suivi des échéances existe déjà) | nouveau |

## 5. Assistant IA

| Fonctionnalité | Détail |
| --- | --- |
| **Mémoire durable** | Faits que je lui confie (« le loyer arrive vers le 5 », « ce virement mensuel est un remboursement ») gardés et réutilisés, consultables et effaçables |
| **Réponses avec graphiques** | Courbes et barres dans la réponse (web et Android) |
| **Liens vers les opérations** | Les transactions citées deviennent cliquables vers leur fiche |
| **Questions programmées** | « Chaque lundi, résume la semaine » ; résultat en notification |
| **Lecture à voix haute** | Réponse lue par le téléphone après une dictée |
| **Recherche dans les documents** | Questions sur les factures et relevés importés |

## 6. Application Android

Notifications natives (sans l'appli ntfy), widget « solde et loyers du mois », partage d'une photo
de facture vers l'appli, mode hors ligne en lecture (cache chiffré), création de bail et import.

## 7. Fiabilité et exploitation (à faire en premier)

| Sujet | Pourquoi |
| --- | --- |
| **Relier à nouveau une banque** pour les cartes renouvelées, avec reprise de l'historique sur la nouvelle carte et ancienne carte ignorée | Cartes inaccessibles depuis le 30/09 (`ASPSP_ACCOUNT_NOT_ACCESSIBLE`) |
| **Journal système persistant** | Le week-end sans activité n'a pu être diagnostiqué faute de journaux conservés |
| **Chien de garde matériel du Pi** | Redémarrage automatique si le système se fige (comme avec la clé USB défaillante) |
| **Alerte « Pi muet » vérifiée** | Healthchecks doit prévenir par e-mail si le Pi s'arrête : à confirmer de mon côté |
| **Renouvellement du consentement bancaire** | Rappel et parcours guidé avant l'expiration (90 à 180 jours) |
| **Mise à jour de Gradle** et des dépendances signalées | Avertissements de la CI |

## Calendrier proposé

| Période | Contenu | Raison du placement |
| --- | --- | --- |
| **Oct. 2026** (maintenant) | Fiabilité (§7) et industrialisation P-3, P-4 ; **taxe foncière** (échéance du 15/10) ; **calendrier des échéances** avec rappels et export ICS | Échéance imminente ; base des rappels suivants |
| **Nov. 2026** | Lot 3 : catégorisation IA, budgets, abonnements, anomalies en alertes ; **import de factures** (web puis Android) | Dépenses lisibles avant la fin d'année |
| **Déc. 2026** | CFE (15/12) ; révision IRL et quittances ; dépôt de garantie ; relevés de gérance si une agence intervient | Échéances de décembre ; anniversaire du bail |
| **Janv.–mars 2027** | Récapitulatif 2026 ; **LMNP** (amortissements, 2031/2033) ; indivision ; régularisation des charges | Préparation de la déclaration |
| **Avr.–mai 2027** | Aide à la déclaration (2042-C-PRO) ; patrimoine (épargne, crédits, valeur du bien) | Déclaration de mai |
| **Ensuite** | Assistant (mémoire, graphiques, questions programmées) ; Android (notifications natives, widget, hors ligne) ; coffre de documents | Confort |

## Réponses (Q23) et conséquences

- **Gestion en direct** : les relevés de gérance restent au programme pour d'autres foyers, après le reste.
- **LMNP au réel** : amortissements, 2031/2033 et déficit reportable deviennent prioritaires (janvier à mars).
- **Taxe foncière annuelle**, **factures PDF** : traités en premier ; mensualisation et photos ensuite.
- Vision produit (Q24) : chaque fonctionnalité est construite paramétrable (régimes, gestion, banques),
  et la phase d'industrialisation (PLAN §3.8) avance en parallèle, à commencer par la fiabilité de
  l'hôte et la configuration unique.

Question encore ouverte : export ICS vers l'agenda, ou seulement des rappels ntfy ? → **Les deux, par défaut** (07/10/2026) : rappels ntfy, et fichier `.ics` à télécharger depuis la page Échéances. Un abonnement par adresse (mise à jour automatique dans l'agenda) demanderait un jeton dans l'URL : à décider.

## Reste à faire (point du 07/10/2026)

**Essais réels (rien à coder)** : relancer `bootstrap.sh` (journal persistant, chien
de garde) et faire l'essai Healthchecks « Pi muet » (P-3, T0.14) ; reconstruction sur support vierge
(T0.15) ; relire le runbook de vol (T0.16) ; saisir les baux réels et suivre deux échéances (T2.7) ;
essai de l'appli Android (A-4b), de l'extension MCP (B1.8) et de l'assistant intégré
avec une clé API (B2.3) ; relier à nouveau la banque dont les cartes ont été renouvelées.

**Au programme, pas encore commencé** :

| Thème | Fonctionnalité | Exigence |
| --- | --- | --- |
| Fiscalité | **LMNP au réel** : comparaison micro-BIC / réel, amortissements, aide 2031/2033 et 2042-C-PRO, déficit reportable | FIS-01, FIS-02, FIS-04 |
| Location | **Régularisation des charges** (provisions contre charges réelles, décompte) ; lettre de révision IRL ; import automatique de l'IRL | LOC-08, nouveau |
| Documents | **Import de factures** (PDF, photo, lecture automatique, rattachement à l'opération) ; coffre de documents ; relevés bancaires PDF | LOC-09, COL-05 |
| Dépenses | **Étiquettes** transversales (travaux, voyage…) ; rapport mensuel du 1er | ANA-05, ALR-07 |
| Patrimoine | Tableau d'amortissement des crédits (intérêts déductibles) ; échéancier d'un prêt à un particulier | nouveau |
| Assistant | Mémoire durable, graphiques et liens vers les opérations dans les réponses, questions programmées | nouveau |
| Android | Notifications natives, widget, partage d'une facture, mode hors ligne, quittance et dépôt dans l'appli ; **migration Gradle 9 / AGP 9** | nouveau |
| Produit | Configuration unique (P-4), installation générique (P-5), rôles (P-6), chaîne de livraison signée (P-7), support à distance (P-8), réversibilité RGPD (P-9) | PROD |

## Propositions (révisées le 07/10/2026)

### Optimisations retenues

| Optimisation | Gain |
| --- | --- |
| **Tableau de bord mis en cache** entre deux mises à jour bancaires | Ouverture instantanée au téléphone, moins de charge sur le Pi |
| **Entretien de la base planifié** (`ANALYZE` hebdomadaire, `VACUUM` mensuel la nuit) et taille suivie dans la page de santé | Pages qui restent rapides avec les années d'historique ; usure du support limitée |
| **Mode sombre automatique** (réglage du téléphone) | Confort le soir ; les couleurs sont déjà des variables CSS |
| **Contrôle d'accessibilité automatique** dans les tests de bout en bout | La charte d'ergonomie vérifiée à chaque modification |
| **Correctifs de dépendances fusionnés automatiquement** quand la CI est verte | Failles corrigées sans attendre |
| **Journal d'audit consultable** (qui a fait quoi, y compris l'IA) | Confiance dans l'IA : on voit et on annule ce qu'elle a fait |
| **Migration Android Gradle 9 / AGP 9** | Éviter de rester bloqué sur une chaîne de compilation obsolète |

### Fonctionnalités à fort impact (générales, pas seulement la location)

| # | Fonctionnalité | Ce que ça change | Faisabilité |
| --- | --- | --- | --- |
| 1 | **« Reste à dépenser »** : ce que je peux dépenser librement jusqu'à la prochaine paie, une fois déduits les prélèvements et factures à venir ; montant par jour, widget Android | La question que tout le monde se pose (« je peux me faire ce resto ? ») a une réponse en un coup d'œil | Bonne : la prévision et les opérations régulières existent déjà |
| 2 | **Chasseur d'économies** : un total « X €/an récupérables » et la liste des actions concrètes — abonnements en double ou plus utilisés, argent qui dort sur le compte courant (intérêts perdus par rapport à un livret), frais bancaires évitables, assurance emprunteur à renégocier (changement possible à tout moment, loi Lemoine), forfait mobile ou box plus cher que la moyenne | Rend de l'argent, chiffré ; on y revient chaque mois | Bonne pour les quatre premiers points (données déjà là) ; comparaison de prix de marché indicative |
| 3 | **Remboursements santé** : chaque dépense de santé reconnue (médecin, pharmacie…), remboursement Sécurité sociale et mutuelle attendu, alerte s'il n'arrive pas sous 30 jours | Les remboursements oubliés se comptent souvent en centaines d'euros par an | Bonne : rapprochement comme pour les loyers |
| 4 | **Dépenses partagées** (façon Tricount) : marquer une dépense « partagée avec X », solde de qui doit quoi, virement de remboursement reconnu automatiquement | Couple, colocation, vacances entre amis : fini le tableur | Bonne : même mécanique de rapprochement |
| 5 | **Impôt sur le revenu sans surprise** : estimation de l'impôt de l'année à partir des revenus réels, comparaison avec le prélèvement à la source (alerte « régularisation de X € en septembre »), liste des dépenses qui donnent droit à réduction ou crédit d'impôt (dons, emploi à domicile, garde d'enfant) prête pour la déclaration | Plus de mauvaise surprise ; des réductions d'impôt qu'on oublie de déclarer | Moyenne : barème public, calcul indicatif avec source citée |
| 6 | **Objectifs avec projection** : « Japon, 4 000 € en juin » → effort mensuel nécessaire, progression suivie automatiquement, alerte si l'objectif décroche | Motivant, et relié aux vrais comptes | Bonne |
| 7 | **Simulateur « Et si… ? »** : nouvelle voiture à crédit, baisse de salaire, déménagement, achat locatif → solde prévu sur 24 mois, avant/après | Décider en voyant les conséquences | Moyenne : s'appuie sur la prévision existante |
| 8 | **Opérations suspectes** : paiement à l'étranger, montant inhabituel, nouveau marchand en pleine nuit → notification « C'est bien vous ? » avec la marche à suivre (opposition) | Détecter une fraude plus tôt | Bonne, avec une limite honnête : au mieux à la mise à jour suivante (4 par jour, DSP2) |
| 9 | **Garanties et retours** : achat repéré + facture jointe → fin de garantie légale (2 ans), délai de retour, rappel avant expiration | Faire jouer une garantie au lieu de racheter | Moyenne : demande l'import de factures |
| 10 | **Papiers et échéances de la vie courante** : passeport, permis, contrôle technique, fin d'engagement box ou mobile, assurance auto à renégocier → rappels | Le calendrier des échéances étendu à toute la vie administrative | Bonne : calendrier existant |
| 11 | **Récit du mois par l'IA** : le 1er, une page qui raconte le mois (« +18 % en restaurants, surtout le week-end ; 2 abonnements inutilisés ; épargne 640 € »), graphiques à l'appui | Comprendre ses finances en deux minutes, sans tableaux | Bonne : l'assistant et les données existent |
| 12 | **Indépendance financière** : patrimoine net projeté, taux d'épargne, âge auquel les revenus du patrimoine couvriraient les dépenses | Vision long terme, très motivante | Bonne (indicatif) |

### Décision (07/10/2026)

- **Toutes les optimisations ci-dessus sont retenues** et réalisées en premier.
- **Fonctionnalités : une seule retenue**, le **widget Android des loyers et remboursements** : une
  case ☐ / ☑ par élément pour le mois en cours (payé ou non), choix des éléments affichés dans le
  widget, et renommage des loyers et remboursements depuis l'application.
- Les autres fonctionnalités de cette liste restent des idées, **non programmées**.

## Avancement

| Date | Fonctionnalité | État |
| --- | --- | --- |
| 07/10/2026 | Fiabilité : chien de garde du disque, journal persistant (P-3) | ✅ code ; à déployer par `bootstrap.sh` |
| 07/10/2026 | Calendrier des échéances, taxe foncière, export ICS (F-1) | ✅ |
| 07/10/2026 | Budgets par catégorie, alertes 80 % et 100 % (F-2) | ✅ |
| 07/10/2026 | Abonnements (hausse de prix, prélèvement manquant) et anomalies en alertes (F-3) | ✅ |
| 07/10/2026 | Catégorisation automatique par IA, suggestions à valider, règle proposée (F-4) | ✅ (à activer sur la page Sécurité) |
| 07/10/2026 | Heures silencieuses, alertes regroupées, bilan hebdomadaire (F-5) | ✅ |
| 07/10/2026 | Prévision de trésorerie par compte et alerte « solde bas prévu » (F-6) | ✅ |
| 07/10/2026 | Récapitulatif annuel par bien, réparti entre indivisaires, export CSV (F-7) | ✅ |
| 07/10/2026 | Révision IRL : proposition à l'anniversaire, application après validation (F-8) | ✅ (indices saisis à la main) |
| 07/10/2026 | Patrimoine : épargne, placements, bien, crédits saisis, patrimoine net (F-9) | ✅ |
| 07/10/2026 | Principe « intuitif avant tout » : charte, navigation regroupée, vocabulaire unifié (Q25) | ✅ première passe ; à appliquer à chaque changement |
| 07/10/2026 | Notifications sur le téléphone vérifiables (bouton d'essai, procédure) ; audit des dépendances | ✅ |
| 07/10/2026 | Quittances de loyer PDF après paiement complet, remises par le bailleur (LOC-07) | ✅ |
| 07/10/2026 | Dépôt de garantie : encaissement, retenues motivées, restitution, rappel du délai légal (LEG-04) | ✅ |
| 07/10/2026 | Optimisations : cache du tableau de bord, entretien de la base, état du système et journal, mode sombre, accessibilité, correctifs de sécurité automatiques | ✅ |
| 07/10/2026 | Android : Gradle 9 / AGP 9 ; widget « Loyers du mois » (☑ / ☐ par élément choisi) et renommage depuis l'appli | ✅ |
