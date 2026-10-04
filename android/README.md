# JeevanRekha Responder (Android)

The staff mobile app for ASHA workers and dispatch-desk operators. When an
emergency call is triaged, the backend fans out **FCM high-priority data
messages**; this app wakes from Doze, rings a **full-screen alarm-style
alert over the lock screen**, and lets the responder **Confirm / Cannot help**
with one tap — the same two decisions as pressing 1/2 on the voice call.
In the foreground it polls `/api/staff/overview` every 4 s, so cases,
pending alerts and the operator follow-up queue stay live. Everything is
role-scoped server-side: an ASHA only ever sees her own region.

Stack: Kotlin + Jetpack Compose (Material 3), OkHttp, org.json,
EncryptedSharedPreferences (Keystore), Firebase Messaging. No WebView, no
cross-platform runtime — background reliability is the product here.

## Build

Requirements: JDK 17–21 (not 27 — AGP 8.13 does not support it yet),
Android SDK with platform 36.

```bash
cd android
echo "sdk.dir=$ANDROID_HOME" > local.properties   # or export ANDROID_HOME
JAVA_HOME=/usr/lib/jvm/java-21-openjdk ./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

Verified: `BUILD SUCCESSFUL`, APK installs as `org.jeevanrekha.responder`.

## Firebase setup (needed for push; app works without it via polling)

1. Create a Firebase project → add an **Android** app with package
   `org.jeevanrekha.responder`.
2. Download the real `google-services.json` and replace the placeholder at
   `app/google-services.json` (the placeholder builds fine; FCM token fetch
   just fails quietly and push stays off).
3. On the backend: Project settings → Service accounts → generate a key,
   then set `JR_FCM_SERVICE_ACCOUNT=/path/to/service-account.json`
   (optionally `JR_FCM_PROJECT_ID`). The backend signs FCM HTTP v1 requests
   with it directly (google-auth; no firebase-admin needed server-side).
4. Rebuild + reinstall the app, sign in — the app registers its FCM token
   via `POST /api/devices`.

## Server URL

The login screen takes the backend origin (e.g. `https://api.your-domain`).
Default is `http://10.0.2.2:8300` — the Android emulator's alias for the
host machine's localhost, matching `./run.sh`. For a physical phone on the
same LAN use the host's IP; production must be HTTPS (and then remove
`usesCleartextTraffic` from the manifest).

## Field-device setup (do this at onboarding — it decides whether alerts wake the phone)

Android 14+ and OEM skins aggressively kill background apps. For every
field phone:

1. **Allow notifications** when the app asks (Android 13+).
2. **Allow full-screen intents**: Settings → Apps → JeevanRekha Responder →
   "Alarms & reminders"/"Full-screen notifications" → Allow. (Android 14
   grants `USE_FULL_SCREEN_INTENT` by default only to alarm/calling apps;
   declare the use in Play Console and check `canUseFullScreenIntent()`
   onboarding — until granted, alerts arrive as loud heads-up
   notifications instead of full-screen.)
3. **Battery**: Settings → Battery → set this app to *Unrestricted* /
   disable optimisation.
4. **OEM autostart** (the big one on Indian-market phones):
   - Xiaomi/Redmi/Poco: Settings → Apps → Manage apps → JeevanRekha →
     *Autostart* ON; Battery saver → *No restrictions*.
   - Samsung: Settings → Battery → Background usage limits → remove the
     app from *Sleeping/Deep sleeping*; enable *Allow activity*.
   - Vivo/Oppo/Realme: iManager/Phone Manager → allow *Auto-launch* and
     background running for the app.
5. Keep the app signed in. Tokens rebind on login, so a handed-down phone
   stops alerting the previous owner automatically.

## Distribution

For pilots: sideload the APK (`adb install` or share the file) or use
**Play Console internal/closed testing** with the workers' email list —
public listing is unnecessary and brings health-app policy review. Declare
the full-screen-intent use in the Play Console permissions declaration.

## Architecture notes

- **Push, not sockets, for wake-up.** Doze/App Standby kill background
  sockets; Google's own guidance for real-time alert apps is FCM
  high-priority. Messages are data-only so `onMessageReceived` always runs
  and builds the full-screen alert itself.
- **Polling in the foreground** (4 s, single compact endpoint) instead of a
  persistent connection: simpler, battery-cheap, and resilient on 2G/3G.
  The last good snapshot stays on screen when the network drops, with an
  honest "showing last known state" banner.
- **Auth**: `POST /api/auth/login` → the same signed token the web session
  uses, stored in EncryptedSharedPreferences, sent as `Authorization:
  Bearer`. 401 anywhere → back to login.
- **Desk actions** (`/api/desk/{id}/confirm|decline|ack`) hit the same
  service functions as the voice DTMF path and the simulator desk — one
  state machine, three channels. An ASHA may act on her own region's
  alerts; operator follow-ups are operator/admin only.

## Known limitations (pilot)

- Placeholder `google-services.json` in the repo → push off until replaced
  (polling still works).
- No offline mutation queue: confirm/decline needs connectivity. The
  backend's timeout escalation is the safety net — an unanswered alert
  escalates down the chain with or without the app.
- Launcher icon is a placeholder vector; replace with a proper adaptive
  icon set before any store listing.
- English-only UI strings for now (server data carries the caller's
  language; staff UI localisation is a separate content task).
