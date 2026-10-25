package perso.finances

import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import perso.finances.session.KeystoreSessionStore
import perso.finances.session.Session
import perso.finances.session.SessionStore
import perso.finances.ui.FinancesApp
import perso.finances.ui.FinancesTheme
import perso.finances.ui.Hint
import perso.finances.ui.PairingScreen

/**
 * Point d'entrée. Données financières à l'écran : capture d'écran et aperçu des tâches récentes
 * interdits (FLAG_SECURE). Verrouillage par empreinte ou code du téléphone à l'ouverture et après
 * deux minutes en arrière-plan.
 */
class MainActivity : FragmentActivity() {
    private lateinit var store: SessionStore
    private var session by mutableStateOf<Session?>(null)
    private var locked by mutableStateOf(true)
    private var backgroundSince = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        store = KeystoreSessionStore(this)
        session = store.load()
        locked = canLock()
        setContent {
            FinancesTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    val current = session
                    when {
                        current == null -> PairingScreen(onPaired = { paired ->
                            store.save(paired)
                            session = paired
                            locked = false
                        })
                        locked -> LockScreen(::unlock)
                        else -> FinancesApp(
                            session = current,
                            onUnauthorized = ::forget,
                            onForget = ::forget,
                        )
                    }
                }
            }
        }
    }

    override fun onStop() {
        super.onStop()
        backgroundSince = SystemClock.elapsedRealtime()
    }

    override fun onStart() {
        super.onStart()
        if (backgroundSince > 0 && SystemClock.elapsedRealtime() - backgroundSince > LOCK_AFTER_MS && canLock()) {
            locked = true
        }
        if (locked) unlock()
    }

    private fun forget() {
        store.clear()
        // Les écrans gardent le client du jeton retiré : sans cela, un nouvel appairage
        // retrouverait les mêmes modèles (refus en boucle, ou données de l'ancien serveur).
        viewModelStore.clear()
        session = null
    }

    private fun canLock(): Boolean =
        session != null &&
            BiometricManager.from(this).canAuthenticate(BIOMETRIC_WEAK or DEVICE_CREDENTIAL) == BiometricManager.BIOMETRIC_SUCCESS

    private fun unlock() {
        if (!canLock()) {
            locked = false
            return
        }
        val prompt = BiometricPrompt(
            this,
            ContextCompat.getMainExecutor(this),
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    locked = false
                }
            },
        )
        prompt.authenticate(
            BiometricPrompt.PromptInfo.Builder()
                .setTitle("Finances")
                .setSubtitle("Déverrouiller l'accès à vos comptes")
                .setAllowedAuthenticators(BIOMETRIC_WEAK or DEVICE_CREDENTIAL)
                .build(),
        )
    }

    private companion object {
        const val LOCK_AFTER_MS = 2 * 60 * 1000L
    }
}

@Composable
private fun LockScreen(onUnlock: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Finances verrouillé", style = MaterialTheme.typography.headlineSmall)
        Hint("Empreinte digitale ou code du téléphone.")
        Button(onClick = onUnlock) { Text("Déverrouiller") }
    }
}
