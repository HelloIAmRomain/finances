package perso.finances.ui

import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import perso.finances.api.Account
import perso.finances.format.day
import perso.finances.format.instant

private val TYPES = mapOf("checking" to "Compte courant", "savings" to "Livret", "card" to "Carte", "other" to "Autre")

@Composable
fun AccountsScreen(model: LoadViewModel<List<Account>>, snackbar: SnackbarHostState, onSync: (Int) -> Unit) {
    val state by model.state.collectAsStateWithLifecycle()
    Screen(state, model::refresh, snackbar, model::messageShown) { accounts ->
        accounts.forEach { account ->
            item(key = account.id) {
                Section(account.nom, Modifier.testTag("compte-${account.id}")) {
                    Hint(
                        listOfNotNull(
                            TYPES[account.type] ?: account.type,
                            account.banque ?: "import de fichiers",
                            if (!account.compte) "consultation seule" else null,
                        ).joinToString(" · "),
                    )
                    account.solde?.let { balance ->
                        LabelledRow(if (account.soldeType == "available") "Solde prévisionnel au ${day(account.soldeDate)}" else "Solde au ${day(account.soldeDate)}") {
                            Amount(balance)
                        }
                    }
                    if (account.banque != null) {
                        Hint("Dernière mise à jour : ${instant(account.derniereSynchro)}")
                        account.derniereErreur?.let { Hint("Dernier essai : $it") }
                        account.consentementJusquAu?.let { Hint("Accès à la banque jusqu'au ${day(it)}") }
                        OutlinedButton(
                            enabled = account.synchroManuelleImpossible == null,
                            onClick = { onSync(account.id) },
                            modifier = Modifier.testTag("synchroniser-${account.id}"),
                        ) { Text("Mettre à jour") }
                        account.synchroManuelleImpossible?.let { Hint(it) }
                    }
                }
            }
        }
    }
}
