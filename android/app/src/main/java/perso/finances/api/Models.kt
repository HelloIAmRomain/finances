package perso.finances.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// Modèles de l'API JSON v1 (app/web/api.py). Montants en centimes entiers, dates ISO.

@Serializable
data class PairingRequest(val code: String, val nom: String)

@Serializable
data class PairingResponse(val jeton: String, val appareil: Int)

@Serializable
data class Me(val appareil: Int, val version: Int)

@Serializable
data class Message(val message: String)

@Serializable
data class MonthFlow(val mois: String, val recettes: Long, val depenses: Long)

@Serializable
data class RentOfMonth(val bail: String, val etat: String, @SerialName("reste_du") val resteDu: Long)

@Serializable
data class Dashboard(
    val mois: String,
    val patrimoine: Long,
    @SerialName("comptes_sans_solde") val comptesSansSolde: List<String>,
    val recettes: Long,
    val depenses: Long,
    val solde: Long,
    @SerialName("taux_epargne_pour_mille") val tauxEpargnePourMille: Int?,
    @SerialName("recettes_n_1") val recettesN1: Long,
    @SerialName("depenses_n_1") val depensesN1: Long,
    val categories: List<List<kotlinx.serialization.json.JsonElement>>,
    @SerialName("flux_12_mois") val flux12Mois: List<MonthFlow>,
    val loyers: List<RentOfMonth>,
) {
    /** (catégorie, montant) : l'API renvoie des paires [nom, montant]. */
    val categoryTotals: List<Pair<String, Long>>
        get() = categories.map { pair ->
            val name = (pair[0] as kotlinx.serialization.json.JsonPrimitive).content
            val amount = (pair[1] as kotlinx.serialization.json.JsonPrimitive).content.toLong()
            name to amount
        }
}

@Serializable
data class Account(
    val id: Int,
    val nom: String,
    val type: String,
    val banque: String? = null,
    val connexion: String? = null,
    @SerialName("consentement_jusqu_au") val consentementJusquAu: String? = null,
    @SerialName("derniere_synchro") val derniereSynchro: String? = null,
    @SerialName("derniere_erreur") val derniereErreur: String? = null,
    val solde: Long? = null,
    @SerialName("solde_type") val soldeType: String? = null,
    @SerialName("solde_date") val soldeDate: String? = null,
    val compte: Boolean,
    @SerialName("synchros_par_jour") val synchrosParJour: Int,
    @SerialName("synchro_manuelle_impossible") val synchroManuelleImpossible: String? = null,
)

@Serializable
data class Transaction(
    val id: Int,
    val date: String,
    val compte: String,
    val libelle: String,
    val montant: Long,
    val categories: List<String>,
    @SerialName("virement_interne") val virementInterne: Boolean,
)

@Serializable
data class TransactionPage(val total: Int, val page: Int, val transactions: List<Transaction>)

@Serializable
data class Category(val id: Int, val libelle: String)

@Serializable
data class CategorizeRequest(val categorie: Int)

@Serializable
data class Due(
    val id: Int,
    val mois: String,
    val montant: Long,
    val recu: Long,
    @SerialName("reste_du") val resteDu: Long,
    val etat: String,
)

@Serializable
data class Lease(
    val id: Int,
    val type: String,
    val nom: String,
    val bien: String? = null,
    val compte: String,
    val mensuel: Long,
    @SerialName("jour_alerte") val jourAlerte: Int,
    @SerialName("jour_impaye") val jourImpaye: Int,
    val echeances: List<Due>,
)

@Serializable
data class Pending(
    val id: Int,
    val bail: String,
    val date: String,
    val libelle: String,
    val montant: Long,
    val mois: String? = null,
)

@Serializable
data class Rental(
    val baux: List<Lease>,
    @SerialName("a_valider") val aValider: List<Pending>,
    @SerialName("trop_percus") val tropPercus: List<Pending>,
)

@Serializable
data class Alert(
    val id: Int,
    val type: String,
    val gravite: String,
    val etat: String,
    val titre: String,
    @SerialName("ouverte_le") val ouverteLe: String,
)

@Serializable
data class ErrorBody(val detail: String? = null)

@Serializable
data class AssistantRequest(val texte: String, val conversation: Int? = null)

@Serializable
data class AssistantReply(
    val conversation: Int,
    val titre: String,
    val reponse: String,
    val outils: List<String>,
    val modifications: List<String>,
)

@Serializable
data class ConversationSummary(
    val id: Int,
    val titre: String,
    @SerialName("mise_a_jour") val miseAJour: String,
    val epinglee: Boolean = false,
)

@Serializable
data class ExchangeDto(val role: String, val texte: String, val outils: List<String>)

@Serializable
data class ConversationDetail(
    val id: Int,
    val titre: String,
    val messages: List<ExchangeDto>,
    val epinglee: Boolean = false,
)

@Serializable
data class RenameRequest(val titre: String)

/** Nouveau nom d'un loyer ou d'un remboursement. */
@Serializable
data class LeaseRename(val nom: String)

@Serializable
data class PinRequest(val epinglee: Boolean)

@Serializable
data class TurnStart(val tour: String, val conversation: Int, val titre: String)

@Serializable
data class TurnEvent(val n: Int, val type: String, val texte: String)

@Serializable
data class Turn(
    val tour: String,
    val conversation: Int,
    val etat: String,
    val evenements: List<TurnEvent>,
    @SerialName("texte_partiel") val textePartiel: String,
    val reponse: String? = null,
    val outils: List<String>,
    val modifications: List<String>,
    val erreur: String? = null,
)
