package perso.finances.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import perso.finances.format.euros
import perso.finances.format.stateLabel

/**
 * Cadre commun d'un écran : tirer pour actualiser, chargement, erreur avec « Réessayer »,
 * message éphémère (snackbar) après une action.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun <T> Screen(
    state: Loadable<T>,
    onRefresh: () -> Unit,
    snackbar: SnackbarHostState,
    onMessageShown: () -> Unit,
    content: LazyListScope.(T) -> Unit,
) {
    LaunchedEffect(state.message) {
        state.message?.let {
            snackbar.showSnackbar(it)
            onMessageShown()
        }
    }
    val data = state.data
    PullToRefreshBox(isRefreshing = state.loading && data != null, onRefresh = onRefresh, modifier = Modifier.fillMaxSize()) {
        when {
            data != null -> LazyColumn(
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
                modifier = Modifier.fillMaxSize().testTag("contenu"),
            ) {
                state.error?.let { item { ErrorCard(it, onRefresh) } }
                content(data)
            }
            state.error != null -> Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
                ErrorCard(state.error, onRefresh)
            }
            else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(Modifier.testTag("chargement"))
            }
        }
    }
}

@Composable
fun ErrorCard(message: String, onRetry: () -> Unit) {
    Card(Modifier.fillMaxWidth().testTag("erreur")) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(message, color = MaterialTheme.colorScheme.error)
            Button(onClick = onRetry) { Text("Réessayer") }
        }
    }
}

@Composable
fun Section(title: String, modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Card(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
fun Amount(cents: Long, modifier: Modifier = Modifier, strong: Boolean = false) {
    Text(
        euros(cents),
        color = amountColor(cents),
        fontWeight = if (strong) FontWeight.SemiBold else FontWeight.Normal,
        style = if (strong) MaterialTheme.typography.headlineSmall else MaterialTheme.typography.bodyLarge,
        textAlign = TextAlign.End,
        modifier = modifier,
    )
}

@Composable
fun StateChip(state: String) {
    Surface(
        shape = RoundedCornerShape(50),
        border = BorderStroke(1.dp, stateColor(state)),
        color = MaterialTheme.colorScheme.surface,
    ) {
        Text(
            stateLabel(state),
            color = stateColor(state),
            style = MaterialTheme.typography.labelMedium,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 2.dp),
        )
    }
}

@Composable
fun LabelledRow(label: String, value: @Composable () -> Unit) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
        value()
    }
}

@Composable
fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}
