package org.jeevanrekha.responder

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

private val JrRed = Color(0xFFB3261E)
private val JrGreen = Color(0xFF146C2E)
private val JrAmber = Color(0xFFB26A00)

/** Read-only case file: answers, triage result and the full dispatch trail. */
@Composable
fun CallDetailScreen(ref: String, onBack: () -> Unit) {
    val context = LocalContext.current
    var detail by remember { mutableStateOf<CallDetail?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(ref) {
        runCatching { Api.callDetail(context, ref) }
            .onSuccess { detail = it }
            .onFailure { error = it.message ?: "failed to load" }
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().background(JrRed).padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = onBack) { Text("← Back", color = Color.White) }
            Text(ref, color = Color.White, fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(start = 8.dp))
        }

        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(16.dp))
            return
        }
        val d = detail
        if (d == null) {
            androidx.compose.foundation.layout.Box(
                Modifier.fillMaxSize(), contentAlignment = Alignment.Center
            ) { CircularProgressIndicator() }
            return
        }

        LazyColumn(
            contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.fillMaxSize(),
        ) {
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp)) {
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically) {
                            d.tier?.let {
                                Chip(it.uppercase(), when (it) {
                                    "emergency" -> JrRed
                                    "urgent" -> JrAmber
                                    else -> JrGreen
                                })
                            }
                            Chip(d.status, Color.Gray)
                        }
                        Spacer(Modifier.height(8.dp))
                        InfoLine("Region", d.region ?: "—")
                        InfoLine("Started", d.startedAt)
                        InfoLine("Ended", d.endedAt.ifEmpty { "—" })
                        if (d.flagged.isNotEmpty()) {
                            InfoLine("Danger signs", d.flagged.joinToString(", "))
                        }
                        if (d.unclearCount > 0) {
                            InfoLine("Unclear answers", "${d.unclearCount} (escalated by design)")
                        }
                    }
                }
            }

            if (d.answers.isNotEmpty()) {
                item { Section("ANSWERS") }
                items(d.answers, key = { "ans-${it.questionId}" }) { a ->
                    Card(Modifier.fillMaxWidth()) {
                        Row(
                            Modifier.fillMaxWidth().padding(12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(a.questionId, fontWeight = FontWeight.Bold,
                                    fontSize = 13.sp)
                                Text(
                                    listOfNotNull(
                                        a.severity,
                                        if (a.ambiguous) "ambiguous -> red flag" else null,
                                        a.at,
                                    ).joinToString(" · "),
                                    fontSize = 11.sp, color = Color.Gray,
                                )
                            }
                            Chip(a.value, when (a.value) {
                                "yes" -> JrRed
                                "unclear" -> JrAmber
                                else -> JrGreen
                            })
                        }
                    }
                }
            }

            if (d.timeline.isNotEmpty()) {
                item { Section("DISPATCH TIMELINE") }
                itemsIndexed(d.timeline, key = { i, _ -> "tl-$i" }) { _, t ->
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(12.dp)) {
                            Row(horizontalArrangement = Arrangement.SpaceBetween,
                                modifier = Modifier.fillMaxWidth()) {
                                Text(t.action, fontWeight = FontWeight.Bold,
                                    fontSize = 13.sp)
                                Text(t.at, fontSize = 11.sp, color = Color.Gray)
                            }
                            Text(
                                listOfNotNull(
                                    t.track,
                                    "attempt ${t.attempt}".takeIf { t.attempt > 0 },
                                    t.contact,
                                ).filter { it.isNotEmpty() }.joinToString(" · "),
                                fontSize = 12.sp,
                            )
                            t.detail?.takeIf { it.isNotEmpty() }?.let {
                                Text(it, fontSize = 11.sp, color = Color.Gray)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun Section(text: String) {
    Text(text, fontSize = 12.sp, fontWeight = FontWeight.Bold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 6.dp))
}

@Composable
private fun InfoLine(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text("$label: ", fontSize = 13.sp, color = Color.Gray)
        Text(value, fontSize = 13.sp)
    }
}

@Composable
private fun Chip(text: String, color: Color) {
    Text(
        text,
        fontSize = 11.sp,
        color = Color.White,
        modifier = Modifier
            .background(color, RoundedCornerShape(6.dp))
            .padding(horizontal = 8.dp, vertical = 2.dp),
    )
}
