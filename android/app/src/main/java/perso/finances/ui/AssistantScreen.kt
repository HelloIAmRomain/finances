package perso.finances.ui

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.speech.RecognizerIntent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.AlertDialog
import androidx.compose.runtime.remember
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import perso.finances.api.ApiClient
import perso.finances.api.ApiException
import perso.finances.api.ConversationSummary
import perso.finances.format.instant

data class Exchange(
    val fromMe: Boolean,
    val text: String,
    val detail: String? = null,
    val steps: List<String> = emptyList(),
    val pending: Boolean = false,
    val failed: Boolean = false,
)

data class AssistantState(
    val exchanges: List<Exchange> = emptyList(),
    val conversation: Int? = null,
    val title: String? = null,
    val sending: Boolean = false,
    val error: String? = null,
    val showList: Boolean = false,
    val conversations: List<ConversationSummary> = emptyList(),
    val loadingList: Boolean = false,
)

private const val POLL_MS = 800L

/** Conversation avec l'assistant (serveur : mêmes outils que le client MCP du PC, annulables). */
class AssistantViewModel(private val api: ApiClient, private val onUnauthorized: () -> Unit) : ViewModel() {
    private val _state = MutableStateFlow(AssistantState())
    val state: StateFlow<AssistantState> = _state.asStateFlow()

    fun send(text: String) {
        val question = text.trim()
        if (question.isEmpty() || _state.value.sending) return
        _state.value = _state.value.copy(
            exchanges = _state.value.exchanges + Exchange(true, question) + Exchange(false, "", pending = true),
            sending = true,
            error = null,
        )
        viewModelScope.launch {
            try {
                val start = api.sendMessage(question, _state.value.conversation)
                _state.value = _state.value.copy(conversation = start.conversation, title = start.titre)
                follow(start.tour)
            } catch (e: ApiException.Unauthorized) {
                onUnauthorized()
            } catch (e: ApiException) {
                replaceLast(Exchange(false, e.message ?: "Échec de l'envoi.", failed = true))
                _state.value = _state.value.copy(sending = false)
            }
        }
    }

    /** Suit le tour sur le serveur : étapes, puis texte qui s'écrit, puis réponse finale. */
    private suspend fun follow(id: String) {
        var since = 0
        val steps = mutableListOf<String>()
        while (true) {
            val turn = try {
                api.turn(id, since)
            } catch (_: ApiException.Unreachable) {
                delay(POLL_MS * 3) // réseau mobile coupé : on réessaie
                continue
            }
            turn.evenements.forEach { event ->
                since = maxOf(since, event.n)
                if (event.type == "etape") steps += event.texte
            }
            when (turn.etat) {
                "en_cours" -> {
                    replaceLast(Exchange(false, turn.textePartiel, steps = steps.toList(), pending = true))
                    delay(POLL_MS)
                }
                "termine" -> {
                    val text = turn.reponse ?: turn.textePartiel
                    replaceLast(Exchange(false, text, detail(turn.outils, turn.modifications), steps.toList()))
                    _state.value = _state.value.copy(sending = false)
                    return
                }
                else -> {
                    replaceLast(Exchange(false, turn.erreur ?: "Échec de la réponse.", steps = steps.toList(), failed = true))
                    _state.value = _state.value.copy(sending = false)
                    return
                }
            }
        }
    }

    private fun replaceLast(exchange: Exchange) {
        _state.value = _state.value.copy(exchanges = _state.value.exchanges.dropLast(1) + exchange)
    }

    fun restart() {
        if (!_state.value.sending) _state.value = AssistantState()
    }

    /** Liste des conversations mémorisées (la plus récente d'abord). */
    fun showConversations(visible: Boolean = true) {
        _state.value = _state.value.copy(showList = visible)
        if (visible) guarded {
            _state.value = _state.value.copy(loadingList = true)
            _state.value = _state.value.copy(conversations = api.conversations(), loadingList = false)
        }
    }

