# Charte d'ergonomie et de rédaction (principe de premier rang, 07/10/2026)

L'outil doit se comprendre **sans documentation ni connaissance financière ou technique** : à
chaque écran, on sait où l'on est, ce que l'on voit, ce que l'on peut faire et ce qui va se
passer. Cette charte s'applique à tout ce qui est visible : pages web, application Android,
notifications, messages d'erreur, exports, réponses de l'assistant. Elle prime sur l'élégance du
code : une fonctionnalité juste mais incompréhensible n'est pas terminée.

## 1. Comprendre

- **Chaque page commence par une phrase** qui dit à quoi elle sert et ce qu'elle attend de moi,
  en langage courant (« Ce que vous dépensez chaque mois par catégorie, et ce qui reste »).
- **Un nombre n'est jamais seul** : il dit ce qu'il mesure, sur quelle période et à quelle date
  (« Solde au 06/10/2026 », « dans 30 jours », « sur 3 mois »).
- **Un état est dit avec des mots**, et sa couleur ne fait que le confirmer (« en retard »,
  « payé » ; jamais une couleur ou une icône seule).
- **Pas de jargon** : ni terme technique (« API », « synchro DSP2 », « JSON », « dédoublonnage »,
  « jeton »), ni sigle sans explication. Un sigle métier nécessaire (IRL, CFE, PNO, TEOM) est
  développé à sa première apparition sur la page ou expliqué en une ligne.
- **Un estimé se présente comme tel** (« estimation », « indicatif »), avec sa limite (« les
  dépenses ponctuelles ne sont pas prévues »).

## 2. Rédiger

- Français courant, phrases courtes, **vouvoiement** dans l'interface ; ton neutre et rassurant.
- **Même mot pour la même chose partout** (glossaire ci-dessous) ; jamais deux mots pour une même
  notion, jamais un mot pour deux notions.
- **Boutons : un verbe d'action précis** (« Enregistrer le budget », « Marquer comme réglée »),
  jamais « OK », « Valider » seul ou « Soumettre ».
- **Messages de confirmation** : ce qui a été fait, et la suite s'il y en a une (« Loyer révisé à
  partir du 01/11/2026 ; prévenez le locataire du nouveau montant »).
- **Messages d'erreur** : ce qui ne va pas, puis comment corriger, avec un exemple (« Montant
  illisible : écrivez par exemple 1 234,56 »). Jamais de code, de trace ni de terme interne.
- Majuscule initiale seulement : le reste du message garde sa casse (sigles, noms propres).
- Dates JJ/MM/AAAA, montants « 1 234,56 € », pourcentages entiers « 80 % » (UI-05).

## 3. Utiliser

- **États vides utiles** : dire pourquoi c'est vide et quelle est la première action (« Aucun
  budget. Choisissez une catégorie ci-dessous ; la moyenne de vos trois derniers mois vous
  aide à fixer le montant. »).
- **Valeurs par défaut sûres et pré-remplies** ; suggestions plutôt que champs vides (moyenne,
  date du jour, échéances habituelles).
- **Saisie tolérante** : montants avec ou sans espaces, virgule ou point, avec ou sans « € » ;
  dates par sélecteur ; jamais de format imposé que l'utilisateur doit deviner.
- **Rien d'irréversible sans le dire** ; tout ce qui peut s'annuler se dit annulable. Aucun envoi
  au locataire sans validation (inv. 6).
- **Une action, un endroit** : on agit là où l'on voit le problème (lien direct depuis une
  alerte vers l'objet concerné, depuis le tableau de bord vers le détail).
- **Navigation regroupée par besoin** (suivre mon argent, mes locations, réglages) plutôt
  qu'une longue liste plate.
- Utilisable au téléphone (360 px), au clavier et au lecteur d'écran (étiquettes, contrastes).

## 4. Fonctionnement général

- **L'outil travaille seul et le dit** : rapprochements, rappels, catégorisations automatiques
  sont visibles et expliqués (« reconnu automatiquement », « proposé par l'IA, à confirmer »).
- **Une notification se comprend sans ouvrir l'application** : titre explicite (sans montant ni
  nom, DT §6), lien vers le détail.
- **L'utilisateur garde la main** : toute décision automatique se corrige en un clic et la
  correction est retenue (elle n'est pas refaite au passage suivant).

## 5. Glossaire (termes à employer)

| Notion | Terme affiché | Éviter |
| --- | --- | --- |
| Transaction bancaire | opération | transaction, écriture, ligne |
| Compte additionné dans les totaux | compte suivi | compte compté |
| Compte visible mais non additionné | consultation seule | — |
| Récupération des opérations auprès de la banque | mise à jour (du compte) | synchro, synchronisation |
| Consentement DSP2 | accès à la banque (à renouveler) | consentement, session |
| Échéance de loyer | échéance | due, période |
| Date limite administrative ou fiscale | échéance (page Échéances) | deadline |
| Opération régulière | opération régulière, abonnement | récurrent, série |
| Indice de référence des loyers | IRL (indice de référence des loyers) | indice seul |

## 6. Contrôle avant de terminer une tâche

1. La page s'ouvre-t-elle sur une phrase qui dit à quoi elle sert ?
2. Chaque nombre a-t-il son libellé, sa période ou sa date ?
3. Chaque bouton est-il un verbe précis ? Chaque message dit-il quoi faire ensuite ?
4. L'état vide guide-t-il vers la première action ?
5. Un mot technique, un sigle non expliqué ou un terme hors glossaire subsiste-t-il ?
6. Le parcours est-il faisable au téléphone (test de bout en bout à 360 px) ?
