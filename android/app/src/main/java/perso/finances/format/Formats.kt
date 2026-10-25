package perso.finances.format

import java.math.BigDecimal
import java.text.NumberFormat
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

// Formats français (UI-05) : montants en euros depuis des centimes entiers (inv. 2, aucun
// flottant), dates JJ/MM/AAAA, instants affichés à l'heure de Paris (inv. 9).

private val FRENCH = Locale.FRANCE
private val PARIS = ZoneId.of("Europe/Paris")
private val DAY = DateTimeFormatter.ofPattern("dd/MM/yyyy", FRENCH)
private val INSTANT = DateTimeFormatter.ofPattern("dd/MM/yyyy 'à' HH:mm", FRENCH)
private val MONTHS = listOf(
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

fun euros(cents: Long): String {
    val format = NumberFormat.getCurrencyInstance(FRENCH)
    return format.format(BigDecimal.valueOf(cents, 2))
}

fun day(iso: String?): String = iso?.let { LocalDate.parse(it.take(10)).format(DAY) } ?: "—"

fun instant(iso: String?): String =
    iso?.let { OffsetDateTime.parse(normalizeOffset(it)).atZoneSameInstant(PARIS).format(INSTANT) } ?: "jamais"

/** L'API envoie des instants UTC, parfois sans décalage explicite (« …T10:00:00 »). */
private fun normalizeOffset(value: String): String =
    if (value.endsWith("Z") || value.matches(Regex(".*[+-]\\d{2}:\\d{2}$"))) value else "${value}Z"

fun monthLabel(month: String): String {
    val (year, number) = month.split("-")
    return "${MONTHS[number.toInt() - 1]} $year"
}

fun shiftMonth(month: String, delta: Int): String {
    val (year, number) = month.split("-").map { it.toInt() }
    val index = year * 12 + (number - 1) + delta
    return "%04d-%02d".format(index / 12, index % 12 + 1)
}

fun savingsRate(perMille: Int?): String =
    perMille?.let { "${it / 10},${kotlin.math.abs(it % 10)} %" } ?: "—"

val STATE_LABELS = mapOf(
    "attendu" to "attendu",
    "partiel" to "partiel",
    "en_retard" to "en retard",
    "impaye" to "impayé",
    "paye" to "payé",
)

fun stateLabel(state: String): String = STATE_LABELS[state] ?: state
