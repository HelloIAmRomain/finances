package perso.finances.ui

import android.os.Build
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import perso.finances.api.ApiClient
import perso.finances.api.ApiException
import perso.finances.session.Session

/** Adresse du serveur valide : HTTPS obligatoire (Tailscale fournit le certificat). */
fun normalizeServer(input: String): String? {
    val trimmed = input.trim().trimEnd('/')
    val host = when {
        trimmed.startsWith("https://") -> trimmed.removePrefix("https://")
        "://" in trimmed -> return null // http:// ou autre : refusé
        else -> trimmed
    }
    return if (HOST.matches(host)) "https://$host" else null
}

private val HOST = Regex("^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\\d{1,5})?$")

/**
 * Appairage : adresse du serveur (celle affichée par la page « Appareils ») et code à usage
 * unique. Le jeton reçu est aussitôt chiffré par le magasin de clés du téléphone.
 */
@Composable
fun PairingScreen(
    onPaired: (Session) -> Unit,
    client: (String) -> ApiClient = { ApiClient(it, token = null) },
) {
    var server by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Finances", style = MaterialTheme.typography.headlineMedium)
        Hint("Sur l'ordinateur, ouvrez la page « Appareils » de l'interface web et cliquez sur « Appairer un téléphone ». Tailscale doit être actif sur ce téléphone.")
        OutlinedTextField(
            value = server,
            onValueChange = { server = it },
            label = { Text("Adresse du serveur") },
            placeholder = { Text("https://nom-du-pi.tailnet.ts.net") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth().testTag("serveur"),
        )
        OutlinedTextField(
            value = code,
            onValueChange = { code = it.uppercase().take(9) },
            label = { Text("Code d'appairage") },
            placeholder = { Text("ABCD-EFGH") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters),
            modifier = Modifier.fillMaxWidth().testTag("code"),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.testTag("erreur")) }
        Button(
            enabled = !busy && server.isNotBlank() && code.length >= 8,
            onClick = {
                val base = normalizeServer(server)
                if (base == null) {
                    error = "Adresse invalide : elle doit commencer par https://"
                    return@Button
                }
                busy = true
                error = null
                scope.launch {
                    try {
                        val paired = client(base).pair(code, "${Build.MANUFACTURER} ${Build.MODEL}".trim())
                        onPaired(Session(base, paired.jeton))
                    } catch (e: ApiException) {
                        error = e.message
                    } finally {
                        busy = false
                    }
                }
            },
            modifier = Modifier.fillMaxWidth().testTag("appairer"),
        ) {
            if (busy) CircularProgressIndicator(Modifier.padding(2.dp)) else Text("Appairer ce téléphone")
        }
    }
}
