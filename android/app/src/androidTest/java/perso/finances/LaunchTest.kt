package perso.finances

import android.view.WindowManager
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import perso.finances.session.KeystoreSessionStore
import perso.finances.session.Session

/** Sur un vrai système Android : démarrage, écran d'appairage, magasin de clés matériel. */
@RunWith(AndroidJUnit4::class)
class LaunchTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    @Before
    fun noSession() {
        KeystoreSessionStore(InstrumentationRegistry.getInstrumentation().targetContext).clear()
    }

    @Test
    fun startsOnPairingScreenWithScreenshotsForbidden() {
        compose.onNodeWithText("Appairer ce téléphone").assertIsDisplayed()
        val flags = compose.activity.window.attributes.flags
        assertTrue("FLAG_SECURE attendu", flags and WindowManager.LayoutParams.FLAG_SECURE != 0)
    }

    @Test
    fun plainHttpAddressIsRefused() {
        compose.onNodeWithTag("serveur").performTextInput("http://pi.exemple.ts.net")
        compose.onNodeWithTag("code").performTextInput("ABCD-EFGH")
        compose.onNodeWithTag("appairer").performClick()
        compose.onNodeWithTag("erreur").assertIsDisplayed()
    }

    @Test
    fun tokenIsSealedByTheAndroidKeystore() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val store = KeystoreSessionStore(context)
        store.save(Session("https://pi.exemple.ts.net", "jeton-factice-de-test"))
        val raw = context.getSharedPreferences("session", 0).getString("token", null).orEmpty()
        assertTrue("le jeton ne doit pas être stocké en clair", "jeton-factice" !in raw && raw.isNotEmpty())
        assertEquals(Session("https://pi.exemple.ts.net", "jeton-factice-de-test"), KeystoreSessionStore(context).load())
        store.clear()
        assertNull(store.load())
    }
}
