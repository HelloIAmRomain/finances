package perso.finances.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Mêmes couleurs que l'interface web (app/web/static/app.css), clair et sombre.
val Positive = Color(0xFF1B7F3B)
val Negative = Color(0xFFB3261E)
val Warning = Color(0xFF9A6700)
private val PositiveDark = Color(0xFF6FCF8E)
private val NegativeDark = Color(0xFFF28B82)

private val Light = lightColorScheme(
    primary = Color(0xFF1F4E79),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE8F0F8),
    onPrimaryContainer = Color(0xFF1F4E79),
    background = Color(0xFFF4F6F9),
    surface = Color.White,
    error = Negative,
)

private val Dark = darkColorScheme(
    primary = Color(0xFF7FB2E5),
    onPrimary = Color(0xFF0F141A),
    primaryContainer = Color(0xFF1C2C3D),
    onPrimaryContainer = Color(0xFF7FB2E5),
    background = Color(0xFF0F141A),
    surface = Color(0xFF171E26),
    error = NegativeDark,
)

@Composable
fun FinancesTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (dark) Dark else Light, content = content)
}

@Composable
fun amountColor(cents: Long): Color = when {
    cents < 0 -> if (isSystemInDarkTheme()) NegativeDark else Negative
    cents > 0 -> if (isSystemInDarkTheme()) PositiveDark else Positive
    else -> MaterialTheme.colorScheme.onSurface
}

@Composable
fun stateColor(state: String): Color = when (state) {
    "paye" -> if (isSystemInDarkTheme()) PositiveDark else Positive
    "partiel", "en_retard" -> Warning
    "impaye" -> if (isSystemInDarkTheme()) NegativeDark else Negative
    else -> MaterialTheme.colorScheme.onSurfaceVariant
}
