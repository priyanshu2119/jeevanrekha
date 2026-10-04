package org.jeevanrekha.responder

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Thin HTTP client for the staff API.
 *
 * Timeouts are generous and every call is a suspend function on IO: field
 * connectivity is 2G/3G at best, and a slow request must never freeze the
 * UI or the full-screen alert.
 */
object Api {
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    class ApiException(val code: Int, message: String) : Exception(message)

    private fun base(context: Context) = Session.serverUrl(context).trimEnd('/')

    private fun execute(request: Request): JSONObject? {
        client.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw ApiException(resp.code, text.take(160).ifEmpty { "HTTP ${resp.code}" })
            }
            return if (text.isBlank()) null else JSONObject(text)
        }
    }

    private fun get(context: Context, path: String): JSONObject? {
        val b = Request.Builder().url(base(context) + path).get()
        Session.token(context)?.let { b.header("Authorization", "Bearer $it") }
        return execute(b.build())
    }

    private fun post(context: Context, path: String, body: JSONObject?): JSONObject? {
        val b = Request.Builder().url(base(context) + path)
            .post((body?.toString() ?: "{}").toRequestBody(JSON))
        Session.token(context)?.let { b.header("Authorization", "Bearer $it") }
        return execute(b.build())
    }

    suspend fun login(
        context: Context,
        server: String,
        username: String,
        password: String,
    ): UserInfo = withContext(Dispatchers.IO) {
        val url = server.trimEnd('/') + "/api/auth/login"
        val body = JSONObject().put("username", username).put("password", password)
        val req = Request.Builder().url(url)
            .post(body.toString().toRequestBody(JSON)).build()
        val json = execute(req) ?: throw ApiException(500, "empty response")
        val token = json.optString("token")
        if (token.isEmpty()) throw ApiException(500, "server returned no token")
        val user = parseUser(json.getJSONObject("user"))
        Session.saveLogin(context, server, token, user)
        user
    }

    suspend fun overview(context: Context): Overview = withContext(Dispatchers.IO) {
        parseOverview(get(context, "/api/staff/overview")
            ?: throw ApiException(500, "empty response"))
    }

    suspend fun callDetail(context: Context, ref: String): CallDetail =
        withContext(Dispatchers.IO) {
            parseDetail(get(context, "/api/staff/calls/$ref")
                ?: throw ApiException(500, "empty response"))
        }

    /** confirm | decline | ack. False means the alert was already resolved. */
    suspend fun deskAction(context: Context, eventId: Int, action: String): Boolean =
        withContext(Dispatchers.IO) {
            val json = post(context, "/api/desk/$eventId/$action", JSONObject())
            json?.optBoolean("ok") ?: false
        }

    /** Blocking on purpose: called from FCM's onNewToken on a worker thread. */
    fun registerDeviceBlocking(context: Context, fcmToken: String) {
        try {
            val body = JSONObject()
                .put("token", fcmToken)
                .put("platform", "android")
                .put("label", android.os.Build.MODEL ?: "")
            post(context, "/api/devices", body)
        } catch (_: Exception) {
            // Retried at next login / token refresh; never fatal.
        }
    }

    suspend fun registerDevice(context: Context, fcmToken: String) =
        withContext(Dispatchers.IO) { registerDeviceBlocking(context, fcmToken) }
}
