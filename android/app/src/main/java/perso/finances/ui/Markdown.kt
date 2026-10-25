package perso.finances.ui

import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp

/**
 * Markdown des réponses de l'assistant : le sous-ensemble utile (titres, paragraphes, listes,
 * tableaux, citations, code, gras, italique, barré, code en ligne). Le HTML n'est jamais
 * interprété : il s'affiche tel quel. Les liens s'affichent comme du texte, sans ouverture.
 */
sealed interface MdBlock {
    data class Heading(val level: Int, val text: String) : MdBlock
    data class Paragraph(val text: String) : MdBlock
    data class ListBlock(val ordered: Boolean, val items: List<String>) : MdBlock
    data class Table(val header: List<String>, val rows: List<List<String>>) : MdBlock
    data class Quote(val text: String) : MdBlock
    data class Code(val text: String) : MdBlock
    data object Rule : MdBlock
}

object Markdown {
    private val heading = Regex("^(#{1,6})\\s+(.*)$")
    private val bullet = Regex("^\\s*[-*+]\\s+(.*)$")
    private val numbered = Regex("^\\s*\\d+[.)]\\s+(.*)$")
    private val tableSeparator = Regex("^\\s*\\|?\\s*:?-{2,}:?\\s*(\\|\\s*:?-{2,}:?\\s*)*\\|?\\s*$")
    private val rule = Regex("^\\s*([-*_])(\\s*\\1){2,}\\s*$")

    fun parse(source: String): List<MdBlock> {
        val lines = source.replace("\r\n", "\n").lines()
        val blocks = mutableListOf<MdBlock>()
        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            when {
                line.isBlank() -> i++
                line.trimStart().startsWith("```") -> {
                    val code = mutableListOf<String>()
                    i++
                    while (i < lines.size && !lines[i].trimStart().startsWith("```")) code += lines[i++]
                    i++
                    blocks += MdBlock.Code(code.joinToString("\n"))
                }
                heading.matches(line) -> {
                    val match = heading.find(line)!!
                    blocks += MdBlock.Heading(match.groupValues[1].length, match.groupValues[2].trim().trimEnd('#').trim())
                    i++
                }
                rule.matches(line) -> { blocks += MdBlock.Rule; i++ }
                line.contains('|') && i + 1 < lines.size && tableSeparator.matches(lines[i + 1]) -> {
                    val header = cells(line)
                    i += 2
                    val rows = mutableListOf<List<String>>()
                    while (i < lines.size && lines[i].contains('|') && lines[i].isNotBlank()) rows += cells(lines[i++])
                    blocks += MdBlock.Table(header, rows.map { row -> List(header.size) { row.getOrElse(it) { "" } } })
                }
                bullet.matches(line) || numbered.matches(line) -> {
                    val ordered = numbered.matches(line)
                    val pattern = if (ordered) numbered else bullet
                    val items = mutableListOf<String>()
                    while (i < lines.size && pattern.matches(lines[i])) {
                        var item = pattern.find(lines[i])!!.groupValues[1]
                        i++
                        // Suite d'un élément sur la ligne suivante, indentée.
                        while (i < lines.size && lines[i].startsWith("  ") && !bullet.matches(lines[i]) && !numbered.matches(lines[i])) {
                            item += " " + lines[i++].trim()
                        }
                        items += item
                    }
                    blocks += MdBlock.ListBlock(ordered, items)
                }
                line.trimStart().startsWith(">") -> {
                    val quote = mutableListOf<String>()
                    while (i < lines.size && lines[i].trimStart().startsWith(">")) quote += lines[i++].trimStart().removePrefix(">").trim()
                    blocks += MdBlock.Quote(quote.joinToString(" "))
                }
                else -> {
                    val text = mutableListOf<String>()
                    while (i < lines.size && lines[i].isNotBlank() && !startsBlock(lines, i)) text += lines[i++].trim()
                    blocks += MdBlock.Paragraph(text.joinToString(" "))
                }
            }
        }
        return blocks
    }

    private fun startsBlock(lines: List<String>, i: Int): Boolean {
        val line = lines[i]
        return heading.matches(line) || bullet.matches(line) || numbered.matches(line) ||
            line.trimStart().startsWith("```") || line.trimStart().startsWith(">") || rule.matches(line) ||
            (line.contains('|') && i + 1 < lines.size && tableSeparator.matches(lines[i + 1]))
    }

    private fun cells(line: String): List<String> =
        line.trim().removePrefix("|").removeSuffix("|").split('|').map { it.trim() }

    /** Gras, italique, barré, code en ligne ; un lien devient son texte. */
    fun inline(text: String): AnnotatedString = buildAnnotatedString {
        var i = 0
        while (i < text.length) {
            val rest = text.substring(i)
            val link = Regex("^\\[([^\\]]+)]\\(([^)]*)\\)").find(rest)
            when {
                rest.startsWith("`") && rest.indexOf('`', 1) > 0 -> {
                    val end = rest.indexOf('`', 1)
                    withStyle(SpanStyle(fontFamily = FontFamily.Monospace)) { append(rest.substring(1, end)) }
                    i += end + 1
                }
                (rest.startsWith("**") || rest.startsWith("__")) && rest.indexOf(rest.take(2), 2) > 2 -> {
                    val end = rest.indexOf(rest.take(2), 2)
                    withStyle(SpanStyle(fontWeight = FontWeight.SemiBold)) { append(inline(rest.substring(2, end))) }
                    i += end + 2
                }
                rest.startsWith("~~") && rest.indexOf("~~", 2) > 2 -> {
                    val end = rest.indexOf("~~", 2)
                    withStyle(SpanStyle(textDecoration = TextDecoration.LineThrough)) { append(inline(rest.substring(2, end))) }
                    i += end + 2
                }
                (rest.startsWith("*") || rest.startsWith("_")) && rest.length > 2 && !rest[1].isWhitespace() &&
                    rest.indexOf(rest[0], 1) > 1 -> {
                    val end = rest.indexOf(rest[0], 1)
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { append(inline(rest.substring(1, end))) }
                    i += end + 1
                }
                link != null -> {
                    append(inline(link.groupValues[1]))
                    i += link.value.length
                }
                else -> {
                    append(text[i])
                    i++
                }
            }
        }
    }
}

