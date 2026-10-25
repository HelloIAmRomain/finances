package perso.finances

import android.appwidget.AppWidgetManager
import android.content.Context
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import perso.finances.api.ApiClient
import perso.finances.api.Rental
import perso.finances.session.MemorySessionStore
import perso.finances.session.Session
import perso.finances.ui.FinancesTheme
import perso.finances.widget.RentWidgetProvider
import perso.finances.widget.RentWidgetRows
import perso.finances.widget.WidgetConfigureScreen
import perso.finances.widget.WidgetContent
import perso.finances.widget.WidgetSelection

@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class WidgetTest {
    @get:Rule
    val compose = createComposeRule()
    private val server = MockWebServer()
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val rental: Rental = ApiClient.json.decodeFromString(Rental.serializer(), Fixtures.text("loyers"))

    @Before
    fun start() {
        server.dispatcher = Fixtures.dispatcher()
        server.start()
    }

    @After
    fun stop() {
        server.shutdown()
        RentWidgetProvider.sessionStore = { MemorySessionStore() }
    }

    @Test
    fun `one box per chosen lease, ticked when the month is paid`() {
        val october = RentWidgetRows.rows(rental, null, "2026-10")
        assertEquals(listOf("☐ Studio démo"), october.map { it.line })
        assertEquals("attendu", october.single().detail)
        val paid = rental.copy(baux = rental.baux.map { lease ->
            lease.copy(echeances = lease.echeances.map { if (it.mois == "2026-10") it.copy(etat = "paye") else it })
        })
        assertEquals("☑ Studio démo", RentWidgetRows.rows(paid, null, "2026-10").single().line)
        assertEquals("pas d'échéance ce mois-ci", RentWidgetRows.rows(rental, null, "2027-01").single().detail)
        assertTrue(RentWidgetRows.rows(rental, emptySet(), "2026-10").isEmpty())
        val none = RentWidgetRows.content(rental, emptySet(), "2026-10") as WidgetContent.Message
        assertTrue(none.text.startsWith("Aucun élément choisi"))
        val empty = RentWidgetRows.content(rental.copy(baux = emptyList()), null, "2026-10") as WidgetContent.Message
        assertTrue(empty.text.startsWith("Aucun loyer"))
        assertEquals("2026-10", RentWidgetRows.currentMonth(java.time.LocalDate.of(2026, 10, 7)))
    }

    @Test
    fun `selection is remembered per widget`() {
        val selection = WidgetSelection(context)
        assertNull(selection.get(42))
        selection.save(42, setOf(1, 3))
        assertEquals(setOf(1, 3), selection.get(42))
        selection.remove(42)
        assertNull(selection.get(42))
    }

    private fun message(content: WidgetContent?): String = (content as WidgetContent.Message).text

    @Test
    fun `widget shows the month rows from the server`() = runBlocking {
        RentWidgetProvider.sessionStore = { MemorySessionStore(Session(server.url("/").toString(), "jeton-factice-de-test")) }
        val manager = AppWidgetManager.getInstance(context)
        val id = shadowOf(manager).createWidget(RentWidgetProvider::class.java, R.layout.widget_rents)
        val content = RentWidgetProvider.contents(context, intArrayOf(id)).getValue(id) as WidgetContent.Rows
        assertEquals(listOf("☐ Studio démo"), content.rows.map { it.line }) // échéance du mois non payée (ou absente)
        RentWidgetProvider.refresh(context, manager, intArrayOf(id))
        assertTrue(shadowOf(manager).getViewFor(id) != null)
    }

    @Test
    fun `widget explains what to do when the phone is not paired or the server refuses`() = runBlocking {
        RentWidgetProvider.sessionStore = { MemorySessionStore() }
        assertTrue(message(RentWidgetProvider.contents(context, intArrayOf(1))[1]).startsWith("Ouvrez l'application Finances"))
        RentWidgetProvider.sessionStore = { MemorySessionStore(Session(server.url("/").toString(), "jeton-revoque")) }
        assertTrue(message(RentWidgetProvider.contents(context, intArrayOf(1))[1]).startsWith("Ouvrez l'application Finances"))
        RentWidgetProvider.sessionStore = { MemorySessionStore(Session("https://127.0.0.1:1/", "jeton-factice-de-test")) }
        assertEquals(
            context.getString(R.string.widget_unreachable),
            message(RentWidgetProvider.contents(context, intArrayOf(1))[1]),
        )
    }

    @Test
    fun `rendering titles the month or names the widget`() {
        val rows = RentWidgetRows.content(rental, null, "2026-09") as WidgetContent.Rows
        assertEquals("Septembre 2026", RentWidgetProvider.title(context, rows))
        assertEquals(listOf("☐ Studio démo" to "partiel"), rows.rows.map { it.line to it.detail })
        val message = WidgetContent.Message("Aucun élément choisi.")
        assertEquals("Loyers du mois", RentWidgetProvider.title(context, message))
        // Vues construites sans erreur dans les deux cas (leur affichage relève de l'écran d'accueil).
        assertEquals(R.layout.widget_rents, RentWidgetProvider.views(context, 7, rows).layoutId)
        assertEquals(R.layout.widget_rents, RentWidgetProvider.views(context, 7, message).layoutId)
    }

    @Test
    fun `configuration lets the user pick the leases shown`() {
        var saved: Set<Int>? = null
        compose.setContent {
            FinancesTheme(dark = false) { WidgetConfigureScreen(rental.baux, null, null) { saved = it } }
        }
        compose.onNodeWithTag("choix-1").performClick()
        compose.onNodeWithTag("enregistrer-widget").assertIsNotEnabled()
        compose.onNodeWithText("Cochez au moins un élément.").assertExists()
        compose.onNodeWithTag("choix-1").performClick()
        compose.onNodeWithTag("enregistrer-widget").performClick()
        assertEquals(setOf(1), saved)
    }

    @Test
    fun `configuration explains a missing pairing`() {
        compose.setContent {
            FinancesTheme(dark = false) { WidgetConfigureScreen(null, "Ouvrez d'abord l'application.", null) {} }
        }
        compose.onNodeWithTag("erreur-widget").assertExists()
    }
}
