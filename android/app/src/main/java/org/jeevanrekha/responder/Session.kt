package org.jeevanrekha.responder

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Auth token + server URL in Keystore-backed encrypted preferences.
 *
 * The token is the same signed credential the web session cookie carries;
 * on a shared or lost device it must not sit in plain storage. The server
 * URL is kept across logout so a field worker does not have to retype it.
 */
object Session {
    private const val FILE = "jr_session"
    private const val K_TOKEN = "token"
    private const val K_SERVER = "server_url"
    private const val K_NAME = "full_name"
    private const val K_ROLE = "role"
    private const val K_REGION = "region"

    // 10.0.2.2 is the host machine's loopback from the Android emulator --
    // a sane default for local development against `./run.sh`.
    private const val DEFAULT_SERVER = "http://10.0.2.2:8300"

    private var cache: SharedPreferences? = null

    private fun prefs(context: Context): SharedPreferences {
        cache?.let { return it }
        val master = MasterKey.Builder(context.applicationContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        val p = EncryptedSharedPreferences.create(
            context.applicationContext,
            FILE,
            master,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
        cache = p
        return p
    }

    fun token(context: Context): String? = prefs(context).getString(K_TOKEN, null)

    fun serverUrl(context: Context): String =
        prefs(context).getString(K_SERVER, null) ?: DEFAULT_SERVER

    fun fullName(context: Context): String = prefs(context).getString(K_NAME, null) ?: ""
    fun role(context: Context): String = prefs(context).getString(K_ROLE, null) ?: ""
    fun region(context: Context): String = prefs(context).getString(K_REGION, null) ?: ""
    fun loggedIn(context: Context): Boolean = token(context) != null

    fun saveLogin(context: Context, server: String, token: String, user: UserInfo) {
        prefs(context).edit()
            .putString(K_SERVER, server.trimEnd('/'))
            .putString(K_TOKEN, token)
            .putString(K_NAME, user.fullName)
            .putString(K_ROLE, user.role)
            .putString(K_REGION, user.region ?: "")
            .apply()
    }

    fun clear(context: Context) {
        val server = serverUrl(context)
        prefs(context).edit().clear().putString(K_SERVER, server).apply()
    }
}
