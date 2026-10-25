package perso.finances

import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.RecordedRequest

/** Réponses de référence de l'API (validées côté serveur par tests/web/test_api_contract.py). */
object Fixtures {
    fun text(name: String): String =
        requireNotNull(Fixtures::class.java.getResourceAsStream("/api/$name.json")) { name }
            .bufferedReader().use { it.readText() }

    fun json(name: String, code: Int = 200): MockResponse =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(text(name))

    /** Serveur factice : chaque chemin de l'API renvoie sa réponse de référence. */
    fun dispatcher(token: String = "jeton-factice-de-test"): Dispatcher = object : Dispatcher() {
        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.requestUrl?.encodedPath.orEmpty().removePrefix("/api/v1/")
            if (path == "appairage") return json("appairage")
            if (request.getHeader("Authorization") != "Bearer $token") {
                return MockResponse().setResponseCode(401).setBody("""{"detail":"Appareil non appairé ou révoqué."}""")
            }
            return when {
                path == "tableau-de-bord" -> json("tableau-de-bord")
                path == "comptes" -> json("comptes")
                path == "transactions" -> json("transactions")
                path == "categories" -> json("categories")
                path == "loyers" -> json("loyers")
                path == "alertes" -> json("alertes")
                path == "assistant" -> json("assistant")
                path == "assistant/messages" -> json("assistant-envoi", code = 202)
                path.startsWith("assistant/tours/") -> json("assistant-tour")
                path == "assistant/conversations" -> json("conversations")
                path.startsWith("assistant/conversations/") && request.method == "DELETE" ->
                    MockResponse().setBody("""{"message":"Conversation supprimée"}""")
                path.startsWith("assistant/conversations/") -> json("conversation")
                request.method == "POST" -> MockResponse().setBody("""{"message":"Action faite"}""")
                else -> MockResponse().setResponseCode(404).setBody("""{"detail":"inconnu"}""")
            }
        }
    }
}