    fun open(id: Int) = guarded {
        val detail = api.conversation(id)
        _state.value = AssistantState(
            exchanges = detail.messages.map { Exchange(it.role == "user", it.texte, detail(it.outils, emptyList())) },
            conversation = detail.id,
            title = detail.titre,
        )
    }

    fun rename(id: Int, title: String) = guarded {
        val detail = api.renameConversation(id, title.trim())
        _state.value = _state.value.copy(
            title = if (_state.value.conversation == id) detail.titre else _state.value.title,
            conversations = api.conversations(),
        )
    }

    /** Épingle (en tête de liste) ou désépingle ; la liste revient triée par le serveur. */
    fun pin(id: Int, pinned: Boolean) = guarded {
        api.pinConversation(id, pinned)
        _state.value = _state.value.copy(conversations = api.conversations())
    }

    fun delete(id: Int) = guarded {
        api.deleteConversation(id)
        val current = _state.value
        _state.value = (if (current.conversation == id) AssistantState(showList = current.showList) else current)
            .copy(conversations = api.conversations())
    }

    private fun guarded(block: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                block()
            } catch (e: ApiException.Unauthorized) {
                onUnauthorized()
            } catch (e: ApiException) {
                _state.value = _state.value.copy(loadingList = false, error = e.message)
            }
        }
    }

    fun errorShown() {
        _state.value = _state.value.copy(error = null)
    }

    private fun detail(tools: List<String>, changes: List<String>): String? {
        val parts = buildList {
            if (tools.isNotEmpty()) add("${tools.size} consultation${if (tools.size > 1) "s" else ""}")
            if (changes.isNotEmpty()) {
                add("${changes.size} modification${if (changes.size > 1) "s" else ""} (annulable dans la version web)")
            }
        }
        return parts.takeIf { it.isNotEmpty() }?.joinToString(" · ")
    }
}

/** Icône de micro (tracé Material « mic »), absente des icônes de base. */
private val Mic: ImageVector = ImageVector.Builder("Micro", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(androidx.compose.ui.graphics.Color.Black)) {
        moveTo(12f, 14f)
        curveToRelative(1.66f, 0f, 2.99f, -1.34f, 2.99f, -3f)
        lineTo(15f, 5f)
        curveToRelative(0f, -1.66f, -1.34f, -3f, -3f, -3f)
        reflectiveCurveTo(9f, 3.34f, 9f, 5f)
        verticalLineToRelative(6f)
        curveToRelative(0f, 1.66f, 1.34f, 3f, 3f, 3f)
        close()
        moveTo(17.3f, 11f)
        curveToRelative(0f, 3f, -2.54f, 5.1f, -5.3f, 5.1f)
        reflectiveCurveTo(6.7f, 14f, 6.7f, 11f)
        horizontalLineTo(5f)
        curveToRelative(0f, 3.41f, 2.72f, 6.23f, 6f, 6.72f)
        verticalLineTo(21f)
        horizontalLineToRelative(2f)
        verticalLineToRelative(-3.28f)
        curveToRelative(3.28f, -0.48f, 6f, -3.3f, 6f, -6.72f)
        horizontalLineToRelative(-1.7f)
        close()
    }
}.build()

/** Punaise (« push_pin » de Material Symbols) : conversation épinglée en tête de liste. */
private val Pin: ImageVector = ImageVector.Builder("Punaise", 24.dp, 24.dp, 24f, 24f).apply {
    path(fill = SolidColor(androidx.compose.ui.graphics.Color.Black)) {
        moveTo(16f, 9f)
        verticalLineTo(4f)
        horizontalLineToRelative(1f)
        curveToRelative(0.55f, 0f, 1f, -0.45f, 1f, -1f)
        reflectiveCurveToRelative(-0.45f, -1f, -1f, -1f)
        horizontalLineTo(7f)
        curveToRelative(-0.55f, 0f, -1f, 0.45f, -1f, 1f)
        reflectiveCurveToRelative(0.45f, 1f, 1f, 1f)
        horizontalLineToRelative(1f)
        verticalLineToRelative(5f)
        curveToRelative(0f, 1.66f, -1.34f, 3f, -3f, 3f)
        verticalLineToRelative(2f)
        horizontalLineToRelative(5.97f)
        verticalLineToRelative(7f)
        lineToRelative(1f, 1f)
        lineToRelative(1f, -1f)
        verticalLineToRelative(-7f)
        horizontalLineTo(19f)
        verticalLineToRelative(-2f)
        curveToRelative(-1.66f, 0f, -3f, -1.34f, -3f, -3f)
        close()
    }
}.build()

