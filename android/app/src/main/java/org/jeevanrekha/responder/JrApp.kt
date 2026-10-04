package org.jeevanrekha.responder

import android.app.Application

class JrApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Channels must exist before the first FCM message arrives --
        // a notification posted to a missing channel is silently dropped.
        Notifications.ensureChannels(this)
    }
}
