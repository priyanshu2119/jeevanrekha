package org.jeevanrekha.responder

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * Two channels, deliberately unequal:
 *
 * - jr_emergency: IMPORTANCE_HIGH + alarm-ringtone + vibration + full-screen
 *   intent. This is the "call cut hote hi dikhe" path -- a dispatch alert
 *   must survive a locked, dozing phone in a pocket.
 * - jr_updates: default importance for confirmations/escalations that inform
 *   but do not need to wake anyone.
 *
 * Android 14+ grants USE_FULL_SCREEN_INTENT by default only to alarm/calling
 * style apps; the manifest declares it, Play Console must state the use, and
 * if the system downgrades us the alert still arrives as a high-priority
 * heads-up notification (the channel sound/vibration carry it).
 */
object Notifications {
    const val CHANNEL_EMERGENCY = "jr_emergency"
    const val CHANNEL_UPDATES = "jr_updates"

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return

        val alarm = NotificationChannel(
            CHANNEL_EMERGENCY,
            "Emergency dispatch alerts",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "Full-screen, alarm-style alerts for emergency dispatches"
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 800, 400, 800, 400, 800)
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        nm.createNotificationChannel(alarm)

        val updates = NotificationChannel(
            CHANNEL_UPDATES,
            "Case updates",
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "Confirmations, escalations and status changes"
        }
        nm.createNotificationChannel(updates)
    }

    fun showDispatchAlert(context: Context, data: Map<String, String>) {
        val type = data["type"] ?: "dispatch_alert"
        val ref = data["call_ref"] ?: ""
        val region = data["region"] ?: ""
        val title = when (type) {
            "operator_alert" -> "OPERATOR FOLLOW-UP REQUIRED"
            "exhausted" -> "ALL CONTACTS EXHAUSTED"
            else -> "EMERGENCY DISPATCH"
        }
        val body = buildString {
            append(ref)
            if (region.isNotEmpty()) append("  ·  ").append(region)
            data["contact"]?.takeIf { it.isNotEmpty() }?.let {
                append("  ·  ").append(it)
            }
        }

        val intent = Intent(context, AlertActivity::class.java).apply {
            putExtra("type", type)
            putExtra("call_ref", ref)
            putExtra("region", region)
            putExtra("event_id", data["event_id"] ?: "")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
        val pi = PendingIntent.getActivity(
            context,
            (ref + type).hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val n = NotificationCompat.Builder(context, CHANNEL_EMERGENCY)
            .setSmallIcon(R.drawable.ic_logo)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setFullScreenIntent(pi, true)
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        notifySafely(context, idFor(ref, type), n)
    }

    fun showUpdate(context: Context, data: Map<String, String>) {
        val type = data["type"] ?: "update"
        val ref = data["call_ref"] ?: ""
        val title = when (type) {
            "confirmed" -> "Help confirmed"
            "declined" -> "Responder declined — escalating"
            "timeout" -> "No confirmation yet — escalating"
            else -> "Case update"
        }
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val pi = PendingIntent.getActivity(
            context, ref.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val n = NotificationCompat.Builder(context, CHANNEL_UPDATES)
            .setSmallIcon(R.drawable.ic_logo)
            .setContentTitle(if (ref.isNotEmpty()) "$title · $ref" else title)
            .setContentText(data["region"] ?: "")
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        notifySafely(context, idFor(ref, type), n)
    }

    private fun notifySafely(
        context: Context,
        id: Int,
        notification: android.app.Notification,
    ) {
        try {
            NotificationManagerCompat.from(context).notify(id, notification)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS denied. The app still shows everything on
            // open; onboarding asks the user to grant it.
        }
    }

    private fun idFor(ref: String, type: String) = (ref + type).hashCode()
}