@Composable
fun AssistantScreen(model: AssistantViewModel, snackbar: SnackbarHostState) {
    val state by model.state.collectAsStateWithLifecycle()
    var input by rememberSaveable { mutableStateOf("") }
    val list = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val dictation = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val heard = result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        if (result.resultCode == Activity.RESULT_OK && !heard.isNullOrBlank()) {
            input = if (input.isBlank()) heard else "$input $heard"
        }
    }

    LaunchedEffect(state.error) {
        state.error?.let {
            snackbar.showSnackbar(it)
            model.errorShown()
        }
    }
    LaunchedEffect(state.exchanges.size) {
        if (state.exchanges.isNotEmpty()) list.animateScrollToItem(state.exchanges.size - 1)
    }

    var renaming by remember { mutableStateOf<ConversationSummary?>(null) }
    var deleting by remember { mutableStateOf<ConversationSummary?>(null) }
    renaming?.let { target -> RenameDialog(target, onDismiss = { renaming = null }) { model.rename(target.id, it); renaming = null } }
    deleting?.let { target ->
        AlertDialog(
            onDismissRequest = { deleting = null },
            title = { Text("Supprimer la conversation ?") },
            text = { Text("« ${target.titre} » sera supprimée définitivement.") },
            confirmButton = { TextButton(onClick = { model.delete(target.id); deleting = null }, modifier = Modifier.testTag("confirmer-suppression")) { Text("Supprimer") } },
            dismissButton = { TextButton(onClick = { deleting = null }) { Text("Annuler") } },
        )
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = 16.dp, end = 4.dp, top = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                if (state.showList) "Conversations" else state.title ?: "Nouvelle conversation",
                style = MaterialTheme.typography.titleSmall,
                maxLines = 1,
                modifier = Modifier.weight(1f).testTag("titre-conversation"),
            )
            IconButton(onClick = { model.showConversations(!state.showList) }, modifier = Modifier.testTag("liste-conversations")) {
                Icon(Icons.AutoMirrored.Filled.List, contentDescription = "Conversations")
            }
            IconButton(onClick = { model.restart() }, enabled = !state.sending, modifier = Modifier.testTag("nouvelle-conversation")) {
                Icon(Icons.Filled.Add, contentDescription = "Nouvelle conversation")
            }
        }
        if (state.showList) {
            ConversationList(state, model::open, { model.pin(it.id, !it.epinglee) }, { renaming = it }, { deleting = it })
            return@Column
        }
        LazyColumn(
            state = list,
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.weight(1f).fillMaxWidth().testTag("conversation"),
        ) {
            if (state.exchanges.isEmpty()) {
                item {
                    Section("Assistant") {
                        Hint("Posez une question, au clavier ou en dictée : « Combien ai-je dépensé en restaurants ce mois-ci ? », « Quels sont mes abonnements ? », « Le loyer de septembre est-il arrivé ? »")
                        Hint("Il peut aussi classer des opérations ou gérer des règles si les modifications par l'IA sont autorisées (page Sécurité de la version web).")
                    }
                }
            }
            itemsIndexed(state.exchanges) { index, exchange -> Bubble(exchange, Modifier.testTag("message-$index")) }
        }
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it.take(2000) },
                placeholder = { Text("Votre question") },
                maxLines = 4,
                modifier = Modifier.weight(1f).testTag("question"),
            )
            IconButton(
                onClick = {
                    val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                        .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                        .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "fr-FR")
                        .putExtra(RecognizerIntent.EXTRA_PROMPT, "Votre question")
                    try {
                        dictation.launch(intent)
                    } catch (_: ActivityNotFoundException) {
                        scope.launch { snackbar.showSnackbar("Dictée indisponible sur ce téléphone.") }
                    }
                },
                enabled = !state.sending,
                modifier = Modifier.testTag("dicter"),
            ) { Icon(Mic, contentDescription = "Dicter") }
            IconButton(
                onClick = {
                    model.send(input)
                    input = ""
                },
                enabled = !state.sending && input.isNotBlank(),
                modifier = Modifier.testTag("envoyer"),
            ) { Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Envoyer") }
        }
    }
}

