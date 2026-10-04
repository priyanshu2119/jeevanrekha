package org.jeevanrekha.responder

import android.content.Context
import android.util.Log
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * FCM receiver. The backend sends DATA-ONLY high-priority messages, so
 * onMessageReceived runs even when the app is backgrounded or the device is
 * in Doze -- that is the whole point: a background socket cannot do this,
 * FCM high priority is the sanctioned wake path.
 */
class PushService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        // Re-register whenever a staff member is signed in on this device.
        if (Session.token(this) != null) {
            Thread { Api.registerDeviceBlocking(this, token) }.start()
        }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        when (data["type"]) {
            "dispatch_alert", "operator_alert", "exhausted" ->
                Notifications.showDispatchAlert(this, data)
            else ->
                Notifications.showUpdate(this, data)
        }
    }
}

/** Best-effort FCM registration after login. */
object Push {
    fun register(context: Context) {
        try {
            FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
                if (task.isSuccessful) {
                    val t = task.result
                    Thread { Api.registerDeviceBlocking(context, t) }.start()
                } else {
                    // Placeholder google-services.json or no Play services:
                    // the app still works in the foreground via polling.
                    Log.w("jr.push", "FCM token unavailable: ${task.exception?.message}")
                }
            }
        } catch (e: Exception) {
            Log.w("jr.push", "Firebase not configured: ${e.message}")
        }
    }
}
