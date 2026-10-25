package perso.finances.widget

import android.content.Context
import androidx.core.content.edit

/** Éléments choisis pour chaque widget posé (un téléphone peut en avoir plusieurs). */
class WidgetSelection(context: Context) {
    private val prefs = context.getSharedPreferences("widget-loyers", Context.MODE_PRIVATE)

    /** null : widget pas encore configuré, tous les éléments sont affichés. */
    fun get(widgetId: Int): Set<Int>? =
        prefs.getStringSet(key(widgetId), null)?.mapNotNull { it.toIntOrNull() }?.toSet()

    fun save(widgetId: Int, leaseIds: Set<Int>) {
        prefs.edit { putStringSet(key(widgetId), leaseIds.map { it.toString() }.toSet()) }
    }

    fun remove(widgetId: Int) {
        prefs.edit { remove(key(widgetId)) }
    }

    private fun key(widgetId: Int) = "widget-$widgetId"
}