@Composable
private fun Bubble(exchange: Exchange, modifier: Modifier = Modifier) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (exchange.fromMe) Arrangement.End else Arrangement.Start) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            color = if (exchange.fromMe) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant,
            modifier = modifier.widthIn(max = 320.dp),
        ) {
            Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                exchange.steps.forEach { Hint("• $it") }
                when {
                    exchange.fromMe -> Text(exchange.text, style = MaterialTheme.typography.bodyMedium)
                    exchange.failed -> Text(exchange.text, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
                    exchange.text.isNotEmpty() -> MarkdownText(exchange.text)
                }
                if (exchange.pending) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        CircularProgressIndicator(Modifier.padding(2.dp).size(16.dp).testTag("reflexion"), strokeWidth = 2.dp)
                        Hint(if (exchange.text.isEmpty()) "L'assistant réfléchit…" else "L'assistant écrit…")
                    }
                }
                exchange.detail?.let { Hint(it) }
            }
        }
    }
}

@Composable
private fun ConversationList(
    state: AssistantState,
    onOpen: (Int) -> Unit,
    onPin: (ConversationSummary) -> Unit,
    onRename: (ConversationSummary) -> Unit,
    onDelete: (ConversationSummary) -> Unit,
) {
    LazyColumn(
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
        modifier = Modifier.fillMaxSize().testTag("conversations"),
    ) {
        if (state.loadingList && state.conversations.isEmpty()) item { CircularProgressIndicator() }
        if (!state.loadingList && state.conversations.isEmpty()) item { Hint("Aucune conversation mémorisée.") }
        itemsIndexed(state.conversations, key = { _, c -> c.id }) { _, conversation ->
            Surface(
                shape = RoundedCornerShape(12.dp),
                color = if (conversation.id == state.conversation) MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceVariant,
                modifier = Modifier.fillMaxWidth().clickable { onOpen(conversation.id) }.testTag("conversation-${conversation.id}"),
            ) {
                Row(Modifier.padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f).padding(vertical = 8.dp)) {
                        Text(conversation.titre, style = MaterialTheme.typography.bodyMedium, maxLines = 2)
                        Hint(instant(conversation.miseAJour))
                    }
                    IconButton(onClick = { onPin(conversation) }, modifier = Modifier.testTag("epingler-${conversation.id}")) {
                        Icon(
                            Pin,
                            contentDescription = if (conversation.epinglee) "Désépingler" else "Épingler",
                            tint = if (conversation.epinglee) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.45f),
                        )
                    }
                    IconButton(onClick = { onRename(conversation) }, modifier = Modifier.testTag("renommer-${conversation.id}")) {
                        Icon(Icons.Filled.Edit, contentDescription = "Renommer")
                    }
                    IconButton(onClick = { onDelete(conversation) }, modifier = Modifier.testTag("supprimer-${conversation.id}")) {
                        Icon(Icons.Filled.Delete, contentDescription = "Supprimer")
                    }
                }
            }
        }
    }
}

@Composable
private fun RenameDialog(target: ConversationSummary, onDismiss: () -> Unit, onConfirm: (String) -> Unit) {
    var title by rememberSaveable(target.id) { mutableStateOf(target.titre) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Renommer") },
        text = {
            OutlinedTextField(
                value = title,
                onValueChange = { title = it.take(60) },
                singleLine = true,
                modifier = Modifier.testTag("nouveau-titre"),
            )
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(title) }, enabled = title.isNotBlank(), modifier = Modifier.testTag("confirmer-titre")) {
                Text("Renommer")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Annuler") } },
    )
}
