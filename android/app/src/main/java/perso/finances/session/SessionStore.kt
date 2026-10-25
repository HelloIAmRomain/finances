package perso.finances.session

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.content.edit
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Adresse du serveur et jeton d'appareil (obtenu par appairage). */
data class Session(val server: String, val token: String)

interface SessionStore {
    fun load(): Session?
    fun save(session: Session)
    fun clear()
}

/**
 * Jeton chiffré (AES-GCM) par une clé du magasin de clés Android, qui ne quitte jamais le
 * matériel du téléphone ; rien n'est sauvegardé hors du téléphone (allowBackup=false).
 */
class KeystoreSessionStore(context: Context) : SessionStore {
    private val prefs = context.getSharedPreferences("session", Context.MODE_PRIVATE)

    override fun load(): Session? {
        val server = prefs.getString(SERVER, null) ?: return null
        val sealed = prefs.getString(TOKEN, null) ?: return null
        return runCatching { Session(server, decrypt(sealed)) }.getOrNull()
    }

    override fun save(session: Session) {
        prefs.edit {
            putString(SERVER, session.server)
            putString(TOKEN, encrypt(session.token))
        }
    }

    override fun clear() {
        prefs.edit { clear() }
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (store.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
        val sealed = cipher.iv + cipher.doFinal(plain.toByteArray())
        return Base64.encodeToString(sealed, Base64.NO_WRAP)
    }

    private fun decrypt(encoded: String): String {
        val sealed = Base64.decode(encoded, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_LENGTH))
        return String(cipher.doFinal(sealed, IV_LENGTH, sealed.size - IV_LENGTH))
    }

    private companion object {
        const val PROVIDER = "AndroidKeyStore"
        const val ALIAS = "finances-session"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_LENGTH = 12
        const val SERVER = "server"
        const val TOKEN = "token"
    }
}

/** Pour les tests (le magasin de clés n'existe pas hors d'un vrai téléphone). */
class MemorySessionStore(private var session: Session? = null) : SessionStore {
    override fun load(): Session? = session
    override fun save(session: Session) {
        this.session = session
    }
    override fun clear() {
        session = null
    }
}
