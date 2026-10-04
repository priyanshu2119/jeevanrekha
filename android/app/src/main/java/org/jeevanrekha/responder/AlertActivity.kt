package org.jeevanrekha.responder

import android.app.KeyguardManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * The full-screen emergency alert, launched from the notification's
 * fullScreenIntent: shows over the lock screen, turns the screen on, and
 * offers the two decisions that map to the exact same service calls as the
 * responder's DTMF on a voice call -- CONFIRM (help is moving) or CANNOT
 * HELP (escalate immediately). A mispress is impossible by construction:
 * there is no third action, and dismissing changes nothing server-side.
 */
class AlertActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        showOverLockScreen()

        val type = intent.getStringExtra("type") ?: "dispatch_alert"
        val ref = intent.getStringExtra("call_ref") ?: ""
        val region = intent.getStringExtra("region") ?: ""
        val eventId = intent.getStringExtra("event_id")?.toIntOrNull()

        setContent {
            AlertScreen(type = type, ref = ref, region = region,
                eventId = eventId, onDone = { finish() })
        }
    }

    private fun showOverLockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            val km = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
            km?.requestDismissKeyguard(this, null)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
            )
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }
}

private val JrRed = Color(0xFFB3261E)
private val JrGreen = Color(0xFF146C2E)

@Composable
private fun AlertScreen(
    type: String,
    ref: String,
    region: String,
    eventId: Int?,
    onDone: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }

    val title = when (type) {
        "operator_alert" -> "FOLLOW-UP REQUIRED"
        "exhausted" -> "ALL CONTACTS EXHAUSTED"
        else -> "EMERGENCY DISPATCH"
    }

    fun act(action: String) {
        if (eventId == null) {
            onDone()
            return
        }
        busy = true
        scope.launch {
            val ok = runCatching { Api.deskAction(context, eventId, action) }
                .getOrDefault(false)
            message = if (ok) "Recorded." else "Alert already resolved."
            delay(800)
            onDone()
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(JrRed)
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(title, color = Color.White, fontSize = 30.sp,
            fontWeight = FontWeight.Bold, textAlign = TextAlign.Center)
        Spacer(Modifier.height(12.dp))
        if (ref.isNotEmpty()) {
            Text(ref, color = Color.White, fontSize = 24.sp,
                fontWeight = FontWeight.Bold)
        }
        if (region.isNotEmpty()) {
            Text(region, color = Color.White, fontSize = 18.sp)
        }
        Spacer(Modifier.height(28.dp))
        message?.let {
            Text(it, color = Color.White, fontSize = 16.sp)
            Spacer(Modifier.height(12.dp))
        }

        when {
            eventId != null && type == "dispatch_alert" -> {
                Button(
                    onClick = { act("confirm") },
                    enabled = !busy,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Color.White, contentColor = JrGreen),
                    modifier = Modifier.fillMaxWidth().height(76.dp),
                ) {
                    Text("CONFIRM — I am responding", fontSize = 18.sp,
                        fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.height(14.dp))
                OutlinedButton(
                    onClick = { act("decline") },
                    enabled = !busy,
                    modifier = Modifier.fillMaxWidth().height(64.dp),
                ) {
                    Text("Cannot help right now", color = Color.White,
                        fontSize = 16.sp)
                }
            }
            eventId != null && (type == "operator_alert" || type == "exhausted") -> {
                Button(
                    onClick = { act("ack") },
                    enabled = !busy,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Color.White, contentColor = JrRed),
                    modifier = Modifier.fillMaxWidth().height(72.dp),
                ) {
                    Text("ACKNOWLEDGE — handling manually", fontSize = 16.sp,
                        fontWeight = FontWeight.Bold)
                }
            }
            else -> {
                Button(
                    onClick = onDone,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Color.White, contentColor = JrRed),
                    modifier = Modifier.fillMaxWidth().height(64.dp),
                ) {
                    Text("Close", fontSize = 16.sp)
                }
            }
        }
        Spacer(Modifier.height(10.dp))
        TextButton(onClick = onDone) {
            Text("Dismiss", color = Color.White.copy(alpha = 0.8f))
        }
    }
}
