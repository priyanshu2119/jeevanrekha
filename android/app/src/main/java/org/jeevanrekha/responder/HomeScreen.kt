package org.jeevanrekha.responder

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val JrRed = Color(0xFFB3261E)
private val JrGreen = Color(0xFF146C2E)
private val JrAmber = Color(0xFFB26A00)

private const val POLL_MS = 4_000L

/**
 * Home screen: the live picture. Foreground updates come from polling the
 * single /api/staff/overview endpoint every few seconds (one round trip --
 * deliberate for 2G/3G); when the app is backgrounded or killed, FCM
 * high-priority push is what wakes it. Everything is role-scoped server-side:
 * an ASHA only ever sees her own region.
 */
@Composable
fun HomeScreen(onOpenCall: (String) -> Unit, onLogout: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var overview by remember { mutableStateOf<Overview?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busyEvent by remember { mutableStateOf<Int?>(null) }
    var loggedOut by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        while (true) {
            val result = runCatching { Api.overview(context) }
            result.onSuccess {
                overview = it
                error = null
            }.onFailure { e ->
                if (e is Api.ApiException && e.code == 401) {
                    Session.clear(context)
                    loggedOut = true
                    return@LaunchedEffect
                }
                // Keep the last good snapshot on screen; a flaky network must
                // not blank the live view. The banner tells the truth.
                error = "Cannot reach server — showing last known state"
            }
            delay(POLL_MS)
        }
    }
    if (loggedOut) {
        onLogout()
        return
    }

    Column(Modifier.fillMaxSize()) {
        // --- header -----------------------------------------------------
        Row(
            modifier = Modifier.fillMaxWidth().background(JrRed).padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(Session.fullName(context), color = Color.White,
                    fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Text(
                    listOf(Session.role(context), Session.region(context))
                        .filter { it.isNotEmpty() }.joinToString(" · "),
                    color = Color.White.copy(alpha = 0.85f), fontSize = 12.sp,
                )
            }
            TextButton(onClick = { scope.launch {
                runCatching { Api.overview(context) }
                    .onSuccess { overview = it; error = null }
                    .onFailure { error = "Cannot reach server" }
            } }) { Text("Refresh", color = Color.White) }
            TextButton(onClick = {
                Session.clear(context)
                onLogout()
            }) { Text("Logout", color = Color.White) }
        }

        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, fontSize = 12.sp,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp))
        }

        val o = overview
        if (o == null) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            return
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (o.pendingAlerts.isNotEmpty()) {
                item { SectionTitle("ACTION NEEDED") }
                items(o.pendingAlerts, key = { "pa-${it.eventId}" }) { a ->
                    AlertCard(a, busy = busyEvent == a.eventId, onAct = { action ->
                        scope.launch {
                            busyEvent = a.eventId
                            runCatching { Api.deskAction(context, a.eventId, action) }
                            busyEvent = null
                            runCatching { Api.overview(context) }
                                .onSuccess { overview = it }
                        }
                    })
                }
            }
            if (o.operatorAlerts.isNotEmpty()) {
                item { SectionTitle("OPERATOR FOLLOW-UP") }
                items(o.operatorAlerts, key = { "oa-${it.eventId}" }) { a ->
                    OperatorCard(a, busy = busyEvent == a.eventId, onAck = {
                        scope.launch {
                            busyEvent = a.eventId
                            runCatching { Api.deskAction(context, a.eventId, "ack") }
                            busyEvent = null
                            runCatching { Api.overview(context) }
                                .onSuccess { overview = it }
                        }
                    })
                }
            }

            item { SectionTitle("LIVE CASES") }
            if (o.activeCases.isEmpty()) {
                item { EmptyNote("No active cases right now.") }
            } else {
                items(o.activeCases, key = { "ac-${it.caseId}" }) { c ->
                    CaseCard(c, onClick = { onOpenCall(c.callRef) })
                }
            }

            item { SectionTitle("RECENT CALLS") }
            if (o.recentCalls.isEmpty()) {
                item { EmptyNote("No calls yet.") }
            } else {
                items(o.recentCalls, key = { "rc-${it.ref}" }) { c ->
                    RecentRow(c, onClick = { onOpenCall(c.ref) })
                }
            }

            item {
                Text("Server time: ${o.serverTime} · updates every ${POLL_MS / 1000}s",
                    fontSize = 11.sp, color = Color.Gray,
                    modifier = Modifier.padding(top = 6.dp))
            }
        }
    }
}

// --- small building blocks --------------------------------------------------

@Composable
private fun SectionTitle(text: String) {
    Text(text, fontSize = 12.sp, fontWeight = FontWeight.Bold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 6.dp, bottom = 2.dp))
}

