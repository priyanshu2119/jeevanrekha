package org.jeevanrekha.responder

import org.json.JSONObject

/**
 * Wire models for the staff JSON API (app/web/staff_api.py), parsed with
 * org.json to keep the dependency footprint small -- this app has to build
 * and run on five-year-old low-end phones, not impress anyone.
 */

data class UserInfo(
    val id: Int,
    val username: String,
    val fullName: String,
    val role: String,
    val regionId: Int?,
    val region: String?,
    val language: String,
)

data class TrackState(
    val state: String,
    val attempts: Int,
    val currentContact: String?,
    val confirmedAt: String?,
)

data class ActiveCase(
    val caseId: Int,
    val callRef: String,
    val status: String,
    val region: String?,
    val startedAt: String,
    val ambulance: TrackState?,
    val backup: TrackState?,
    val operatorAlerts: Int,
)

data class PendingAlert(
    val eventId: Int,
    val caseId: Int,
    val callRef: String,
    val track: String,
    val contactName: String?,
    val contactKind: String?,
    val attempt: Int,
    val dueAt: String,
    val region: String?,
)

data class OperatorAlert(
    val eventId: Int,
    val callRef: String,
    val action: String,
    val detail: String?,
    val at: String,
)

data class RecentCall(
    val ref: String,
    val status: String,
    val channel: String,
    val region: String?,
    val startedAt: String,
    val tier: String?,
)

data class Overview(
    val serverTime: String,
    val activeCases: List<ActiveCase>,
    val pendingAlerts: List<PendingAlert>,
    val operatorAlerts: List<OperatorAlert>,
    val recentCalls: List<RecentCall>,
)

data class AnswerRow(
    val questionId: String,
    val value: String,
    val ambiguous: Boolean,
    val severity: String?,
    val at: String,
)

data class TimelineEntry(
    val at: String,
    val track: String,
    val action: String,
    val attempt: Int,
    val contact: String?,
    val detail: String?,
)

data class CallDetail(
    val ref: String,
    val status: String,
    val region: String?,
    val startedAt: String,
    val endedAt: String,
    val tier: String?,
    val flagged: List<String>,
    val unclearCount: Int,
    val answers: List<AnswerRow>,
    val timeline: List<TimelineEntry>,
)

// --- parsing helpers --------------------------------------------------------

private fun JSONObject.str(key: String): String = optString(key, "")

private fun JSONObject.strOrNull(key: String): String? =
    if (isNull(key)) null else optString(key, "").ifEmpty { null }

fun parseUser(o: JSONObject): UserInfo = UserInfo(
    id = o.optInt("id"),
    username = o.str("username"),
    fullName = o.str("full_name"),
    role = o.str("role"),
    regionId = if (o.isNull("region_id")) null else o.optInt("region_id"),
    region = o.strOrNull("region"),
    language = o.str("language"),
)

private fun parseTrack(o: JSONObject?): TrackState? {
    if (o == null || o.isNull("state")) return null
    return TrackState(
        state = o.str("state"),
        attempts = o.optInt("attempts"),
        currentContact = o.strOrNull("current_contact"),
        confirmedAt = o.strOrNull("confirmed_at"),
    )
}

fun parseOverview(o: JSONObject): Overview {
    val cases = o.optJSONArray("active_cases")?.let { arr ->
        (0 until arr.length()).map { i ->
            val c = arr.getJSONObject(i)
            ActiveCase(
                caseId = c.optInt("case_id"),
                callRef = c.str("call_ref"),
                status = c.str("status"),
                region = c.strOrNull("region"),
                startedAt = c.str("started_at"),
                ambulance = parseTrack(c.optJSONObject("ambulance")),
                backup = parseTrack(c.optJSONObject("backup")),
                operatorAlerts = c.optInt("operator_alerts"),
            )
        }
    } ?: emptyList()
    val alerts = o.optJSONArray("pending_alerts")?.let { arr ->
        (0 until arr.length()).map { i ->
            val a = arr.getJSONObject(i)
            PendingAlert(
                eventId = a.optInt("event_id"),
                caseId = a.optInt("case_id"),
                callRef = a.str("call_ref"),
                track = a.str("track"),
                contactName = a.strOrNull("contact_name"),
                contactKind = a.strOrNull("contact_kind"),
                attempt = a.optInt("attempt"),
                dueAt = a.str("due_at"),
                region = a.strOrNull("region"),
            )
        }
    } ?: emptyList()
    val ops = o.optJSONArray("operator_alerts")?.let { arr ->
        (0 until arr.length()).map { i ->
            val a = arr.getJSONObject(i)
            OperatorAlert(
                eventId = a.optInt("event_id"),
                callRef = a.str("call_ref"),
                action = a.str("action"),
                detail = a.strOrNull("detail"),
                at = a.str("at"),
            )
        }
    } ?: emptyList()
    val recent = o.optJSONArray("recent_calls")?.let { arr ->
        (0 until arr.length()).map { i ->
            val c = arr.getJSONObject(i)
            RecentCall(
                ref = c.str("ref"),
                status = c.str("status"),
                channel = c.str("channel"),
                region = c.strOrNull("region"),
                startedAt = c.str("started_at"),
                tier = c.strOrNull("tier"),
            )
        }
    } ?: emptyList()
    return Overview(
        serverTime = o.str("server_time"),
        activeCases = cases,
        pendingAlerts = alerts,
        operatorAlerts = ops,
        recentCalls = recent,
    )
}

fun parseDetail(o: JSONObject): CallDetail {
    val call = o.optJSONObject("call") ?: JSONObject()
    val result = o.optJSONObject("result")
    val flagged = result?.optJSONArray("flagged")?.let { arr ->
        (0 until arr.length()).map { arr.getString(it) }
    } ?: emptyList()
    val answers = o.optJSONArray("answers")?.let { arr ->
        (0 until arr.length()).map { i ->
            val a = arr.getJSONObject(i)
            AnswerRow(
                questionId = a.str("question_id"),
                value = a.str("value"),
                ambiguous = a.optBoolean("ambiguous"),
                severity = a.strOrNull("severity"),
                at = a.str("at"),
            )
        }
    } ?: emptyList()
    val timeline = o.optJSONArray("timeline")?.let { arr ->
        (0 until arr.length()).map { i ->
            val t = arr.getJSONObject(i)
            TimelineEntry(
                at = t.str("at"),
                track = t.str("track"),
                action = t.str("action"),
                attempt = t.optInt("attempt"),
                contact = t.strOrNull("contact"),
                detail = t.strOrNull("detail"),
            )
        }
    } ?: emptyList()
    return CallDetail(
        ref = call.str("ref"),
        status = call.str("status"),
        region = call.strOrNull("region"),
        startedAt = call.str("started_at"),
        endedAt = call.str("ended_at"),
        tier = result?.strOrNull("tier"),
        flagged = flagged,
        unclearCount = result?.optInt("unclear_count") ?: 0,
        answers = answers,
        timeline = timeline,
    )
}
