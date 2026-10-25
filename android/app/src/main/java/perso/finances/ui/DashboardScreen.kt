package perso.finances.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import perso.finances.api.Dashboard
import perso.finances.api.MonthFlow
import perso.finances.format.euros
import perso.finances.format.monthLabel
import perso.finances.format.savingsRate
import perso.finances.format.shiftMonth

@Composable
fun DashboardScreen(model: LoadViewModel<Dashboard>, snackbar: SnackbarHostState, onMonth: (String) -> Unit) {
    val state by model.state.collectAsStateWithLifecycle()
    Screen(state, model::refresh, snackbar, model::messageShown) { board ->
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                TextButton(onClick = { onMonth(shiftMonth(board.mois, -1)) }) { Text("← Précédent") }
                Text(monthLabel(board.mois).replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleMedium, modifier = Modifier.testTag("mois"))
                TextButton(onClick = { onMonth(shiftMonth(board.mois, 1)) }) { Text("Suivant →") }
            }
        }
        item {
            Section("Patrimoine (comptes suivis)") {
                Amount(board.patrimoine, strong = true, modifier = Modifier.testTag("patrimoine"))
                if (board.comptesSansSolde.isNotEmpty()) Hint("Sans solde connu : ${board.comptesSansSolde.joinToString()}")
            }
        }
        item {
            Section("Ce mois-ci") {
                LabelledRow("Recettes") { Amount(board.recettes) }
                LabelledRow("Dépenses") { Amount(board.depenses) }
                LabelledRow("Solde") { Amount(board.solde) }
                LabelledRow("Taux d'épargne") { Text(savingsRate(board.tauxEpargnePourMille)) }
                Hint("Il y a un an : recettes ${euros(board.recettesN1)}, dépenses ${euros(board.depensesN1)}")
            }
        }
        if (board.loyers.isNotEmpty()) {
            item {
                Section("Loyers et remboursements du mois") {
                    board.loyers.forEach { rent ->
                        LabelledRow(rent.bail) {
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                                if (rent.resteDu > 0 && rent.etat != "attendu") Hint("reste ${euros(rent.resteDu)}")
                                StateChip(rent.etat)
                            }
                        }
                    }
                }
            }
        }
        item { Section("Recettes et dépenses sur 12 mois") { FlowChart(board.flux12Mois) } }
        item {
            Section("Dépenses par catégorie") {
                val totals = board.categoryTotals
                if (totals.isEmpty()) Hint("Aucune dépense ce mois-ci.")
                val largest = totals.maxOfOrNull { -it.second }?.coerceAtLeast(1) ?: 1
                totals.forEach { (name, amount) ->
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        LabelledRow(name) { Amount(amount) }
                        ProportionBar(fraction = (-amount).toFloat() / largest)
                    }
                }
            }
        }
    }
}

@Composable
private fun ProportionBar(fraction: Float) {
    val color = MaterialTheme.colorScheme.primary
    val track = MaterialTheme.colorScheme.primaryContainer
    Canvas(Modifier.fillMaxWidth().height(6.dp)) {
        drawRect(track, size = size)
        drawRect(color, size = Size(size.width * fraction.coerceIn(0f, 1f), size.height))
    }
}

/** Barres recettes / dépenses sur 12 mois, dessinées sans bibliothèque externe. */
@Composable
fun FlowChart(flows: List<MonthFlow>) {
    val income = Positive
    val expense = Negative
    val peak = flows.maxOfOrNull { maxOf(it.recettes, -it.depenses) }?.coerceAtLeast(1) ?: 1
    Canvas(
        Modifier.fillMaxWidth().height(160.dp).semantics {
            contentDescription = "Recettes et dépenses sur ${flows.size} mois"
        },
    ) {
        if (flows.isEmpty()) return@Canvas
        val slot = size.width / flows.size
        val bar = slot * 0.35f
        flows.forEachIndexed { index, flow ->
            val x = index * slot + slot * 0.1f
            val h1 = size.height * flow.recettes / peak
            val h2 = size.height * (-flow.depenses) / peak
            drawRect(income, topLeft = Offset(x, size.height - h1), size = Size(bar, h1))
            drawRect(expense, topLeft = Offset(x + bar, size.height - h2), size = Size(bar, h2))
        }
    }
    Hint("Vert : recettes · rouge : dépenses")
}
