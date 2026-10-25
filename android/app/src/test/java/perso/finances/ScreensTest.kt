package perso.finances

import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.click
import androidx.compose.ui.test.percentOffset
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextReplacement
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.concurrent.TimeUnit
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import perso.finances.api.ApiClient
import perso.finances.format.euros
import perso.finances.session.Session
import perso.finances.ui.FinancesApp
import perso.finances.ui.FinancesTheme
import perso.finances.ui.PairingScreen

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class ScreensTest {
    @get:Rule
    val compose = createComposeRule()
    private val server = MockWebServer()

    @Before
    fun start() {
        server.dispatcher = Fixtures.dispatcher()
        server.start()
    }

    @After
    fun stop() = server.shutdown()

    private fun app(token: String = "jeton-factice-de-test", onUnauthorized: () -> Unit = {}) {
        val session = Session(server.url("/").toString(), token)
        compose.setContent {
            FinancesTheme(dark = false) {
                FinancesApp(session, onUnauthorized = onUnauthorized, onForget = {}, api = ApiClient(session.server, token))
            }
        }
    }

    private fun waitFor(tag: String) = compose.waitUntil(5_000) {
        compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty()
    }

    @Test
    fun `dashboard shows net worth and the month rents`() {
        app()
        waitFor("patrimoine")
        compose.onNodeWithTag("patrimoine").assertTextEquals(euros(1495310))
        compose.onNodeWithTag("mois").assertTextEquals("Septembre 2026")
        compose.onNodeWithTag("contenu").performScrollToNode(hasText("Studio démo"))
        compose.onNodeWithText("Studio démo").assertExists()
    }

    @Test
    fun `a pending rent can be confirmed from the phone`() {
        app()
        compose.onNodeWithTag("onglet-rental").performClick()
        waitFor("confirmer-7")
        compose.onNodeWithTag("confirmer-7").performClick()
        compose.waitUntil(5_000) {
            generateSequence { server.takeRequest(10, TimeUnit.MILLISECONDS) }.any { it.path == "/api/v1/loyers/propositions/7/confirmer" }
        }
    }

    @Test
    fun `a lease can be renamed from the phone`() {
        app()
        compose.onNodeWithTag("onglet-rental").performClick()
        waitFor("renommer-bail-1")
        compose.onNodeWithTag("contenu").performScrollToNode(hasTestTag("renommer-bail-1"))
        compose.onNodeWithTag("renommer-bail-1").performClick()
        compose.onNodeWithTag("nouveau-nom").performTextReplacement("Studio Lyon")
        compose.onNodeWithTag("confirmer-nom").performClick()
        compose.waitUntil(5_000) {
            generateSequence { server.takeRequest(10, TimeUnit.MILLISECONDS) }.any {
                it.path == "/api/v1/loyers/1/renommer" && it.body.readUtf8().contains("Studio Lyon")
            }
        }
    }

    @Test
    fun `manual sync button is disabled with its reason`() {
        app()
        compose.onNodeWithTag("onglet-accounts").performClick()
        waitFor("compte-2")
        compose.onNodeWithTag("contenu").performScrollToNode(hasTestTag("synchroniser-3"))
        compose.onNodeWithTag("synchroniser-3").assertIsNotEnabled()
        compose.onNodeWithText("Une synchro manuelle par quart d'heure", substring = true).assertExists()
    }

    @Test
    fun `alerts can be resolved`() {
        app()
        compose.onNodeWithTag("onglet-alerts").performClick()
        waitFor("resoudre-5")
        compose.onNodeWithTag("resoudre-5").performClick()
        compose.waitUntil(5_000) {
            generateSequence { server.takeRequest(10, TimeUnit.MILLISECONDS) }.any { it.path == "/api/v1/alertes/5/resoudre" }
        }
    }

    @Test
    fun `operations are listed and searchable`() {
        app()
        compose.onNodeWithTag("onglet-operations").performClick()
        waitFor("operation-11")
        compose.onNodeWithText("SUPERMARCHE DEMO").assertExists()
        compose.onNodeWithTag("recherche").performTextInput("salaire")
        compose.waitUntil(5_000) {
            generateSequence { server.takeRequest(10, TimeUnit.MILLISECONDS) }.any { it.path?.contains("texte=salaire") == true }
        }
    }

    @Test
    fun `a revoked device goes back to pairing`() {
        var unauthorized = false
        app(token = "revoque") { unauthorized = true }
        compose.waitUntil(10_000) { unauthorized }
        assertTrue(unauthorized)
    }

    private fun pairing(onPaired: (Session) -> Unit) = compose.setContent {
        FinancesTheme(dark = false) {
            PairingScreen(onPaired = onPaired, client = { ApiClient(server.url("/").toString(), token = null) })
        }
    }

    @Test
    fun `pairing refuses a plain http address`() {
        pairing { error("ne doit pas être appairé") }
        compose.onNodeWithTag("serveur").performTextInput("http://pi.exemple.ts.net")
        compose.onNodeWithTag("code").performTextInput("ABCD-EFGH")
        compose.onNodeWithTag("appairer").performClick()
        compose.onNodeWithTag("erreur").assertExists()
    }

    @Test
    fun `pairing with a valid code stores the https session`() {
        var paired: Session? = null
        pairing { paired = it }
        compose.onNodeWithTag("serveur").performTextInput("pi.exemple.ts.net")
        compose.onNodeWithTag("code").performTextInput("abcd-efgh")
        compose.onNodeWithTag("appairer").performClick()
        compose.waitUntil(5_000) { paired != null }
        assertEquals(Session("https://pi.exemple.ts.net", "jeton-factice-de-test"), paired)
    }

    @Test
    fun assistantAnswersTypedQuestion() {
        app()
        compose.onNodeWithTag("onglet-assistant").performClick()
        compose.onNodeWithTag("envoyer").assertIsNotEnabled()
        compose.onNodeWithTag("question").performTextInput("Combien en restaurants ?")
        compose.onNodeWithTag("envoyer").performClick()
        waitFor("message-1")
        compose.onNodeWithTag("titre-conversation").assertTextEquals("Combien en restaurants ?")
        compose.waitUntil(5_000) {  // la réponse arrive après le message, par interrogation du serveur
            compose.onAllNodesWithText("Septembre", useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
        }  // titre Markdown
        compose.onNodeWithText("Total : 153,69 €", useUnmergedTree = true).assertExists()
        compose.onNodeWithText("1 consultation", useUnmergedTree = true).assertExists()
        compose.onNodeWithText("• Consulte : Dépenses (ou revenus) par catégorie", useUnmergedTree = true).assertExists()  // étape
        compose.onNodeWithTag("nouvelle-conversation").performClick()
        compose.onNodeWithText("Combien en restaurants ?").assertDoesNotExist()
        val request = generateSequence { server.takeRequest(1, TimeUnit.SECONDS) }.first { it.path == "/api/v1/assistant/messages" }
        assertTrue(request.body.readUtf8().contains("\"texte\":\"Combien en restaurants ?\""))
    }

    @Test
    fun assistantConversationsCanBeOpenedRenamedPinnedAndDeleted() {
        app()
        compose.onNodeWithTag("onglet-assistant").performClick()
        compose.onNodeWithTag("liste-conversations").performClick()
        waitFor("conversation-3")
        compose.onNodeWithText("Abonnements").assertExists()
        // À gauche : sur l'écran de test (320 px), le centre de la ligne touche la zone des boutons.
        compose.onNodeWithTag("conversation-3").performTouchInput { click(percentOffset(0.1f, 0.5f)) }
        waitFor("message-1")
        compose.onNodeWithTag("titre-conversation").assertTextEquals("Abonnements")
        compose.onNodeWithText("Netflix : 13,99 € par mois").assertExists()  // Markdown rendu
        compose.onNodeWithText("1 consultation").assertExists()
        compose.onNodeWithTag("liste-conversations").performClick()
        waitFor("renommer-3")
        compose.onNodeWithTag("renommer-3").performClick()
        compose.onNodeWithTag("nouveau-titre").performTextReplacement("Mes abonnements")
        compose.onNodeWithTag("confirmer-titre").performClick()
        compose.onNodeWithContentDescription("Désépingler").assertExists()  // la 7 est épinglée
        compose.onNodeWithTag("epingler-3").performClick()
        compose.onNodeWithTag("supprimer-7").performClick()
        compose.onNodeWithTag("confirmer-suppression").performClick()
        val paths = generateSequence { server.takeRequest(1, TimeUnit.SECONDS) }.map { "${it.method} ${it.path}" }.toList()
        assertTrue(paths.contains("POST /api/v1/assistant/conversations/3/renommer"))
        assertTrue(paths.contains("POST /api/v1/assistant/conversations/3/epingler"))
        assertTrue(paths.contains("DELETE /api/v1/assistant/conversations/7"))
    }
}
