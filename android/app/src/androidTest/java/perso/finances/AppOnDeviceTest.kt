package perso.finances

import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.concurrent.CopyOnWriteArrayList
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import perso.finances.api.ApiClient
import perso.finances.format.euros
import perso.finances.session.Session
import perso.finances.ui.FinancesApp
import perso.finances.ui.FinancesTheme

/**
 * L'application complète sur un vrai système Android, contre un faux serveur démarré sur le
 * téléphone même (réponses de référence de l'API) : chaque onglet s'affiche, les actions partent.
 */
@RunWith(AndroidJUnit4::class)
class AppOnDeviceTest {
    @get:Rule
    val compose = createComposeRule()
    private val server = MockWebServer()
    private val paths = CopyOnWriteArrayList<String>()

    @Before
    fun start() {
        val fixtures = Fixtures.dispatcher()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                paths += "${request.method} ${request.path}"
                return fixtures.dispatch(request)
            }
        }
        server.start()
        val session = Session(server.url("/").toString(), "jeton-factice-de-test")
        compose.setContent {
            FinancesTheme {
                FinancesApp(session, onUnauthorized = {}, onForget = {}, api = ApiClient(session.server, session.token))
            }
        }
    }

    @After
    fun stop() = server.shutdown()

    private fun waitFor(tag: String) = compose.waitUntil(10_000) {
        compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty()
    }

    private fun waitForRequest(line: String) = compose.waitUntil(10_000) { line in paths }

    @Test
    fun everyTabWorks() {
        waitFor("patrimoine")
        compose.onNodeWithTag("patrimoine").assertTextEquals(euros(1495310))
        compose.onNodeWithTag("contenu").performScrollToNode(hasText("Dépenses par catégorie"))

        compose.onNodeWithTag("onglet-rental").performClick()
        waitFor("confirmer-7")
        compose.onNodeWithTag("confirmer-7").performClick()
        waitForRequest("POST /api/v1/loyers/propositions/7/confirmer")

        compose.onNodeWithTag("onglet-accounts").performClick()
        waitFor("compte-2")
        compose.onNodeWithTag("contenu").performScrollToNode(hasTestTag("synchroniser-3"))
        compose.onNodeWithTag("synchroniser-3").assertIsNotEnabled()

        compose.onNodeWithTag("onglet-operations").performClick()
        waitFor("operation-11")
        compose.onNodeWithTag("operation-11").performClick()
        waitFor("categories")
        compose.onNodeWithText("Alimentation › Courses").performClick()
        waitForRequest("POST /api/v1/transactions/11/categorie")

        compose.onNodeWithTag("onglet-alerts").performClick()
        waitFor("resoudre-5")
        compose.onNodeWithTag("resoudre-5").performClick()
        waitForRequest("POST /api/v1/alertes/5/resoudre")

        compose.onNodeWithTag("onglet-assistant").performClick()
        compose.onNodeWithTag("question").performTextInput("Combien en restaurants ?")
        compose.onNodeWithTag("envoyer").performClick()
        waitForRequest("POST /api/v1/assistant/messages")
        waitFor("message-1")
    }
}