@Composable
fun MarkdownText(source: String, modifier: Modifier = Modifier) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Markdown.parse(source).forEach { block ->
            when (block) {
                is MdBlock.Heading -> Text(
                    Markdown.inline(block.text),
                    style = if (block.level <= 2) MaterialTheme.typography.titleMedium else MaterialTheme.typography.titleSmall,
                )
                is MdBlock.Paragraph -> Text(Markdown.inline(block.text), style = MaterialTheme.typography.bodyMedium)
                is MdBlock.ListBlock -> Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    block.items.forEachIndexed { index, item ->
                        Row {
                            Text(if (block.ordered) "${index + 1}. " else "• ", style = MaterialTheme.typography.bodyMedium)
                            Text(Markdown.inline(item), style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
                is MdBlock.Table -> Column(Modifier.horizontalScroll(rememberScrollState())) {
                    TableRow(block.header, header = true)
                    HorizontalDivider()
                    block.rows.forEach { TableRow(it, header = false) }
                }
                is MdBlock.Quote -> Text(
                    Markdown.inline(block.text),
                    style = MaterialTheme.typography.bodyMedium,
                    fontStyle = FontStyle.Italic,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 8.dp),
                )
                is MdBlock.Code -> Text(
                    block.text,
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier
                        .border(1.dp, MaterialTheme.colorScheme.outlineVariant)
                        .padding(6.dp)
                        .horizontalScroll(rememberScrollState()),
                )
                MdBlock.Rule -> HorizontalDivider()
            }
        }
    }
}

@Composable
private fun TableRow(cells: List<String>, header: Boolean) {
    Row(Modifier.padding(vertical = 3.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        cells.forEach { cell ->
            Text(
                Markdown.inline(cell),
                style = MaterialTheme.typography.bodySmall,
                fontWeight = if (header) FontWeight.SemiBold else FontWeight.Normal,
                modifier = Modifier.width(110.dp),
            )
        }
    }
}
