package org.jeevanrekha.responder

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext

class MainActivity : ComponentActivity() {

    private val notifPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Without POST_NOTIFICATIONS the emergency alert is invisible on
        // Android 13+; ask once at start, the login screen explains why.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        setContent {
            MaterialTheme { AppRoot() }
        }
    }
}

@Composable
private fun AppRoot() {
    val context = LocalContext.current
    var loggedIn by remember { mutableStateOf(Session.loggedIn(context)) }
    var openRef by remember { mutableStateOf<String?>(null) }

    when {
        !loggedIn -> LoginScreen(onLoggedIn = { loggedIn = true })
        openRef != null -> CallDetailScreen(ref = openRef!!, onBack = { openRef = null })
        else -> HomeScreen(
            onOpenCall = { openRef = it },
            onLogout = { loggedIn = false },
        )
    }
}
