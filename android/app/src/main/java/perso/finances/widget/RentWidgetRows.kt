package perso.finances.widget

import java.time.LocalDate
import perso.finances.api.Rental
import perso.finances.format.stateLabel

/** Une ligne du widget : un loyer ou un remboursement, payé ou non pour le mois en cours. */
data class WidgetRow(val leaseId: Int, val name: String, val paid: Boolean, val state: String?) {
    /** « ☑ Studio Lyon » ou « ☐ Prêt à C. » : la case dit l'état sans couleur (charte). */
    val line: String get() = (if (paid) "☑ " else "☐ ") + name

    /** Précision sous la case : état de l'échéance du mois, ou son absence. */
    val detail: String get() = state?.let(::stateLabel) ?: "pas d'échéance ce mois-ci"
}

/** Contenu affiché par le widget : les lignes du mois, ou un message (pas appairé, injoignable…). */
sealed interface WidgetContent {
    data class Rows(val month: String, val rows: List<WidgetRow>) : WidgetContent

    data class Message(val text: String) : WidgetContent
}

object RentWidgetRows {
    fun currentMonth(today: LocalDate): String = "%04d-%02d".format(today.year, today.monthValue)

    /** `selected` : identifiants choisis à la configuration ; null = tous les loyers et remboursements. */
    fun rows(rental: Rental, selected: Set<Int>?, month: String): List<WidgetRow> =
        rental.baux
            .filter { selected == null || it.id in selected }
            .map { lease ->
                val due = lease.echeances.firstOrNull { it.mois == month }
                WidgetRow(lease.id, lease.nom, paid = due?.etat == "paye", state = due?.etat)
            }

    fun content(rental: Rental, selected: Set<Int>?, month: String): WidgetContent {
        val rows = rows(rental, selected, month)
        return if (rows.isEmpty()) {
            WidgetContent.Message(
                if (rental.baux.isEmpty()) {
                    "Aucun loyer ni remboursement suivi. Ils se créent depuis la version web."
                } else {
                    "Aucun élément choisi : touchez le widget longuement, puis Paramètres."
                },
            )
        } else {
            WidgetContent.Rows(month, rows)
        }
    }
}
