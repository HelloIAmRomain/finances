package perso.finances.api

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody

/** Erreurs de l'API, avec un message lisible pour l'interface. */
sealed class ApiException(message: String) : Exception(message) {
    /** Jeton refusé (appareil révoqué) : il faut appairer à nouveau. */
    class Unauthorized(message: String) : ApiException(message)

    /** Serveur injoignable : le plus souvent, Tailscale n'est pas actif sur le téléphone. */
    class Unreachable : ApiException("Serveur injoignable : Tailscale est-il actif sur ce téléphone ?")

    /** Refus métier (synchro trop rapprochée, décision déjà prise…) ou erreur du serveur. */
    class Refused(val status: Int, message: String) : ApiException(message)
}

/**
 * Client de l'API v1. HTTPS seulement (network_security_config), jeton d'appareil en en-tête,
 * aucun cookie. Toutes les méthodes sont à appeler depuis une coroutine.
 */
class ApiClient(
    baseUrl: String,
    private val token: String?,
    private val http: OkHttpClient = defaultHttp,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) {
    private val base: HttpUrl = baseUrl.trimEnd('/').toHttpUrl()

    suspend fun pair(code: String, deviceName: String): PairingResponse =
        post("appairage", PairingRequest(code, deviceName), PairingRequest.serializer(), PairingResponse.serializer())

    suspend fun me(): Me = get("moi", Me.serializer())

    suspend fun dashboard(month: String? = null): Dashboard =
        get("tableau-de-bord", Dashboard.serializer(), listOfNotNull(month?.let { "mois" to it }))

    suspend fun accounts(): List<Account> = get("comptes", ListSerializer(Account.serializer()))

    suspend fun sync(accountId: Int): Message = post("comptes/$accountId/synchroniser", null, null, Message.serializer())

    suspend fun transactions(text: String?, page: Int): TransactionPage =
        get(
            "transactions",
            TransactionPage.serializer(),
            listOfNotNull(text?.takeIf { it.isNotBlank() }?.let { "texte" to it }, "page" to page.toString()),
        )

    suspend fun categories(): List<Category> = get("categories", ListSerializer(Category.serializer()))

    suspend fun categorize(transactionId: Int, categoryId: Int): Message =
        post(
            "transactions/$transactionId/categorie",
            CategorizeRequest(categoryId),
            CategorizeRequest.serializer(),
            Message.serializer(),
        )

    suspend fun rental(): Rental = get("loyers", Rental.serializer())

    suspend fun renameLease(leaseId: Int, name: String): Message =
        post("loyers/$leaseId/renommer", LeaseRename(name), LeaseRename.serializer(), Message.serializer())

    suspend fun decideProposal(allocationId: Int, confirm: Boolean): Message =
        post(
            "loyers/propositions/$allocationId/${if (confirm) "confirmer" else "refuser"}",
            null,
            null,
            Message.serializer(),
        )

    suspend fun alerts(all: Boolean = false): List<Alert> =
        get("alertes", ListSerializer(Alert.serializer()), if (all) listOf("toutes" to "true") else emptyList())

    suspend fun alertAction(alertId: Int, resolve: Boolean): Message =
        post("alertes/$alertId/${if (resolve) "resoudre" else "vue"}", null, null, Message.serializer())

    /** Question à l'assistant : réponse immédiate (le tour se déroule ensuite sur le serveur). */
    suspend fun sendMessage(text: String, conversation: Int?): TurnStart =
        post("assistant/messages", AssistantRequest(text, conversation), AssistantRequest.serializer(), TurnStart.serializer())

    /** Avancement d'un tour : nouvelles étapes (après `since`), texte partiel, réponse finale. */
    suspend fun turn(id: String, since: Int): Turn =
        get("assistant/tours/$id", Turn.serializer(), listOf("depuis" to since.toString()))

    suspend fun conversations(): List<ConversationSummary> =
        get("assistant/conversations", ListSerializer(ConversationSummary.serializer()))

    suspend fun conversation(id: Int): ConversationDetail =
        get("assistant/conversations/$id", ConversationDetail.serializer())

    suspend fun renameConversation(id: Int, title: String): ConversationDetail =
        post(
            "assistant/conversations/$id/renommer",
            RenameRequest(title),
            RenameRequest.serializer(),
            ConversationDetail.serializer(),
        )

    suspend fun pinConversation(id: Int, pinned: Boolean): ConversationDetail =
        post(
            "assistant/conversations/$id/epingler",
            PinRequest(pinned),
            PinRequest.serializer(),
            ConversationDetail.serializer(),
        )

    suspend fun deleteConversation(id: Int): Message =
        execute(Request.Builder().url(url("assistant/conversations/$id", emptyList())).delete(), Message.serializer())

    private fun url(path: String, query: List<Pair<String, String>>): HttpUrl =
        base.newBuilder().addPathSegments("api/v1/$path").apply {
            query.forEach { (key, value) -> addQueryParameter(key, value) }
        }.build()

    private suspend fun <T> get(path: String, serializer: KSerializer<T>, query: List<Pair<String, String>> = emptyList()): T =
        execute(Request.Builder().url(url(path, query)).get(), serializer)

    private suspend fun <B, T> post(
        path: String,
        body: B?,
        bodySerializer: KSerializer<B>?,
        serializer: KSerializer<T>,
        client: OkHttpClient = http,
    ): T {
        val payload: RequestBody = if (body != null && bodySerializer != null) {
            json.encodeToString(bodySerializer, body).toRequestBody(jsonType)
        } else {
            ByteArray(0).toRequestBody(jsonType)
        }
        return execute(Request.Builder().url(url(path, emptyList())).post(payload), serializer, client)
    }

    private suspend fun <T> execute(
        builder: Request.Builder,
        serializer: KSerializer<T>,
        client: OkHttpClient = http,
    ): T = withContext(io) {
        try {
            token?.let { builder.header("Authorization", "Bearer $it") }
        } catch (_: IllegalArgumentException) {
            throw ApiException.Unauthorized("Jeton d'appareil illisible : appairer à nouveau.")
        }
        builder.header("Accept", "application/json")
        val response = try {
            client.newCall(builder.build()).execute()
        } catch (_: IOException) {
            throw ApiException.Unreachable()
        }
        response.use {
            // La connexion peut aussi tomber pendant la lecture de la réponse (réseau mobile).
            val text = try {
                it.body?.string().orEmpty()
            } catch (_: IOException) {
                throw ApiException.Unreachable()
            }
            when {
                it.code == 401 -> throw ApiException.Unauthorized(detail(text) ?: "Appareil non appairé ou révoqué.")
                !it.isSuccessful -> throw ApiException.Refused(it.code, detail(text) ?: "Erreur du serveur (${it.code}).")
                else -> try {
                    json.decodeFromString(serializer, text)
                } catch (_: SerializationException) {
                    throw ApiException.Refused(it.code, "Réponse inattendue du serveur : mettre à jour l'application ?")
                } catch (_: IllegalArgumentException) {
                    throw ApiException.Refused(it.code, "Réponse inattendue du serveur : mettre à jour l'application ?")
                }
            }
        }
    }

    private fun detail(text: String): String? =
        runCatching { json.decodeFromString(ErrorBody.serializer(), text).detail }.getOrNull()

    companion object {
        val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
        private val jsonType = "application/json; charset=utf-8".toMediaType()
        private val defaultHttp: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(90, TimeUnit.SECONDS) // une synchro manuelle peut prendre du temps
            .build()
    }
}
