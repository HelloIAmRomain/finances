package perso.finances.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.view.View
import android.widget.RemoteViews
import java.time.LocalDate
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import kotlin.concurrent.thread
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import perso.finances.MainActivity
import perso.finances.R
import perso.finances.api.ApiClient
import perso.finances.api.ApiException
import perso.finances.format.monthLabel
import perso.finances.session.KeystoreSessionStore
import perso.finances.session.SessionStore

/**
 * Widget « loyers et remboursements du mois » : une case ☑ / ☐ par élément choisi. Lecture seule
 * (inv. 1) : il interroge l'API de l'application avec le jeton de l'appareil, toutes les heures,
 * au toucher de ↻ et après un renommage dans l'application.
 */
class RentWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, appWidgetIds: IntArray) {
        val pending = goAsync()
        thread {
            try {
                runBlocking { refresh(context.applicationContext, manager, appWidgetIds) }
            } finally {
                pending.finish()
            }
        }
    }

    override fun onDeleted(context: Context, appWidgetIds: IntArray) {
        val selection = WidgetSelection(context)
        appWidgetIds.forEach(selection::remove)
    }

    companion object {
        private const val TIMEOUT_MS = 25_000L

        /** Remplaçable par les tests (le magasin de clés n'existe pas hors d'un vrai téléphone). */
        var sessionStore: (Context) -> SessionStore = { KeystoreSessionStore(it) }

        /** Demande la mise à jour de tous les widgets posés (après configuration ou renommage). */
        fun updateAll(context: Context) {
            val ids = AppWidgetManager.getInstance(context)
                .getAppWidgetIds(ComponentName(context, RentWidgetProvider::class.java))
            if (ids.isNotEmpty()) context.sendBroadcast(updateIntent(context, ids))
        }

        private fun updateIntent(context: Context, ids: IntArray): Intent =
            Intent(context, RentWidgetProvider::class.java)
                .setAction(AppWidgetManager.ACTION_APPWIDGET_UPDATE)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_IDS, ids)

        suspend fun refresh(context: Context, manager: AppWidgetManager, ids: IntArray) {
            contents(context, ids).forEach { (id, content) ->
                manager.updateAppWidget(id, views(context, id, content))
            }
        }

        /** Ce que chaque widget doit afficher : ses lignes du mois, ou la marche à suivre. */
        suspend fun contents(context: Context, ids: IntArray): Map<Int, WidgetContent> {
            val session = sessionStore(context).load()
            val rental = if (session == null) {
                null
            } else {
                runCatching {
                    withTimeoutOrNull(TIMEOUT_MS) { ApiClient(session.server, session.token).rental() }
                }
            }
            val month = RentWidgetRows.currentMonth(LocalDate.now())
            val selection = WidgetSelection(context)
            return ids.associateWith { id ->
                when {
                    session == null -> WidgetContent.Message(context.getString(R.string.widget_not_paired))
                    rental == null -> WidgetContent.Message(context.getString(R.string.widget_unreachable))
                    rental.isFailure -> WidgetContent.Message(failure(context, rental.exceptionOrNull()))
                    else -> rental.getOrNull()?.let { RentWidgetRows.content(it, selection.get(id), month) }
                        ?: WidgetContent.Message(context.getString(R.string.widget_unreachable))
                }
            }
        }

        private fun failure(context: Context, error: Throwable?): String = when (error) {
            is ApiException.Unauthorized -> context.getString(R.string.widget_not_paired)
            is ApiException -> error.message ?: context.getString(R.string.widget_unreachable)
            else -> context.getString(R.string.widget_unreachable)
        }

        /** Titre du widget : le mois affiché (« Octobre 2026 »), ou son nom s'il n'affiche qu'un message. */
        fun title(context: Context, content: WidgetContent): String = when (content) {
            is WidgetContent.Rows -> monthLabel(content.month).replaceFirstChar { it.titlecase() }
            is WidgetContent.Message -> context.getString(R.string.widget_label)
        }

        fun views(context: Context, id: Int, content: WidgetContent): RemoteViews {
            val views = RemoteViews(context.packageName, R.layout.widget_rents)
            views.removeAllViews(R.id.widget_rows)
            when (content) {
                is WidgetContent.Rows -> {
                    views.setTextViewText(R.id.widget_title, title(context, content))
                    views.setViewVisibility(R.id.widget_message, View.GONE)
                    content.rows.forEach { row ->
                        val line = RemoteViews(context.packageName, R.layout.widget_rent_row)
                        line.setTextViewText(R.id.row_name, row.line)
                        line.setTextViewText(R.id.row_state, row.detail)
                        views.addView(R.id.widget_rows, line)
                    }
                }
                is WidgetContent.Message -> {
                    views.setTextViewText(R.id.widget_title, title(context, content))
                    views.setTextViewText(R.id.widget_message, content.text)
                    views.setViewVisibility(R.id.widget_message, View.VISIBLE)
                }
            }
            val time = LocalTime.now().format(DateTimeFormatter.ofPattern("HH:mm"))
            views.setTextViewText(R.id.widget_updated, context.getString(R.string.widget_updated, time))
            views.setOnClickPendingIntent(
                R.id.widget_root,
                PendingIntent.getActivity(
                    context,
                    0,
                    Intent(context, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE,
                ),
            )
            views.setOnClickPendingIntent(
                R.id.widget_refresh,
                PendingIntent.getBroadcast(
                    context,
                    id,
                    updateIntent(context, intArrayOf(id)),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                ),
            )
            return views
        }
    }
}
