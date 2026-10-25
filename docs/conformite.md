# Registre de conformité (créé le 06/10/2026)

Suivi des obligations légales et réglementaires, pour l'usage personnel d'aujourd'hui et pour une
éventuelle offre commerciale (CDC §10.6). Ce registre **n'est pas un avis juridique ni fiscal** :
il recense les points à respecter et ce que le logiciel fait pour y aider. Avant de
commercialiser, le faire relire par un juriste (RGPD, contrats) et un expert-comptable (fiscalité).

État : ✅ fait · 🟡 partiel · ⬜ à faire · — sans objet aujourd'hui.

## 1. Données personnelles (RGPD, loi Informatique et Libertés)

| Point | Usage personnel | Offre commerciale | Ce que fait l'outil / reste à faire |
| --- | --- | --- | --- |
| Base légale des traitements | — (usage domestique, hors RGPD pour mes propres données) | ⬜ | Le client est responsable de traitement de ses données ; je suis **sous-traitant** si j'héberge ou administre : contrat art. 28 à rédiger |
| Données du locataire (nom, IBAN, e-mail) | ✅ minimisées (NF-DATA-01) | ✅ | Chiffrées en base ; supprimables en fin de bail (⬜ bouton d'effacement à ajouter) ; base légale : exécution du bail |
| Noms des loyers et remboursements sur l'écran d'accueil (widget Android) | ✅ | ✅ | Affichés seulement si l'utilisateur pose le widget et choisit les éléments ; aucun montant ; noms modifiables (renommage) pour rester discrets ; rien n'est stocké sur le téléphone hors du choix des éléments |
| Bailleur(s) et adresse du logement (quittances) | ✅ | ✅ | Saisis sur la fiche du bien, chiffrés en base, jamais journalisés (seul le fait d'une modification l'est) ; conservés tant que le bien existe, modifiables et effaçables sur la fiche ; export avec l'instance (PROD-07, à livrer) |
| Minimisation et chiffrement | ✅ | ✅ | Colonnes sensibles chiffrées (AES-GCM), sauvegardes chiffrées côté client, secrets sops+age |
| Durées de conservation | 🟡 | ⬜ | Justificatifs fiscaux 6 ans (NF-DATA-02) ; durées par catégorie de données à paramétrer et purger automatiquement |
| Droits des personnes (accès, export, effacement) | 🟡 | ⬜ | Export CSV des opérations ; export complet et effacement complet d'une instance à livrer (PROD-07) |
| Registre des traitements | — | ⬜ | Modèle à fournir au client |
| Analyse d'impact (AIPD) | — | ⬜ | Données financières à grande échelle : à évaluer si offre hébergée mutualisée |
| Violation de données | 🟡 | ⬜ | Journal d'audit, alertes ; procédure de notification CNIL sous 72 h et information du client à écrire (`runbook-vol.md` à étendre) |
| Transferts hors UE | 🟡 | ⬜ | Hébergement UE (OVH, Backblaze région UE) ; fournisseurs d'IA américains (Anthropic, OpenAI) : clauses contractuelles types, données pseudonymisées ; **OVH (UE) proposé par défaut** |
| Sous-traitants ultérieurs | — | ⬜ | Liste à tenir : Enable Banking, Backblaze, fournisseur d'IA, Tailscale, Healthchecks, GitHub (code seulement) |

## 2. Accès aux comptes bancaires (DSP2)

| Point | État | Détail |
| --- | --- | --- |
| Prestataire agréé | ✅ | Enable Banking est un prestataire de services d'information sur les comptes (AISP) agréé ; l'outil n'accède jamais directement aux banques |
| Lecture seule | ✅ | Aucun droit d'initiation de paiement demandé ; liste blanche des requêtes (inv. 1), testée |
| Usage commercial | ⬜ | L'offre « restricted production » ne couvre que **ses propres comptes**. Pour des clients : contrat de production Enable Banking (chaque client consent pour ses comptes) ou partenariat avec un AISP ; ne jamais se présenter comme agrégateur sans statut |
| Fréquence d'accès sans le client | ✅ | 4 accès par compte et par 24 h au plus (RTS), budget compté par requête (inv. 10) |
| Consentement | ✅ | Donné par le client sur le site de sa banque, 90 à 180 jours, révocable depuis l'outil |

## 3. Fiscalité (location meublée, LMNP au réel)

| Point | État | Détail |
| --- | --- | --- |
| Statut de l'outil | 🟡 | Calculs **indicatifs** ; mention à afficher sur chaque récapitulatif fiscal ; sources citées (CGI, BOFiP) |
| Régimes | ⬜ | Micro-BIC (abattement) et réel (charges déductibles, amortissements par composant, déficit reportable) ; choix paramétrable par bien et par année |
| Amortissements | ⬜ | Bien hors terrain, composants, mobilier, travaux ; durées paramétrables ; plafonnement (pas de déficit créé par les amortissements) ; **réintégration des amortissements dans la plus-value en cas de vente** (loi de finances pour 2025) à signaler |
| Formulaires | ⬜ | Aide au remplissage 2031 et 2033 (A à G), report 2042-C-PRO ; la télédéclaration reste hors périmètre (expert-comptable, OGA ou contribuable) |
| CFE, taxe foncière | 🟡 | ✅ Échéances (15/12, 15/10) avec rappels, paiement rattaché au bien comme charge, part récupérable (ordures ménagères) saisie ; ⬜ déduction au réel dans le récapitulatif, mensualisation |
| Indivision | 🟡 | ✅ Récapitulatif annuel par bien réparti entre indivisaires (loyers encaissés, charges par nature, résultat), export CSV ; ⬜ à confirmer : déclaration par indivisaire, solde entre indivisaires (qui a payé quoi) |
| Conservation des justificatifs | 🟡 | 6 ans par défaut (NF-DATA-02) ; import des factures PDF à livrer |
| Mises à jour | ⬜ | Revue des règles à chaque loi de finances (janvier) : tâche récurrente de la feuille de route |

## 4. Droit locatif (meublé, loi du 6 juillet 1989)

| Point | État | Détail |
| --- | --- | --- |
| Quittance | ✅ | Établie gratuitement, seulement après paiement complet (un paiement partiel ne donne droit qu'à un reçu) ; distingue loyer et charges (art. 21) ; téléchargée par le bailleur et remise par lui : l'outil n'envoie rien (inv. 6) ; chaque établissement journalisé |
| Révision du loyer | 🟡 | ✅ Calcul selon l'IRL à la date anniversaire, appliqué après validation, sans rétroactivité (à partir du mois en cours en cas de retard) ; ⬜ lettre au locataire, import automatique de l'indice |
| Dépôt de garantie | ✅ | Plafond signalé (2 mois de loyer hors charges en meublé, 1 mois en location nue, art. 25-6 et 22) ; date limite de restitution (1 mois après la remise des clés si l'état des lieux de sortie est conforme, 2 mois sinon), rappel 7 jours avant et alerte de retard ; retenues avec motif obligatoire ; rien n'est envoyé au locataire |
| Charges | ⬜ | Provisions et régularisation annuelle, ou forfait (meublé) ; justificatifs tenus à disposition |
| Relances | ✅ | Aucun envoi au locataire sans validation ; journal des envois |

## 5. Intelligence artificielle

| Point | État | Détail |
| --- | --- | --- |
| Transparence | ✅ | L'assistant est présenté comme une IA ; ses modifications sont marquées « via l'IA » et annulables |
| Minimisation | ✅ | Pseudonymisation (noms en initiales, identifiants masqués), garde de sortie ; catégorisation automatique (ANA-02) : libellé pseudonymisé, montant et date seulement, désactivée par défaut |
| Supervision humaine | ✅ | Aperçu et confirmation des écritures de masse ; jamais d'action financière ni d'envoi |
| Règlement européen sur l'IA | 🟡 | Usage à risque limité (assistant conversationnel) : obligations de transparence ; à réévaluer si l'outil donne des recommandations financières |
| Choix du fournisseur | ✅ | Anthropic, OpenAI ou OVH au choix ; hébergement UE recommandé par défaut |

## 6. Logiciel et commercialisation

| Point | État | Détail |
| --- | --- | --- |
| Licences des dépendances | 🟡 | Contrôle automatique en CI à ajouter (QA-03) ; pas de licence contaminante dans le produit livré |
| Sécurité | 🟡 | Analyses automatiques en CI ; test d'intrusion avant commercialisation (QA-05) |
| Contrats | ⬜ | Conditions générales, contrat de sous-traitance, niveau de service, mentions légales, politique de confidentialité |
| Responsabilité | ⬜ | Assurance responsabilité civile professionnelle ; limitations de responsabilité (outil d'aide, pas de conseil) |
| Facturation électronique | — | Réforme B2B (2026-2027) : sans objet pour un particulier ; à surveiller pour l'import de factures (Factur-X) |
