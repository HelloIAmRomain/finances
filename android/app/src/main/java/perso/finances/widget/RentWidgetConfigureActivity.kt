package perso.finances.widget

import android.appwidget.AppWidgetManager
import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import perso.finances.api.ApiClient
import perso.finances.api.ApiException
import perso.finances.api.Lease
import perso.finances.ui.FinancesTheme
import perso.finances.ui.Hint

/**
 * Choix des loyers et remboursements affichés par un widget, à sa pose (et ensuite par
 * « Paramètres » du widget). Rien n'est choisi d'office : tout est coché au départ.
 */
class RentWidgetConfigureActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        val widgetId = intent?.extras?.getInt(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
            ?: AppWidgetManager.INVALID_APPWIDGET_ID
        setResult(RESULT_CANCELED, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
        if (widgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish()
            return
        }
        val selection = WidgetSelection(this)
        val session = RentWidgetProvider.sessionStore(this).load()
        setContent {
            FinancesTheme {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    var leases by remember { mutableStateOf<List<Lease>?>(null) }
                    var error by remember { mutableStateOf<String?>(null) }
                    LaunchedEffect(session) {
                        if (session == null) {
                            error = "Ouvrez d'abord l'application Finances pour appairer ce téléphone, puis posez le widget."
                        } else {
                            try {
                                leases = ApiClient(session.server, session.token).rental().baux
                            } catch (e: ApiException) {
                                error = e.message
                            }
                        }
                    }
                    WidgetConfigureScreen(leases, error, selection.get(widgetId)) { chosen ->
                        selection.save(widgetId, chosen)
                        RentWidgetProvider.updateAll(this)
                        setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
                        finish()
                    }
                }
            }
        }
    }
}

@Composable
fun WidgetConfigureScreen(leases: List<Lease>?, error: String?, initial: Set<Int>?, onSave: (Set<Int>) -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Widget des loyers", style = MaterialTheme.typography.headlineSmall)
        Hint("Cochez ce que le widget affiche. Pour chacun, il montre ☑ si le mois en cours est payé, ☐ sinon.")
        when {
            error != null -> Text(error, color = MaterialTheme.colorScheme.error, modifier = Modifier.testTag("erreur-widget"))
            leases == null -> Hint("Chargement des loyers et remboursements…")
            leases.isEmpty() -> Hint("Aucun loyer ni remboursement suivi. Ils se créent depuis la version web (Loyers › Nouveau).")
            else -> {
                var chosen by remember(leases) { mutableStateOf(initial ?: leases.map { it.id }.toSet()) }
                leases.forEach { lease ->
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(
                            checked = lease.id in chosen,
                            onCheckedChange = { checked -> chosen = if (checked) chosen + lease.id else chosen - lease.id },
                            modifier = Modifier.testTag("choix-${lease.id}"),
                        )
                        Column {
                            Text(lease.nom)
                            Hint(if (lease.type == "loan") "Remboursement attendu" else "Loyer")
                        }
                    }
                }
                Button(
                    onClick = { onSave(chosen) },
                    enabled = chosen.isNotEmpty(),
                    modifier = Modifier.fillMaxWidth().testTag("enregistrer-widget"),
                ) { Text("Afficher dans le widget") }
                if (chosen.isEmpty()) Hint("Cochez au moins un élément.")
            }
        }
    }
}