@Composable
private fun EmptyNote(text: String) {
    Text(text, fontSize = 13.sp, color = Color.Gray,
        modifier = Modifier.padding(vertical = 4.dp))
}

@Composable
private fun StatusChip(text: String, color: Color) {
    Text(
        text,
        fontSize = 11.sp,
        color = Color.White,
        modifier = Modifier
            .background(color, RoundedCornerShape(6.dp))
            .padding(horizontal = 8.dp, vertical = 2.dp),
    )
}

private fun trackChip(state: String): Pair<String, Color> = when (state) {
    "confirmed" -> "confirmed" to JrGreen
    "waiting" -> "waiting" to JrAmber
    "exhausted" -> "exhausted" to JrRed
    "stood_down" -> "stood down" to Color.Gray
    else -> state to Color.Gray
}

@Composable
private fun AlertCard(a: PendingAlert, busy: Boolean, onAct: (String) -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = Color(0xFFFFF4F3)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth()) {
                Text(a.callRef, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                StatusChip(
                    if (a.track == "ambulance") "Track A · ambulance" else "Track B · backup",
                    if (a.track == "ambulance") JrRed else JrAmber,
                )
            }
            Spacer(Modifier.height(4.dp))
            Text(a.contactName ?: "Unknown contact", fontSize = 14.sp)
            Text(
                listOfNotNull(
                    a.region,
                    "attempt ${a.attempt}",
                    "window closes ${a.dueAt}".takeIf { a.dueAt.isNotEmpty() },
                ).joinToString(" · "),
                fontSize = 12.sp, color = Color.Gray,
            )
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = { onAct("confirm") },
                    enabled = !busy,
                    colors = ButtonDefaults.buttonColors(containerColor = JrGreen),
                    modifier = Modifier.weight(1f).height(52.dp),
                ) {
                    Text("✓ Confirm — help is moving", fontSize = 13.sp)
                }
                OutlinedButton(
                    onClick = { onAct("decline") },
                    enabled = !busy,
                    modifier = Modifier.weight(1f).height(52.dp),
                ) {
                    Text("Cannot help", fontSize = 13.sp)
                }
            }
        }
    }
}

@Composable
private fun OperatorCard(a: OperatorAlert, busy: Boolean, onAck: () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = Color(0xFFFFF9E8)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) {
            Text("OPERATOR FOLLOW-UP · ${a.callRef}",
                fontWeight = FontWeight.Bold, fontSize = 14.sp)
            Text("${a.at} — ${a.action}: ${a.detail ?: ""}",
                fontSize = 12.sp, color = Color.Gray)
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = onAck,
                enabled = !busy,
                colors = ButtonDefaults.buttonColors(containerColor = JrRed),
                modifier = Modifier.fillMaxWidth().height(48.dp),
            ) {
                Text("Acknowledge — handling manually", fontSize = 13.sp)
            }
        }
    }
}

@Composable
private fun CaseCard(c: ActiveCase, onClick: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(14.dp)) {
            Row(horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth()) {
                Text(c.callRef, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                StatusChip(
                    if (c.status == "confirmed") "confirmed" else "active",
                    if (c.status == "confirmed") JrGreen else JrRed,
                )
            }
            Text(listOfNotNull(c.region, c.startedAt).joinToString(" · "),
                fontSize = 12.sp, color = Color.Gray)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                c.ambulance?.let { TrackChipRow("108", it) }
                c.backup?.let { TrackChipRow("backup", it) }
            }
            if (c.operatorAlerts > 0) {
                Spacer(Modifier.height(6.dp))
                Text("${c.operatorAlerts} operator alert(s)",
                    fontSize = 12.sp, color = JrRed, fontWeight = FontWeight.Bold)
            }
        }
    }
}

@Composable
private fun TrackChipRow(label: String, t: TrackState) {
    val (text, color) = trackChip(t.state)
    val suffix = when {
        t.state == "confirmed" && t.confirmedAt != null -> " ${t.confirmedAt}"
        t.state == "waiting" && t.attempts > 1 -> " (${t.attempts} attempts)"
        else -> ""
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("$label:", fontSize = 12.sp, color = Color.Gray)
        Spacer(Modifier.width(4.dp))
        StatusChip(text + suffix, color)
    }
}

@Composable
private fun RecentRow(c: RecentCall, onClick: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(c.ref, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Text(
                    listOfNotNull(c.region, c.startedAt, c.channel)
                        .joinToString(" · "),
                    fontSize = 11.sp, color = Color.Gray,
                )
            }
            c.tier?.let {
                StatusChip(it, when (it) {
                    "emergency" -> JrRed
                    "urgent" -> JrAmber
                    else -> JrGreen
                })
            }
        }
    }
}
