package perso.finances.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import perso.finances.api.ApiException

/** État d'un écran : chargement, données, ou erreur lisible ; plus un message éphémère. */
data class Loadable<T>(
    val data: T? = null,
    val loading: Boolean = false,
    val error: String? = null,
    val message: String? = null,
)

/**
 * Modèle de vue générique : charge des données, exécute des actions, remonte les erreurs.
 * Un jeton refusé (appareil révoqué) déclenche `onUnauthorized` (retour à l'appairage).
 */
open class LoadViewModel<T>(
    private val fetch: suspend () -> T,
    private val onUnauthorized: () -> Unit,
) : ViewModel() {
    private val _state = MutableStateFlow(Loadable<T>(loading = true))
    val state: StateFlow<Loadable<T>> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        _state.value = _state.value.copy(loading = true, error = null)
        viewModelScope.launch {
            _state.value = try {
                Loadable(data = fetch(), message = _state.value.message)
            } catch (e: ApiException.Unauthorized) {
                onUnauthorized()
                Loadable(error = e.message)
            } catch (e: ApiException) {
                _state.value.copy(loading = false, error = e.message)
            }
        }
    }

    /** Exécute une action (confirmer, synchroniser…), affiche son message, puis recharge. */
    fun act(action: suspend () -> String) {
        viewModelScope.launch {
            val message = try {
                action()
            } catch (e: ApiException.Unauthorized) {
                onUnauthorized()
                e.message
            } catch (e: ApiException) {
                e.message
            }
            _state.value = _state.value.copy(message = message)
            refresh()
        }
    }

    fun messageShown() {
        _state.value = _state.value.copy(message = null)
    }
}
