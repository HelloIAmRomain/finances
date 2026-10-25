package perso.finances.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import perso.finances.api.Lease
import perso.finances.api.Rental
import perso.finances.format.day
import perso.finances.format.euros
import perso.finances.format.monthLabel

@Composable
fun RentalScreen(
    model: LoadViewModel<Rental>,
    snackbar: SnackbarHostState,
    onDecide: (allocationId: Int, confirm: Boolean) -> Unit,
    onRename: (leaseId: Int, name: String) -> Unit = { _, _ -> },
) {
    val state by model.state.collectAsStateWithLifecycle()
    var renaming by remember { mutableStateOf<Lease?>(null) }
    renaming?.let { lease ->
        LeaseRenameDialog(lease, onDismiss = { renaming = null }) { name ->
            renaming = null
            onRename(lease.id, name.trim())
        }
    }
    Screen(state, model::refresh, snackbar, model::messageShown) { rental ->
        if (rental.aValider.isNotEmpty()) {
            item {
                Section("À valider (${rental.aValider.size})", Modifier.testTag("a-valider")) {
                    Hint("Montant exact d'une échéance, mais émetteur non reconnu : rien n'est affecté sans votre accord.")
                    rental.aValider.forEach { p ->
                        LabelledRow("${day(p.date)} · ${p.libelle}") { Amount(p.montant) }
                        Hint("${p.bail}${p.mois?.let { " · ${monthLabel(it)}" } ?: ""}")
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = { onDecide(p.id, true) }, modifier = Modifier.testTag("confirmer-${p.id}")) { Text("Confirmer") }
                            OutlinedButton(onClick = { onDecide(p.id, false) }) { Text("Refuser") }
                        }
                    }
                }
            }
        }
        if (rental.tropPercus.isNotEmpty()) {
            item {
                Section("Trop-perçus à traiter (${rental.tropPercus.size})") {
                    rental.tropPercus.forEach { o -> LabelledRow("${day(o.date)} · ${o.bail}") { Amount(o.montant) } }
                    Hint("Report ou remboursement : à décider depuis la version web.")
                }
            }
        }
        if (rental.baux.isEmpty()) {
            item { Section("Loyers") { Hint("Aucun bail ni remboursement attendu. Ils se créent depuis la version web (Loyers › Nouveau).") } }
        }
        rental.baux.forEach { lease ->
            item(key = "bail-${lease.id}") {
                Section(lease.nom, Modifier.testTag("bail-${lease.id}")) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.weight(1f)) {
                            Hint(
                                listOfNotNull(
                                    if (lease.type == "loan") "Remboursement attendu" else "Loyer",
                                    lease.bien,
                                    "sur ${lease.compte}",
                                    "${euros(lease.mensuel)} par mois",
                                    "alerte le ${lease.jourAlerte}, impayé le ${lease.jourImpaye}",
                                ).joinToString(" · "),
                            )
                        }
                        IconButton(onClick = { renaming = lease }, modifier = Modifier.testTag("renommer-bail-${lease.id}")) {
                            Icon(Icons.Filled.Edit, contentDescription = "Renommer « ${lease.nom} »")
                        }
                    }
                    lease.echeances.take(6).forEach { due ->
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(monthLabel(due.mois), modifier = Modifier.weight(1f))
                            if (due.resteDu > 0 && due.etat != "attendu") Hint("reste ${euros(due.resteDu)}")
                            StateChip(due.etat)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun LeaseRenameDialog(lease: Lease, onDismiss: () -> Unit, onConfirm: (String) -> Unit) {
    var name by rememberSaveable(lease.id) { mutableStateOf(lease.nom) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Renommer") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Hint("Nom affiché dans l'application, la version web et le widget.")
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it.take(128) },
                    singleLine = true,
                    label = { Text("Nouveau nom") },
                    modifier = Modifier.testTag("nouveau-nom"),
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(name) }, enabled = name.isNotBlank(), modifier = Modifier.testTag("confirmer-nom")) {
                Text("Renommer")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Annuler") } },
    )
}
