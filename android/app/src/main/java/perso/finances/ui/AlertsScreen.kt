package perso.finances.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import perso.finances.api.Alert
import perso.finances.format.instant

private val SEVERITY = mapOf("critical" to "Critique", "warning" to "Avertissement", "info" to "Information")

@Composable
fun AlertsScreen(
    model: LoadViewModel<List<Alert>>,
    snackbar: SnackbarHostState,
    onAction: (alertId: Int, resolve: Boolean) -> Unit,
) {
    val state by model.state.collectAsStateWithLifecycle()
    Screen(state, model::refresh, snackbar, model::messageShown) { alerts ->
        if (alerts.isEmpty()) item { Section("Alertes") { Hint("Aucune alerte ouverte. Tout va bien.") } }
        alerts.forEach { alert ->
            item(key = alert.id) {
                Section(alert.titre, Modifier.testTag("alerte-${alert.id}")) {
                    Text(
                        SEVERITY[alert.gravite] ?: alert.gravite,
                        color = when (alert.gravite) {
                            "critical" -> MaterialTheme.colorScheme.error
                            "warning" -> Warning
                            else -> MaterialTheme.colorScheme.onSurfaceVariant
                        },
                        style = MaterialTheme.typography.labelLarge,
                    )
                    Hint("Ouverte le ${instant(alert.ouverteLe)}${if (alert.etat == "seen") " · vue" else ""}")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (alert.etat == "open") TextButton(onClick = { onAction(alert.id, false) }) { Text("Marquer vue") }
                        OutlinedButton(onClick = { onAction(alert.id, true) }, modifier = Modifier.testTag("resoudre-${alert.id}")) { Text("Résoudre") }
                    }
                }
            }
        }
    }
}
