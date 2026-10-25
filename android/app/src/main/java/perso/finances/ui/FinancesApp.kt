package perso.finances.ui

import android.content.Intent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.AccountBox
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Person
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.core.net.toUri
import androidx.lifecycle.viewmodel.compose.viewModel
import perso.finances.api.ApiClient
import perso.finances.session.Session
import perso.finances.widget.RentWidgetProvider

enum class Tab(val title: String, val icon: ImageVector) {
    DASHBOARD("Tableau de bord", Icons.Filled.Home),
    RENTAL("Loyers", Icons.Filled.Person),
    ACCOUNTS("Comptes", Icons.Filled.AccountBox),
    OPERATIONS("Opérations", Icons.AutoMirrored.Filled.List),
    ALERTS("Alertes", Icons.Filled.Notifications),
    ASSISTANT("Assistant", Icons.Filled.Face),
}

/**
 * Application appairée : six onglets, menu (version web, désappairer). Un jeton refusé ramène
 * à l'appairage (`onUnauthorized`).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FinancesApp(
    session: Session,
    onUnauthorized: () -> Unit,
    onForget: () -> Unit,
    api: ApiClient = remember(session) { ApiClient(session.server, session.token) },
) {
    var tab by rememberSaveable { mutableStateOf(Tab.DASHBOARD) }
    var month by rememberSaveable { mutableStateOf<String?>(null) }
    var menu by remember { mutableStateOf(false) }
    val snackbar = remember { SnackbarHostState() }
    val context = LocalContext.current

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(tab.title) },
                actions = {
                    IconButton(onClick = { menu = true }, modifier = Modifier.testTag("menu")) {
                        Icon(Icons.Filled.MoreVert, contentDescription = "Menu")
                    }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        DropdownMenuItem(
                            text = { Text("Ouvrir la version web") },
                            onClick = {
                                menu = false
                                context.startActivity(Intent(Intent.ACTION_VIEW, session.server.toUri()))
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Désappairer ce téléphone") },
                            onClick = {
                                menu = false
                                onForget()
                            },
                            modifier = Modifier.testTag("desappairer"),
                        )
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                Tab.entries.forEach { entry ->
                    NavigationBarItem(
                        selected = tab == entry,
                        onClick = { tab = entry },
                        icon = { Icon(entry.icon, contentDescription = null) },
                        label = { Text(entry.title.substringBefore(" ")) },
                        modifier = Modifier.testTag("onglet-${entry.name.lowercase()}"),
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.padding(padding)) {
            when (tab) {
                Tab.DASHBOARD -> {
                    val model = viewModel(key = "tableau-${month ?: "courant"}") {
                        LoadViewModel({ api.dashboard(month) }, onUnauthorized)
                    }
                    DashboardScreen(model, snackbar) { month = it }
                }
                Tab.RENTAL -> {
                    val model = viewModel(key = "loyers") { LoadViewModel({ api.rental() }, onUnauthorized) }
                    RentalScreen(
                        model,
                        snackbar,
                        onDecide = { id, confirm -> model.act { api.decideProposal(id, confirm).message } },
                        onRename = { id, name ->
                            model.act { api.renameLease(id, name).message.also { RentWidgetProvider.updateAll(context) } }
                        },
                    )
                }
                Tab.ACCOUNTS -> {
                    val model = viewModel(key = "comptes") { LoadViewModel({ api.accounts() }, onUnauthorized) }
                    AccountsScreen(model, snackbar) { id -> model.act { api.sync(id).message } }
                }
                Tab.OPERATIONS -> {
                    val model = viewModel(key = "operations") { TransactionsViewModel(api, onUnauthorized) }
                    TransactionsScreen(model, snackbar)
                }
                Tab.ALERTS -> {
                    val model = viewModel(key = "alertes") { LoadViewModel({ api.alerts() }, onUnauthorized) }
                    AlertsScreen(model, snackbar) { id, resolve -> model.act { api.alertAction(id, resolve).message } }
                }
                Tab.ASSISTANT -> {
                    val model = viewModel(key = "assistant") { AssistantViewModel(api, onUnauthorized) }
                    AssistantScreen(model, snackbar)
                }
            }
        }
    }
}
