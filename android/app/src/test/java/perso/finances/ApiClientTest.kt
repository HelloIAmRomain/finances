package perso.finances

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import perso.finances.api.ApiClient
import perso.finances.api.ApiException

class ApiClientTest {
    private val server = MockWebServer()
    private lateinit var api: ApiClient

    @Before
    fun start() {
        server.dispatcher = Fixtures.dispatcher()
        server.start()
        api = ApiClient(server.url("/").toString(), token = "jeton-factice-de-test", io = Dispatchers.Unconfined)
    }

    @After
    fun stop() = server.shutdown()

    @Test
    fun `every endpoint parses its reference response`() = runTest {
        val board = api.dashboard("2026-09")
        assertEquals(345086L, board.solde)
        assertEquals(listOf("Non catégorisé" to -12535L, "Alimentation" to -7379L), board.categoryTotals)
        assertEquals("partiel", board.loyers.single().etat)
        val accounts = api.accounts()
        assertEquals(listOf(2, 3), accounts.map { it.id })
        assertNull(accounts[0].synchroManuelleImpossible)
        assertEquals(2, api.transactions("super", 1).total)
        assertEquals(2, api.categories().size)
        val rental = api.rental()
        assertEquals(7, rental.aValider.single().id)
        assertEquals(listOf("attendu", "partiel"), rental.baux.single().echeances.map { it.etat })
        assertEquals(listOf("open", "seen"), api.alerts().map { it.etat })
        val start = api.sendMessage("Combien ?", null)
        assertEquals(7, start.conversation)
        val turn = api.turn(start.tour, 0)
        assertEquals("termine", turn.etat)
        assertEquals(listOf("depenses_par_categorie"), turn.outils)
    }

    @Test
    fun `requests carry the token and the expected paths`() = runTest {
        api.dashboard("2026-09")
        val board = server.takeRequest()
        assertEquals("/api/v1/tableau-de-bord?mois=2026-09", board.path)
        assertEquals("Bearer jeton-factice-de-test", board.getHeader("Authorization"))
        api.transactions("  ", 2)
        assertEquals("/api/v1/transactions?page=2", server.takeRequest().path)
        assertEquals("Action faite", api.decideProposal(7, confirm = true).message)
        assertEquals("/api/v1/loyers/propositions/7/confirmer", server.takeRequest().path)
        api.alertAction(5, resolve = false)
        assertEquals("/api/v1/alertes/5/vue", server.takeRequest().path)
        api.categorize(11, 2)
        val categorize = server.takeRequest()
        assertEquals("""{"categorie":2}""", categorize.body.readUtf8())
        api.sendMessage("Et en août ?", 7)
        val question = server.takeRequest()
        assertEquals("/api/v1/assistant/messages", question.path)
        assertEquals("""{"texte":"Et en août ?","conversation":7}""", question.body.readUtf8())
        api.turn("tour-factice-1", 3)
        assertEquals("/api/v1/assistant/tours/tour-factice-1?depuis=3", server.takeRequest().path)
    }

    @Test
    fun `pairing needs no token`() = runTest {
        val anonymous = ApiClient(server.url("/").toString(), token = null, io = Dispatchers.Unconfined)
        assertEquals("jeton-factice-de-test", anonymous.pair("ABCD-EFGH", "Téléphone").jeton)
        assertEquals("""{"code":"ABCD-EFGH","nom":"Téléphone"}""", server.takeRequest().body.readUtf8())
    }

    @Test
    fun `errors become readable exceptions`() = runTest {
        val revoked = ApiClient(server.url("/").toString(), token = "revoque", io = Dispatchers.Unconfined)
        val unauthorized = runCatching { revoked.me() }.exceptionOrNull()
        assertTrue(unauthorized is ApiException.Unauthorized)
        assertEquals("Appareil non appairé ou révoqué.", unauthorized?.message)

        server.dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
            override fun dispatch(request: okhttp3.mockwebserver.RecordedRequest) =
                MockResponse().setResponseCode(429).setBody("""{"detail":"Une synchro manuelle par quart d'heure"}""")
        }
        val refused = runCatching { api.sync(2) }.exceptionOrNull()
        assertTrue(refused is ApiException.Refused)
        assertEquals(429, (refused as ApiException.Refused).status)
        assertEquals("Une synchro manuelle par quart d'heure", refused.message)

        val unreadable = ApiClient(server.url("/").toString(), token = "é", io = Dispatchers.Unconfined)
        assertTrue(runCatching { unreadable.me() }.exceptionOrNull() is ApiException.Unauthorized)

        server.dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
            override fun dispatch(request: okhttp3.mockwebserver.RecordedRequest) = MockResponse().setBody("pas du json")
        }
        val garbled = runCatching { api.accounts() }.exceptionOrNull()
        assertTrue(garbled is ApiException.Refused && garbled.message!!.contains("inattendue"))

        server.dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
            override fun dispatch(request: okhttp3.mockwebserver.RecordedRequest) =
                MockResponse().setBody("[1,2,3]").setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY)
        }
        assertTrue(runCatching { api.accounts() }.exceptionOrNull() is ApiException.Unreachable)

        server.shutdown()
        val unreachable = runCatching { api.accounts() }.exceptionOrNull()
        assertTrue(unreachable is ApiException.Unreachable)
        assertTrue(unreachable!!.message!!.contains("Tailscale"))
    }
}
