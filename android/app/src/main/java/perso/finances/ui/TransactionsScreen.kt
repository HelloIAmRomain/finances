package perso.finances.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import perso.finances.api.ApiClient
import perso.finances.api.ApiException
import perso.finances.api.Category
import perso.finances.api.Transaction
import perso.finances.format.day

data class TransactionsState(
    val query: String = "",
    val items: List<Transaction> = emptyList(),
    val total: Int = 0,
    val page: Int = 0,
    val loading: Boolean = false,
    val error: String? = null,
    val message: String? = null,
    val categories: List<Category> = emptyList(),
)

/** Recherche paginée (50 par page, « Charger plus »), classement d'une opération. */
class TransactionsViewModel(private val api: ApiClient, private val onUnauthorized: () -> Unit) : ViewModel() {
    private val _state = MutableStateFlow(TransactionsState())
    val state: StateFlow<TransactionsState> = _state.asStateFlow()

    init {
        search("")
    }

    fun search(query: String) {
        _state.value = TransactionsState(query = query, loading = true, categories = _state.value.categories)
        load(page = 1)
    }

    fun more() {
        if (!_state.value.loading && _state.value.items.size < _state.value.total) load(_state.value.page + 1)
    }

    private fun load(page: Int) {
        _state.value = _state.value.copy(loading = true, error = null)
        viewModelScope.launch {
            try {
                val result = api.transactions(_state.value.query, page)
                val categories = _state.value.categories.ifEmpty { api.categories() }
                _state.value = _state.value.copy(
                    items = if (page == 1) result.transactions else _state.value.items + result.transactions,
                    total = result.total,
                    page = page,
                    loading = false,
                    categories = categories,
                )
            } catch (e: ApiException.Unauthorized) {
                onUnauthorized()
            } catch (e: ApiException) {
                _state.value = _state.value.copy(loading = false, error = e.message)
            }
        }
    }

    fun categorize(transactionId: Int, categoryId: Int) {
        viewModelScope.launch {
            val message = try {
                api.categorize(transactionId, categoryId).message
            } catch (e: ApiException) {
                e.message
            }
            _state.value = _state.value.copy(message = message)
            search(_state.value.query)
        }
    }

    fun messageShown() {
        _state.value = _state.value.copy(message = null)
    }
}

@Composable
fun TransactionsScreen(model: TransactionsViewModel, snackbar: SnackbarHostState) {
    val state by model.state.collectAsStateWithLifecycle()
    var query by remember { mutableStateOf(state.query) }
    var picking by remember { mutableStateOf<Transaction?>(null) }
    LaunchedEffect(state.message) {
        state.message?.let {
            snackbar.showSnackbar(it)
            model.messageShown()
        }
    }
    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.testTag("operations"),
    ) {
        item {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it; model.search(it) },
                label = { Text("Rechercher dans les libellés") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().testTag("recherche"),
            )
        }
        item { Hint("${state.total} opération(s)") }
        state.error?.let { item { ErrorCard(it) { model.search(state.query) } } }
        items(state.items, key = { it.id }) { tx ->
            Column(Modifier.fillMaxWidth().clickable { picking = tx }.testTag("operation-${tx.id}")) {
                LabelledRow(tx.libelle) { Amount(tx.montant) }
                Hint(
                    listOf(day(tx.date), tx.compte, if (tx.virementInterne) "virement interne" else tx.categories.joinToString().ifEmpty { "non catégorisée" })
                        .joinToString(" · "),
                )
                HorizontalDivider(Modifier.fillMaxWidth())
            }
        }
        if (state.items.size < state.total) {
            item { OutlinedButton(onClick = model::more, modifier = Modifier.fillMaxWidth()) { Text("Charger plus") } }
        }
    }
    picking?.let { tx ->
        AlertDialog(
            onDismissRequest = { picking = null },
            title = { Text("Classer : ${tx.libelle}", style = MaterialTheme.typography.titleSmall) },
            text = {
                LazyColumn(Modifier.testTag("categories")) {
                    items(state.categories, key = { it.id }) { category ->
                        Text(
                            category.libelle,
                            modifier = Modifier.fillMaxWidth().clickable {
                                model.categorize(tx.id, category.id)
                                picking = null
                            }.padding(vertical = 10.dp),
                        )
                    }
                }
            },
            confirmButton = { TextButton(onClick = { picking = null }) { Text("Annuler") } },
        )
    }
}
