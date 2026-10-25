# Machine à états d'une échéance de loyer

Complète la section 5 de `cahier-des-charges.md` (le schéma n'est pas conservé à l'export).

## États

| État | Signification |
| --- | --- |
| `ATTENDU` | Échéance générée depuis le bail, rien d'affecté |
| `PARTIEL` | Montant affecté confirmé > 0 et < montant dû, jour d'alerte (le 5) non atteint |
| `EN_RETARD` | Jour d'alerte (le 5) atteint, reste dû > 0 |
| `IMPAYE` | Toujours un reste dû au jour d'impayé (le 10) |
| `PAYE` | Montant affecté confirmé = montant dû (l'affectation est plafonnée ; état final sauf rejet) |

## Transitions

| De | Vers | Condition |
| --- | --- | --- |
| ATTENDU | PAYE | Virement(s) affecté(s) couvrant le montant dû |
| ATTENDU | PARTIEL | Virement affecté inférieur au montant dû |
| ATTENDU | EN_RETARD | Jour d'alerte (le 5) atteint sans paiement |
| PARTIEL | PAYE | Complément reçu |
| PARTIEL | EN_RETARD | Reste dû au jour d'alerte (le 5) |
| EN_RETARD | PAYE | Paiement reçu (incident de retard conservé dans l'historique) |
| EN_RETARD | IMPAYE | Jour d'impayé (le 10) atteint sans solde |
| IMPAYE | PAYE | Régularisation |
| PAYE | ATTENDU, EN_RETARD ou IMPAYE | Virement rejeté (écriture inverse) : l'allocation est annulée et l'état recalculé selon la date → alerte critique |

## Paramètres (configurables par bail)

- Jour d'alerte « rien reçu » : le **5** du mois de l'échéance (défaut) ; passage à `EN_RETARD`,
  avertissement, notification simple.
- Jour d'impayé : le **10** du mois de l'échéance (défaut, même référence que « Toujours rien » dans
  le CDC) ; passage à `IMPAYE`, alerte critique.
- Ces deux jours remplacent la « date limite + tolérance » et le « J+10 après la date limite » des
  versions précédentes (décision du 28/09/2026). Contrainte : jour d'alerte < jour d'impayé.
- Fenêtre de rapprochement : du 25 du mois précédent à la fin du mois concerné. Elle sert à capter les paiements
  **anticipés** ; un paiement tardif est affecté en priorité à l'échéance impayée la plus ancienne du bail, même hors fenêtre.
- Premier mois suivi : par défaut le premier mois **complet** d'historique bancaire du compte
  (historique depuis le 30/06 → juillet). Les mois antérieurs ne créent pas d'échéance : invérifiables,
  ils paraîtraient impayés et absorberaient les virements récents (règle ci-dessus). Un bail dont
  des échéances précèdent l'historique est signalé sur sa fiche, avec la correction en un clic.
- Montant : correspondance exacte, sinon PARTIEL ou trop-perçu. L'affectation est plafonnée au montant dû ;
  l'excédent est porté sur une ligne distincte (report à l'échéance suivante ou remboursement, sur décision).

## Principes d'implémentation

- L'état est **dérivé** des allocations **confirmées** (`rent_allocation.status = confirmed`) et de la date courante,
  recalculé à chaque passage : une fonction pure `compute_status(due, allocations, today, params)` facile à tester.
  Les allocations proposées (correspondance faible, émetteur inattendu) n'entrent pas dans le calcul.
- Les alertes sont émises sur les **changements d'état** et dédoublonnées par `dedupe_key`, qui porte un numéro
  d'épisode (ex. `rent:{due_id}:EN_RETARD:{episode}`) : après un rejet, un nouveau retard produit une nouvelle alerte.
- Un retard, un impayé, un rejet ou un paiement par la caution est tracé dans `rent_incident` (LOC-14),
  même une fois l'échéance payée.
- Si une transaction affectée est ensuite reconnue comme doublon d'une autre (API et fichier), ses allocations
  passent sur la transaction canonique. En cas d'allocations manuelles différentes, on garde celles de la canonique
  et on ouvre un conflit dans la file de validation. L'annulation du lien restaure exactement l'état antérieur.
