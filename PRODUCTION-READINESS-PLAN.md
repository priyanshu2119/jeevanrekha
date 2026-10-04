# JeevanRekha — Production-Readiness Plan (Hinglish)

> Ye ek complete, research-backed roadmap hai is project ko "local simulator" se
> "real phone se call karo aur cheezein sach mein hon + live Android app" tak le
> jaane ka. Har section mein **kya karna hai**, **kyun** (with sources), aur
> **code mein kahan** touch karna hai — sab likha hai.
>
> Date: 2026-10-04 · Codebase: ~6,700 LOC Python/FastAPI, 64 tests passing.

---

## 0. TL;DR — Sachchai sabse pehle

**Kya already solid hai (impressive):**
- Triage rules engine (`app/engine/rules.py`) — pure, deterministic, fail-safe.
  Ye genuinely well-designed hai. Ambiguity → escalate, har call ka ek tier,
  engine exception → EMERGENCY + incident log. 64 tests isko prove karte hain.
- Dispatch state machine (`app/services/dispatch.py`) — parallel 2-track
  (ambulance + backup chain), append-only events, timeout → retry → escalate →
  operator alert → exhausted. Restart-safe (timing DB mein hai, memory mein nahi).
- Channel-agnostic call flow (`app/services/call_flow.py`) — ek hi state machine
  web, sim, aur (future) real phone sab chalate hain.
- DPDP-minded data posture, PBKDF2 auth, SSE live feed, i18n (6 languages).

**Kya production mein TODNE wala hai (critical gaps):**
1. **Real telephony abhi kaam NAHI karega.** Exotel/Twilio adapters mein real bugs
   hain (neeche Phase 2). Twilio India domestic voice ke liye usable hi nahi hai.
2. **Inbound real call kisi region se map NAHI hota** (`webhooks.py` mein
   `region_id=None`) → backup chain (Track B) hi nahi chalegi → dispatch adha.
3. **Webhook endpoints unauthenticated hain** — koi bhi internet se
   `POST /webhooks/responder-confirm` bhej ke emergency ko "confirmed" ya
   "declined" kar sakta hai. Life-safety service mein ye sabse bada security hole hai.
4. **Scheduler multi-worker mein DOUBLE fire karega.** `--workers 4` lagate hi
   4 schedulers = 4x escalations. Ye abhi documented hai par fix nahi.
5. **Koi DB migrations nahi** (sirf `create_all`) → schema change = data loss risk.
6. **Android app exist hi nahi karta**, aur **FCM push** (jo "call cut hote hi
   live dikhe" ke liye zaroori hai) backend mein nahi hai.
7. **SQLite → Postgres**, Docker, TLS, CI/CD, observability — kuch nahi hai.
8. **DLT registration / KYC / number provisioning** (TRAI legal requirement) —
   iske bina real SMS/call India mein legally nahi ho sakte.

**Realistic effort:** Ye "weekend project" nahi hai. Ek chhoti team (1 backend +
1 Android + 1 ops/part-time) ke liye **pilot-ready ≈ 6–10 weeks**, aur
**government-integrated production ≈ 4–6 months** (clinical review, DLT/KYC,
108/102 handoff MoUs, security audit included). Neeche phase-wise breakdown hai.

---

## 1. Current-State Index (maine poora codebase padha)

```
app/
  main.py            FastAPI entry, lifespan mein init_db() + scheduler.start()
  config.py          Settings (env-driven): DB, SECRET_KEY, telephony, windows
  db.py              SQLAlchemy engine, UtcDateTime, SQLite WAL pragmas, create_all
  models.py          Region, BackupContact, User, CallSession, Answer, TriageResult,
                     DispatchCase, DispatchEvent (append-only), StatusMessage, IncidentLog
  security.py        PBKDF2 (240k iters) + itsdangerous signed session cookie
  engine/
    questions.py     Fixed maternal(10)/newborn(9) danger-sign questions, red/amber
    rules.py         PURE evaluate() → emergency|urgent|reassurance + guidance
    i18n.py          6 language packs, digit→lang map, fallback-to-en + missing_keys
    lang/{en,hi,mr,bn,ta,te}.py
  services/
    call_flow.py     State machine (language→track→questions→result), handle_digit/
                     handle_silence/hangup/finalise + idle watchdog
    dispatch.py      start_dispatch (2 tracks), _place_alert, confirm/decline/cancel,
                     process_timeouts, _escalate_track, _operator_alert, exhausted
    scheduler.py     In-process asyncio loop (2s tick) → process_timeouts + watchdog
    telephony.py     SimulatedProvider (works) | ExotelProvider | TwilioProvider (buggy)
  web/
    public.py        Landing, /triage, /api/flow/* , SSE /api/flow/{ref}/events
    sim.py           /sim phone simulator + dispatch desk (/api/sim/desk/*)
    staff.py         /login, /asha, /admin, /admin/calls, /admin/regions
    webhooks.py      /webhooks/exotel/inbound, /twilio/inbound, /responder-confirm
    serializers.py   flow_snapshot, dispatch_summary, status_messages
    deps.py          templates, request_user, mask_phone
  templates/  static/{css,js}  (vanilla JS, no build step)
scripts/seed.py      3 regions, staff, 14-day engine-computed history
tests/               64 tests (engine, flow, dispatch, api, i18n)
```

**Verdict:** Architecture *soch* production-grade hai (safety-first, auditable,
deterministic). Lekin **integration layer** (real telephony, push, deploy,
security, compliance) abhi simulated/adhura hai. Neeche usi ko banayenge.

---

## 2. "Main phone se call karoon aur cheezein sach mein hon" — real flow kya chahiye

Aaj ka flow (simulated): `/sim` pe click → browser se `/api/flow/*` → state
machine → dispatch desk (browser) pe confirm. **PSTN involve hi nahi hota.**

Real flow jo chahiye:

```
Caller ka mobile ──dial──► Exotel virtual number (e.g. 080-XXXX-XXXX / 14XXX)
        │
        ▼
Exotel ──HTTP webhook──► JeevanRekha backend (/webhooks/exotel/inbound)
        │  backend ExoML <Gather><Say>…</Say></Gather> return karta hai
        ▼
Caller sunta hai TTS (Hindi/English/…) aur DTMF 1/2/3 press karta hai
        │  har press → webhook → call_flow.handle_digit() → agla ExoML
        ▼
Last answer → rules.evaluate() → EMERGENCY?
        │ haan → dispatch.start_dispatch()
        ├── Track A: Exotel se 108/ambulance node ko outbound call (TTS + press-1)
        └── Track B: backup chain (ASHA→PHC→transport→district) ko outbound calls
        │
        ▼  (SIMULTANEOUSLY)
Responder ka phone ring → "press 1 to confirm" → Exotel webhook → dispatch.confirm()
        │
        ├── FCM high-priority push ──► ASHA-di ka Android app (full-screen alert)
        └── FCM push ──► Operator ka Android app (live case)
        │
        ▼
Caller ko live status (SSE/IVR playback): "requested, NOT yet confirmed" →
"ambulance confirmed at HH:MM IST" (honest, kabhi jhooth nahi)
```

Is poore chain ko banane ke liye 5 phases hain. Chalo ek-ek karke.

---

## 3. PHASE 1 — Backend Production Hardening (Week 1–2)

Ye sabse pehle karo, kyunki baaki sab isi par tikta hai.

### 1.1 PostgreSQL + Alembic migrations
- **Kyun:** SQLite-WAL single-writer hai; multi-worker + concurrent dispatch mein
  lock contention aayega. Production ke liye Postgres. Code already portable hai
  (`JR_DATABASE_URL`, `UtcDateTime` Postgres pe `timestamptz` rehta hai).
- **Kya karo:**
  - `requirements.txt` mein add: `psycopg[binary]>=3.1`, `alembic>=1.13`,
    `sqlalchemy` already hai.
  - Alembic init: `alembic init alembic`, `sqlalchemy.url` ko
    `settings.DATABASE_URL` se drive karo (env.py mein `from app.db import Base`
    + `target_metadata = Base.metadata`).
  - Pehla migration: `alembic revision --autogenerate -m "initial schema"`.
  - `app/db.py:init_db()` ko **sirf dev/test** ke liye rakho; production mein
    startup pe `alembic upgrade head` chalao (Docker entrypoint ya init-container).
  - **Indexes** jo abhi missing hain aur production mein chahiye:
    `DispatchEvent(action, resolved, due_at)` composite (scheduler ki timeout
    query har 2s chalti hai — `process_timeouts` dekho), aur
    `CallSession(region_id, started_at)` (ASHA/admin dashboards).
- **Files:** `app/db.py`, `app/config.py`, `requirements.txt`, naya `alembic/`.

### 1.2 Scheduler ko multi-worker-safe banao (CRITICAL)
- **Problem:** `app/main.py` lifespan mein `scheduler.start()` har Uvicorn worker
  mein chalta hai. `fastapi run --workers 4` = 4 schedulers = har timeout 4 baar
  process = **double/quadruple escalation, duplicate outbound calls**. Life-safety
  mein ye dangerous hai.
- **3 options (koi ek chuno):**
  1. **Dedicated scheduler process** (sabse simple, recommended for pilot):
     Web workers mein scheduler OFF (`JR_RUN_SCHEDULER=false`), aur ek alag
     container/process sirf scheduler chalaye (`python -m app.services.scheduler_worker`).
     `main.py` mein `if settings.RUN_SCHEDULER: scheduler.start()`.
  2. **Postgres advisory lock**: `process_timeouts` ke around
     `SELECT pg_try_advisory_lock(<key>)` — sirf ek worker lock jeetega. Sasta
     multi-worker safety, par thoda subtle.
  3. **Celery beat / APScheduler + Redis lock**: heavy, scale ke liye. Pilot ke
     liye overkill.
- **Mera recommendation:** Pilot mein **option 1** (dedicated worker). Scale pe
  option 2/3. README already ye kehta hai — ab implement karo.
- **Files:** `app/main.py`, `app/services/scheduler.py`, `app/config.py`
  (naya `RUN_SCHEDULER` flag), naya `app/services/scheduler_worker.py`.

### 1.3 Security hardening (life-safety ke liye non-negotiable)
- **Webhook authentication (SABSE ZAROOri):** Abhi `/webhooks/*` khol ke hain.
  - Exotel/Twilio request **signature verify** karo (Twilio: `X-Twilio-Signature`
    HMAC-SHA256; Exotel: request mein account-specific token / IP allowlist).
  - Ya kam se kam ek **shared secret header** (`X-JR-Webhook-Secret`) jo provider
    ke webhook config mein set ho, aur backend verify kare. Bina iske koi bhi
    `responder-confirm` bhej ke emergency band kar sakta hai.
  - **IP allowlist** middleware: Exotel/Twilio ke published IP ranges se hi
    `/webhooks/*` accept karo.
- **SECRET_KEY:** `config.py` ka default `"dev-only-insecure-key-change-me"`
  production mein **hard-fail** hona chahiye (startup pe check: agar default hai
  aur `JR_ENV=production` → raise). Generate: `python -c "import secrets;print(secrets.token_hex(32))"`.
- **HTTPS enforcement:** Cookie `secure=True` jab production ho
  (`staff.py:set_cookie` mein abhi `httponly=True, samesite="lax"` hai — `secure`
  add karo). Reverse proxy (Traefik/nginx) TLS terminate kare; app `--proxy-headers`
  ke saath chale.
- **Rate limiting:** `slowapi` ya nginx-level, especially `/api/flow/start`,
  `/login`, `/webhooks/*` pe (brute-force + abuse rokne ke liye).
- **CSRF:** Login form (`POST /login`) cookie-based hai → CSRF token add karo
  (ya `SameSite=Strict`). Staff admin actions (region config POST) bhi.
- **CORS:** Agar Android app alag origin se API call kare to explicit
  `CORSMiddleware` (abhi koi CORS nahi — native app ke liye theek hai, par web
  companion ke liye socho).
- **Input validation:** `/api/flow/{ref}/digit` pe digit whitelist (`1/2/3`),
  region_id bounds, phone normalization.
- **Secrets management:** `.env` ko git mein mat daalo; production mein
  cloud secret manager (AWS SSM/Secrets Manager, GCP Secret Manager) ya Docker
  secrets. `.env.example` already hai — accha.
- **Files:** `app/web/webhooks.py`, `app/web/staff.py`, `app/config.py`,
  `app/main.py` (middleware), naya `app/web/security_mw.py`.

### 1.4 Inbound region resolution (CRITICAL functional gap)
- **Problem:** `webhooks.py` inbound call pe `region_id=None` se session banata
  hai. Iska matlab real caller ke liye **Track B backup chain hi configure nahi
  hogi** (`_backup_chain(db, None)` → `[]`), sirf Track A (108) chalega, aur wo
  bhi generic. Ye "parallel routing" ke core promise ko todta hai.
- **Solutions (deployment ke hisaab se):**
  1. **Number-plan mapping:** Har region/block ko ek alag Exotel virtual number
     (ya DID) do. Inbound webhook mein `CallTo` (jis number pe call aaya) se
     region resolve karo. Ye sabse reliable hai — caller ko kuch bolna nahi padta.
     Naya table: `RegionPhoneNumber(region_id, provider_number)`.
  2. **Caller-number prefix mapping:** `From` ke STD/mobile prefix se region
     guess karo (kam reliable — mobile portability).
  3. **IVR mein area code poochho:** Pehle DTMF se block/area chunwao
     (caller-friendly nahi, par fallback). README bhi ye mention karta hai.
  - **Recommendation:** Pilot mein **option 1** (per-region DID) + option 3
    fallback. Ek single national helpline number chahiye to IVR mein pehle
    "apne block ka code chunein" (option 3) ya caller ke registered phone se
    auto-map (agar ASHA ne pehle register kiya ho).
- **Files:** `app/web/webhooks.py`, `app/models.py` (naya table),
  `app/services/call_flow.py` (start_call mein region pass), `scripts/seed.py`.

### 1.5 Observability (life-safety mein "pata hona" zaroori hai)
- **Health checks:** `GET /healthz` (liveness) + `GET /readyz` (DB reachable,
  scheduler alive). Docker/K8s probes isi ko use karein.
- **Structured logging:** Abhi `logging.basicConfig` hai. JSON logs
  (`structlog` ya `python-json-logger`) + request-id correlation. Har call ke
  `ref_code` ko log mein tag karo (already partially hai).
- **Error tracking:** **Sentry** (`sentry-sdk[fastapi]`) — fail-safe activations,
  engine exceptions, provider failures, scheduler crashes turant dikhein.
  `IncidentLog` table already hai — usko Sentry se cross-link karo.
- **Metrics:** Prometheus `/metrics` (call volume, tier distribution, dispatch
  confirm-rate, escalation count, provider latency, scheduler lag). Admin
  dashboard already "confirmed-within-window vs escalated vs exhausted" dikhata
  hai — usko Prometheus mein bhi export karo for alerting.
- **Alerting:** "dispatch exhausted" ya "operator_alert" fire ho to on-call ko
  page (PagerDuty/Opsgenie/WhatsApp Business API). Ye *business* alert hai,
  sirf infra nahi.
- **Files:** `app/main.py`, naya `app/web/health.py`, `app/web/metrics.py`,
  `requirements.txt`.

### 1.6 Docker + deployment artifacts
- **Dockerfile** (official FastAPI pattern, `python:3.11-slim`, exec-form CMD,
  `--proxy-headers`):
  - `tiangolo/uvicorn-gunicorn-fastapi` base image **deprecated** hai — apna
    image scratch se banao. (Source: fastapi.tiangolo.com/deployment/docker/)
  - Requirements pehle COPY (layer cache), phir app.
  - Entrypoint: `alembic upgrade head` → `fastapi run app/main.py --port 80 --workers N`.
- **docker-compose.yml** (pilot): `web` (N workers, scheduler OFF),
  `scheduler` (1 process, scheduler ON), `postgres`, `traefik` (TLS via
  Let's Encrypt), optional `redis` (agar Celery/rate-limit).
- **Run:** `fastapi run app/main.py --port 80 --workers 4` (ya
  `uvicorn app.main:app --host 0.0.0.0 --port 80 --workers 4`).
  K8s pe: 1 process/container, replicate at cluster level (workers mat lagao).
- **CI/CD:** GitHub Actions — lint (`ruff`), type-check (`mypy`/`pyright`),
  `pytest` (64 tests), build image, push to registry, deploy (SSH/K8s/Cloud Run).
- **Files:** naye `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`,
  `entrypoint.sh`.

### 1.7 Hosting (India data-residency + latency)
- **Kyun India region:** DPDP + health data localization expectations, aur
  telephony media-anchoring (Plivo/Exotel dono legs India mein chahte hain).
  Caller→backend→provider latency bhi kam rahegi.
- **Options:** AWS Mumbai (ap-south-1), GCP Mumbai (asia-south1), Azure India,
  ya Indian cloud (Netmagic/Yotta/E2E). Pilot ke liye ek single VM (4 vCPU/8GB)
  + managed Postgres (AWS RDS/Cloud SQL) kaafi hai.
- **Note:** Exotel ka data center bhi India mein hai — webhook round-trip fast
  hoga (<3s target, Exotel Passthru timeout 15s default).

---

## 4. PHASE 2 — Real Telephony: "phone se call" (Week 2–4)

Ye sabse important + sabse zyada research wala phase hai.

### 4.1 Provider decision: **Exotel** (Twilio India domestic ke liye NAHI)
- **Twilio India reality check:** Twilio ke apne India voice guidelines mein
  **Domestic Inbound = N/A, Domestic Outbound = N/A**. Toll-free `+91 800`
  numbers "must be answered outside of India" aur "Not for outbound use".
  Matlab **Twilio se ek Indian caller ko Indian number pe domestic IVR call
  practically nahi chal sakti.** Twilio international/US-number use cases ke liye
  theek hai, domestic Indian helpline ke liye nahi.
  - Source: https://www.twilio.com/en-us/guidelines/in/voice
  - Source: https://help.twilio.com/articles/115007579027-Toll-free-Phone-Number-Restrictions-and-Limitations
- **Exotel = India-first CPaaS, sahi choice.** Teen API families:
  1. **Voice V1 (REST)** — outbound trigger (`/Calls/connect`), CDR.
  2. **ExoML (event-driven webhooks, pure code)** — *dynamic IVR in code*,
     `<Gather>`/`<Say>`/`<Dial>` XML verbs, leg-level control. **Yahi JeevanRekha
     ke inbound IVR ke liye chahiye** (kyunki humara flow DB-driven dynamic hai).
  3. **LeadAssist** — number masking (agar caller↔ASHA ko numbers chhupane hon).
  - Source: https://docs.exotel.com/voice-apis
  - Source: https://developer.exotel.com/docs/voice-v1/api-reference/incoming-call
- **Alternatives:** Plivo (India-registered business + KYC + media-anchoring
  ke saath domestic inbound/outbound karta hai; XML `<GetDigits>`/`<Speak>`),
  Gupshup, MSG91, Ozonetel, Knowlarity. **Plivo strong #2 hai** agar Exotel
  pricing/features suit na kare. Par Exotel ka India footprint + ExoML + App
  Bazaar + 108-style IVR experience isko default banata hai.
  - Source: https://www.plivo.com/docs/voice/concepts/india-calling

### 4.2 Exotel inbound IVR — CORRECT architecture (current code fix)
Do tarike hain; humare dynamic flow ke liye **ExoML** best hai:

**Tarika A — ExoML (recommended, code-driven):**
- Exotel dashboard mein ExoPhone ka call-flow ek **webhook URL** pe set karo
  (e.g. `https://api.jeevanrekha.in/webhooks/exotel/inbound`).
- Har inbound event pe Exotel tumhare backend ko POST karta hai; backend
  **ExoML XML** return karta hai. DTMF lene ke liye `<Gather>` zaroori hai:
  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <Response>
    <Gather action="https://api.jeevanrekha.in/webhooks/exotel/inbound"
            method="POST" timeout="7" numDigits="1" finishOnKey="#">
      <Say language="hi-IN" voice="woman">
        JeevanRekha mein aapka swagat hai. Hindi ke liye 1 dabayein…
      </Say>
    </Gather>
    <!-- fallback if no input -->
    <Redirect>https://api.jeevanrekha.in/webhooks/exotel/silence</Redirect>
  </Response>
  ```
  Caller jab `1` press karta hai, Exotel `Digits=1` ke saath `action` URL pe
  POST karta hai → `call_flow.handle_digit()` → agla `<Gather>` (track/question).
  - Source (ExoML verbs `<Gather>`,`<Say>`,`<Dial>`): https://docs.exotel.com/voice-apis
  - **Exact attribute names** (timeout/numDigits/finishOnKey/action/method) ko
    current ExoML reference se confirm karo — Exotel ne docs restructure kiye
    hain (`developer.exotel.com`). Plivo ka `<GetDigits action=… numDigits=…
    timeout=…><Speak>…</Speak></GetDigits>` same concept hai.
    - Source: https://www.plivo.com/docs/voice/xml/overview

**Tarika B — App Bazaar visual flow + Passthru applet (hybrid):**
- Static IVR menu dashboard mein banao, aur **Passthru applet** se dynamic
  decision ke liye backend ko HTTP call karo. Passthru tumhe bhejta hai:
  `CallSid, CallFrom, CallTo, Direction, CurrentTime, DialWhomNumber` (POST
  form-encoded). Tumhara server **plain-text response** deta hai = next applet
  ka naam ya ExoML URL (e.g. `https://my.exotel.com/exoml/start/<flow>`).
  - Requirements: HTTPS (TLS 1.2+), publicly accessible, respond < 3s (default
    timeout 15s), Exotel IP ranges validate karo.
  - Source: https://developer.exotel.com/docs/app-bazaar/passthru-applet-guide
  - Source: https://developer.exotel.com/docs/call-support/call-features/ivr-setup

**Current code mein kya galat hai (`app/web/webhooks.py` + `telephony.py`):**
1. Inbound webhook `<Response><Say>…</Say></Response>` return karta hai —
   **`<Gather>` missing**, isliye caller ke DTMF presses kabhi wapas nahi aayenge.
   Fix: har prompt ko `<Gather>` ke andar wrap karo (upar wala pattern).
2. `ExotelProvider.place_dispatch_alert` mein `"Url": message` — ye **TTS text
   ko URL ki jagah pass kar raha hai**. Exotel `Calls/connect` ka `Url` ek
   **ExoML endpoint** hona chahiye jo `<Say>` + `<Gather>` return kare.
   - Source: https://developer.exotel.com/api/make-a-call-api
3. `From`/`CallerId` ke liye `settings.TWILIO_FROM` use ho raha hai Exotel
   provider mein — alag `JR_EXOTEL_FROM`/ExoPhone config banao.
4. Auth: Exotel `-u <api_key>:<api_token>` (HTTP basic) use karta hai —
   `httpx.post(..., auth=(sid, token))` theek hai, par base URL
   `https://<subdomain>.exotel.com/v1/Accounts/<sid>` confirm karo.

**Plan:** `telephony.py` ke Exotel adapter ko rewrite karo:
- `place_dispatch_alert` → `POST /Calls/connect` with `From=<ExoPhone>`,
  `To=<responder>`, `Url=<backend>/webhooks/exotel/responder-alert?event_id=<id>`.
- Naya endpoint `/webhooks/exotel/responder-alert` → ExoML return kare:
  `<Gather action="…/responder-confirm?event_id=<id>" numDigits="1"><Say>
  JeevanRekha emergency… press 1 to confirm</Say></Gather>`.
- Inbound `/webhooks/exotel/inbound` → ExoML `<Gather>` per flow step
  (`call_flow.opening_prompts` ke text ko `<Say>` mein daalo).
- **Region resolve** karo `CallTo` (DID) se — Phase 1.4.
- **Signature/IP verify** karo — Phase 1.3.

### 4.3 Outbound responder alert (Track A + B) — "press 1 to confirm"
- `dispatch._place_alert` already provider call karta hai. Exotel pe:
  `Calls/connect` se responder ko call lagao, `Url` ek ExoML endpoint pe point
  karo jo `<Say>` (TTS message) + `<Gather numDigits="1">` (confirm) play kare.
  Responder `1` press kare → Exotel `Digits=1` ke saath `responder-confirm`
  webhook pe POST kare → `dispatch.confirm()`. `2`/koi aur → `decline()`.
- **SMS fallback:** `dispatch.py` already `send_sms` karta hai. Exotel SMS API
  (`/Sms/send`) — par **DLT-registered template + header** zaroori (Phase 4).
- **Important nuance:** Track A aksar **108** hota hai (government ambulance).
  108 ek real human-operated emergency line hai — Exotel se 108 dial karke
  "press 1 to confirm" expect karna realistic NAHI (108 IVR aapke DTMF ko
  confirm nahi karega). Isliye Track A ke liye better model:
  - **(a)** 108 ko call lagao aur **operator/ASHA ko app pe notify karo** ki
    "108 dialed, ab aap follow-up karo" (human-in-loop), ya
  - **(b)** Seedha **ambulance node / PHC duty room** ko outbound alert bhejo
    (jo press-1 confirm kar sake), aur 108 ko *parallel* inform karo.
  - Ye real-world 108/102 integration ka sabse practical pattern hai. Neeche 4.6.

### 4.4 Number provisioning + KYC (real number kaise milega)
- **Eligibility:** Sirf **India-registered business** (Pvt Ltd / LLP / registered
  entity) Indian virtual numbers rent kar sakta hai + domestic calls kar sakta
  hai. KYC documents (CIN/GST/PAN, address proof, authorized signatory) lagenge.
  - Source: https://www.plivo.com/docs/voice/concepts/india-calling
- **TRAI number series (galat series = violation):**
  | Series | Use | JeevanRekha ke liye |
  |---|---|---|
  | **140** | Promotional voice ONLY | ❌ nahi |
  | **Landline (022/080/075…)** | **Service + Transactional** calls | ✅ inbound helpline + responder alerts |
  | **160** | BFSI transactional | ❌ nahi |
  - Humara use-case **service/transactional** hai (emergency response, existing
    relationship with ASHA/PHC) → **landline-series virtual number** chahiye,
    promotional 140 nahi.
  - Source: https://www.plivo.com/docs/voice/concepts/india-calling
- **Toll-free (1800):** Possible, par "must be answered within India" aur
  provisioning mein time lagta hai. Pilot ke liye ek **landline-series DID per
  region** zyada practical hai (aur region-resolution bhi solve karta hai — 4.2/1.4).
- **Reality:** Ek startup ko number mil sakta hai (KYC ke baad), par
  **"emergency helpline" jaisa 108/102 number government allocate karta hai.**
  Private taur par tum 108 ko *replace* nahi kar sakte — sirf *augment/handoff*
  kar sakte ho (4.6).

### 4.5 DLT registration (TRAI) — SMS/calls ke liye legally zaroori
- **Kya hai:** TRAI ke TCCCPR-2018 ke under, India mein SMS (aur telemarketing
  calls) bhejne ke liye **DLT platform pe register** hona mandatory hai:
  - **Principal Entity (PE)** registration (tumhari company).
  - **Header/Sender-ID** registration (e.g. `JRKHA`).
  - **Content Template** registration (har SMS template pre-approved).
  - **Consent Template** (agar commercial ho).
  - Source: https://trai.gov.in/advice-to-senders
  - Source: https://www.infobip.com/docs/essentials/asia-registration/dlt-registration
- **Transactional vs Promotional:** Emergency/service SMS **transactional**
  category mein aate hain (OTP/alerts jaise) — inhe explicit consent ki zaroorat
  nahi, par **DLT registration phir bhi chahiye** (header + template). Promotional
  ke liye consent mandatory.
- **Process:** Provider (Exotel/Plivo/Jio/Airtel/Vi DLT portal) ke through PE
  register karo → headers + templates submit karo → approval (kuch din) → phir
  API se SMS bhejo. Exotel/Plivo isme guide karte hain.
- **Action item:** Backend mein SMS bhejne se pehle **DLT-approved template ID +
  header** use karo (hardcoded free-text SMS production mein block ho jayega).
  `telephony.send_sms` ko template-based banao.

### 4.6 108 / 102 / government integration (the honest part)
- **108** = national emergency ambulance (medical emergencies). **102** =
  maternal & newborn transport (MAA/MNH under MoHFW, pregnant women + sick
  neonates). **104** = health information/advice. **14416** = tele-MANAS (mental
  health). Ye sab **government-run** hain.
- **Legal/practical:** Ek private app **108 ko impersonate nahi kar sakta** aur
  na hi uska "official" number le sakta hai. Tumhara model hona chahiye:
  - Caller tumhare helpline pe call kare → tum triage karo → **Track A: 108/102
    ko dial karo** (ya unke control room ko alert bhejo) **aur** Track B: local
    ASHA/PHC chain ko alert karo → dono ka status caller + app pe live dikhao.
  - Ya **government ke saath MoU/integration**: seedha 108 CAD (Computer-Aided
    Dispatch) system ko structured alert bhejo (API/WhatsApp/email), bajaye
    sirf phone call ke. Ye pilot ke baad ka step hai.
- **Clinical governance:** Danger-sign questions aur guidance text MoHFW/IMNCI/WHO
  material se liye gaye hain (README mein cite hai) — par **field use se pehle
  native speakers + health educators + ideally a medical advisory board se review
  karwao**. Ye README ki "honest limitations" mein bhi hai. Life-safety mein
  content review non-negotiable hai.

### 4.7 Telephony testing (real calls)
- Exotel sandbox/test credentials se shuru karo, phir ek real DID pe pilot.
- **End-to-end test script:** apne phone se DID dial karo → Hindi chuno →
  maternal → bleeding=1 → emergency → apne doosre phone (responder) pe outbound
  call aaye → press 1 → app pe confirm dikhe → caller ko "confirmed" status.
- `tests/` mein provider adapter ke **mocked HTTP tests** add karo (httpx mock)
  taaki ExoML generation + webhook parsing regress na ho.

---

## 5. PHASE 3 — Android App: ASHA-di + Operator, "call cut hote hi LIVE" (Week 3–7)

User ki core demand: app pe ASHA + operator login ho, aur **call khatam hote hi
turant** sab live dikhe. Iske liye sirf SSE kaafi NAHI (app background/killed
mein SSE mar jaata hai). **FCM high-priority push** zaroori hai.

### 5.1 Real-time delivery: FCM high-priority data message (the ONLY reliable way)
- **Kyun FCM, WebSocket/SSE nahi:** Android **Doze mode + App Standby Buckets +
  background execution limits** (Android 8+) kisi bhi background socket/SSE ko
  maar dete hain jab app killed/background ho. Google khud kehta hai: real-time
  alerts ke liye **FCM high-priority messages** use karo — ye sleeping device ko
  **wake** kar sakte hain (limited processing + network). Instant-messaging/calling
  apps ke liye battery-optimization exemption "Not Acceptable" — FCM high-priority
  hi sanctioned path hai.
  - Source: https://firebase.google.com/docs/cloud-messaging/android/message-priority
  - Source: https://developer.android.com/training/monitoring-device-state/doze-standby
  - Source: https://firebase.blog/posts/2025/04/fcm-on-android/
- **Data message vs Notification message:** **Data-only high-priority message**
  bhejo (`"data": {...}`, no `"notification"` block) taaki app ka
  `onMessageReceived` hamesha chale (even background mein) aur tum **full-screen
  intent** notification khud bana sako. Notification-message sirf system tray
  dikhata hai, app code nahi chalata.
- **Backend se FCM bhejna (Python/FastAPI):**
  - **Firebase Admin SDK** (`pip install firebase-admin`) — legacy FCM API
    **deprecated** hai, **HTTP v1** use hota hai (service-account JSON +
    OAuth2 access token).
    ```python
    import firebase_admin
    from firebase_admin import credentials, messaging
    firebase_admin.initialize_app(credentials.Certificate("service-account.json"))
    msg = messaging.Message(
        data={"type": "dispatch_alert", "case_id": "123", "call_ref": "JR-4821",
              "tier": "emergency", "region": "Ashta, Sehore"},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="jr_emergency", sound="alarm",
                default_vibrate_timings=[0, 500, 300, 500]),
        ),
        token=device_fcm_token,
    )
    messaging.send(msg)
    ```
  - Source: https://firebase.google.com/docs/cloud-messaging/send/admin-sdk
  - Source: https://firebase.google.com/docs/cloud-messaging/send/v1-api
- **Kahan trigger karo:** `dispatch.py` ke `_place_alert`, `confirm`, `decline`,
  `_operator_alert`, `_maybe_mark_exhausted` mein — jab bhi case state change ho,
  relevant region ke ASHA + sab operators ko FCM bhejo. Ek naya
  `app/services/push.py` banao (FCM sender) + `DeviceToken` model (user↔token).

### 5.2 Full-screen emergency alert (lock screen pe alarm jaisa)
- **Android 14+ FSI restrictions (IMPORTANT):** `USE_FULL_SCREEN_INTENT` ab
  **sirf alarm/calling apps ko by-default** milta hai. Baaki apps ko permission
  grant nahi hoti by default → user ko manually dena padta hai
  (`ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT`), aur **Play Console mein declare**
  karna padta hai. FSI ab **mainly locked screen pe hi** reliably launch hota hai;
  unlocked pe heads-up notification banta hai.
  - Source: https://source.android.com/docs/core/permissions/fsi-limits
  - Source: https://proandroiddev.com/full-screen-intent-fsi-notifications-in-android-14-15-what-changed-why-its-breaking-and-e5e862a75936
- **Strategy for JeevanRekha:**
  1. App ko Play Console mein **"alarm/calling-style"** declare karo (emergency
     dispatch alert = legitimate use). Health-emergency alert is a strong case.
  2. `canUseFullScreenIntent()` check karo; agar nahi, user ko onboarding mein
     permission screen pe bhejo + **notification channel IMPORTANCE_HIGH** +
     alarm-style ringtone + vibration.
  3. **Fallback:** `SYSTEM_ALERT_WINDOW` (overlay/"Display over other apps") —
     rural India mein OEM battery killers (Xiaomi/Samsung/Vivo/Oppo/Realme) ke
     against last resort. Onboarding mein "autostart" + "no battery optimization"
     guide karo (in OEMs ke specific settings).
  4. FCM data message → `onMessageReceived` → full-screen intent activity
     (alarm UI: "EMERGENCY dispatched to your area — REF JR-4821 — [Confirm]
     [View]") + loud ringtone.

### 5.3 App tech stack — recommendation
Options: native **Kotlin + Jetpack Compose**, **Flutter**, **React Native**, ya
**TWA/PWA** (existing web app ko wrap).

| Criteria | Kotlin/Compose | Flutter | React Native | TWA/PWA |
|---|---|---|---|---|
| FCM + full-screen intent + foreground service | ✅ best/native | ✅ good (plugins) | ⚠️ ok (headless tricky) | ❌ push limited, no FSI when killed |
| Offline + low-end device | ✅ | ✅ | ⚠️ | ⚠️ |
| Dev speed (small team) | ⚠️ slower | ✅ fast | ✅ fast | ✅ fastest (reuse web) |
| Indian languages / i18n | ✅ | ✅ | ✅ | ✅ (reuse i18n) |
| Background reliability (Doze/OEM) | ✅ best | ✅ | ⚠️ | ❌ |

- **Recommendation:** **Native Kotlin + Jetpack Compose** for the ASHA/Operator
  app. Reason: ye ek **life-safety, background-critical, push-heavy** app hai —
  full-screen intent, foreground service, exact alarms, OEM battery-killer
  workarounds, aur FCM data-message handling **native pe sabse reliable** hain.
  Flutter acceptable #2 hai (agar team ko Dart aata ho / cross-platform iOS bhi
  chahiye ho). **TWA/PWA reject** — killed app pe FSI/push reliable nahi, jo
  is use-case ki jaan hai.
- **Note:** Existing web UI (`/asha`, `/admin`, `/sim`) ko app ke andar
  **WebView** mein bhi dikha sakte ho (hybrid) — native shell + FCM + WebView
  content. Isse dev time bachta hai aur live dashboards reuse hote hain.

### 5.4 App auth (mobile ke liye token-based)
- **Problem:** Backend abhi **cookie-session** (itsdangerous signed cookie) use
  karta hai (`security.py`). Native app ke liye cookies awkward hain.
- **Solution:** Mobile ke liye **token-based auth** add karo:
  - `POST /api/auth/login` (username+password) → **JWT** (ya opaque token) return
    kare, jisme `user_id`, `role`, `region_id`, expiry. Existing PBKDF2 verify
    reuse karo.
  - App token ko **EncryptedSharedPreferences / Android Keystore** mein store
    kare (plaintext nahi).
  - API calls pe `Authorization: Bearer <token>`.
  - Cookie-session web ke liye rakho; token mobile ke liye — dono `current_user`
    se resolve hon (ek `get_current_user` dependency jo cookie *ya* Bearer dekhe).
- **Device token registration:** Login ke baad app apna **FCM registration token**
  backend ko bheje (`POST /api/devices` {token, role, region_id}) → `DeviceToken`
  table. Dispatch events pe is table se tokens nikaal ke FCM bhejo.
- **Files:** naya `app/web/auth_api.py`, `app/models.py` (DeviceToken),
  `app/services/push.py`, `app/security.py` (JWT helper).

### 5.5 App screens (ASHA + Operator)
- **Login** (role-based redirect).
- **ASHA home:** apne region ke calls (live), repeat callers, recurring danger
  signs — ye `/asha` already web pe dikhata hai; app mein native list + WebView
  detail. **Emergency dispatch alert** full-screen.
- **Operator desk:** live dispatch cases, pending alerts (confirm/decline/ack) —
  `/sim` desk ka mobile version. Sab kuch **live** (FCM + foreground SSE/WebSocket
  refresh).
- **Call detail:** timeline (dispatch events), answers, triage result —
  `/admin/calls/<ref>` ka mobile version.
- **Live updates:** App foreground mein ho to **SSE/WebSocket** se continuous
  refresh (`/api/flow/{ref}/events` already hai; operator ke liye ek
  `/api/desk/events` SSE banao jo sab active cases stream kare). Background/killed
  mein **FCM** wake kare.

### 5.6 Offline + low bandwidth (rural India)
- **Cache** last-known region calls + contacts (Room DB / SQLite local).
- **Retry with exponential backoff** har network call pe (Flaky 2G/3G).
- **Small payloads** (FCM data message minimal rakho; detail app khud fetch kare).
- **Sync-on-reconnect:** offline mein kiya gaya confirm/decline queue karke
  reconnect pe bhejo (conflict resolution: server authoritative).
- **Show staleness:** "last updated X min ago" — honest UI.

### 5.7 Distribution
- **Play Store:** Health apps pe **Data Safety form** + policy compliance.
  "Emergency/medical" claims carefully — app ko **"health information & dispatch
  coordination tool"** bolo, "medical device/diagnostic" nahi (regulatory).
  FSI permission Play Console mein declare karo.
- **Pilot/field:** ASHA workers government-contracted hain → **Play Console
  Internal Testing / Closed Testing** (email list) ya **enterprise distribution**
  (managed Google Play / direct APK sideload with MDM) zyada practical hai
  public listing se. Sideload ke liye OEM "install unknown apps" allow karwana
  padega.
- **iOS:** Abhi skip (ASHA workers mostly Android pe hain, rural India). Baad
  mein Flutter/RN se cross-platform soch sakte ho.

---

## 6. PHASE 4 — Compliance & Safety (parallel, Week 2–8)

### 6.1 DPDP Act 2023 (health data = sensitive)
- **Sabse important legal basis:** DPDP Act **Section 7 "certain legitimate
  uses"** mein **"medical emergencies and health services"** explicitly aata hai.
  Matlab emergency triage/dispatch ke liye personal data process karna **bina
  explicit consent ke bhi lawful** ho sakta hai (medical emergency ground). Ye
  JeevanRekha ke liye bahut bada relief hai — panicking caller se consent form
  sign karwana impossible hai.
  - Source: https://www.hlc.com/en/publications/indias-digital-personal-data-protection-act-2023-brought-into-force-
- **Par consent + obligations phir bhi:**
  - Consent "free, specific, informed, unambiguous, unconditional, clear
    affirmative action" (Section 6) — **bundled consent nahi**. Jahan emergency
    ground na lage (e.g. marketing/follow-up), wahan explicit consent lo.
  - **Data Fiduciary** (tum) **fully responsible** ho — even jab third-party
    (cloud, Exotel, Firebase) use karo. Vendor agreements (Data Processor
    contracts) zaroori.
  - **Data Principal rights:** access, correction, erasure, grievance redressal,
    withdraw consent. Inhe implement karo (ek grievance contact + process).
  - **Breach notification:** Data breach pe Data Protection Board + affected
    users ko notify. Incident response plan banao.
  - **Purpose + storage limitation:** sirf zaroori data, sirf zaroori time tak.
    Caller phone optional (already DPDP-minimal — accha). Retention policy define
    karo (e.g. dispatch records X saal, call recordings Y din).
  - **No "legitimate interest"/"contractual necessity"** grounds (GDPR se alag) —
    sirf consent + Section 7 enumerated uses.
  - Phased compliance timeline **12–18 months** (Act in force). Source:
    https://www.incorpx.io/guide/dpdp-act-2023-compliance-guide-businesses
    https://pmc.ncbi.nlm.nih.gov/articles/PMC12423081/
- **Code posture already accha hai:** phone masked in staff views, admin
  aggregate/anonymised, ASHA region-scoped, contact details snapshotted. Ise
  document karo (privacy notice) + retention/erasure endpoints add karo.
- **TRAI consent ≠ DPDP consent:** Plivo docs explicitly kehte hain TRAI consent
  DPDP obligations ko exempt nahi karta. Dono alag comply karo.

### 6.2 Clinical / content governance
- **Language packs review:** 6 languages ke danger-sign questions + guidance ko
  **native speakers + health educators** se review karwao (README limitation).
  Medical accuracy + cultural appropriateness + reading level.
- **Medical advisory board:** Ek MBBS/OBGYN/pediatrician + ASHA supervisor ko
  advisory board pe rakho. Rules engine ke red/amber classification aur guidance
  pe sign-off lo. Version-control karo (`ENGINE_VERSION` already hai — bump on
  clinical change).
- **No-dosing policy:** README kehta hai "no dosing, no novel clinical
  instructions" — isko enforce karo (guidance text review mein check).
- **Disclaimer:** App + IVR pe clear "ye diagnostic tool nahi, emergency mein
  108/102 ko turant call karein" message.

### 6.3 Emergency-service regulations
- Private entity **"emergency number" operate nahi kar sakta** (108/102/112
  government ke hain). Tumhara number ek **health-helpline/triage line** hai jo
  108/102 ko **handoff** karti hai — marketing/legal mein ye distinction clear
  rakho. "Ambulance bhejte hain" mat bolo; "ambulance dispatch mein madad karte
  hain / 108 ko alert karte hain" bolo.
- **112** = national emergency number (all-in-one). Isko bhi handoff target banao.
- Recording/audit: emergency calls record karna (Exotel `Record=true`) + retain
  karna (legal + quality) — par DPDP retention limits ke andar.

---

## 7. PHASE 5 — Ops, Monitoring, Scale (Week 6+)

- **Backups:** Postgres automated backups (daily full + WAL archiving / PITR).
  Dispatch audit trail kabhi lose nahi hona chahiye. Test restore.
- **DR:** Multi-AZ Postgres, app container restart-on-failure, health probes.
  RPO/RTO define karo (life-safety → low RTO).
- **Load/concurrency:** Pilot mein ek region ke liye single VM kaafi. Scale pe:
  load balancer + multiple web containers (scheduler alag), Postgres read
  replicas (dashboards ke liye), Redis (rate-limit/Celery). SSE connections
  scale karna (har caller ek SSE) — connection limits + timeout dekho.
- **CI/CD:** GitHub Actions → test → build → deploy (blue-green ya rolling).
  Migrations deploy se pehle (init-container / entrypoint).
- **Runbooks + on-call:** "dispatch exhausted" alert → on-call human. Provider
  down → fallback (SMS-only, ya doosra provider). Scheduler crash → restart +
  alert.
- **Cost ballpark (rough, verify karo):**
  - Exotel: per-minute voice (~₹1–3/min inbound/outbound) + per-SMS (~₹0.15–0.30)
    + monthly DID rental (~₹500–2000/number). Pilot: ~₹5k–20k/month.
  - Hosting: AWS/GCP Mumbai VM (4vCPU/8GB) ~₹4k–8k/month + RDS ~₹3k–6k/month.
  - Firebase: FCM free (generous limits). Sentry/Prometheus: free tiers.
  - DLT/KYC: one-time + nominal per-template.
  - (Exact pricing vendor se confirm karo — ye indicative hain.)

---

## 8. Phased Roadmap (order matters)

| Phase | Kya | Duration | Blocker for |
|---|---|---|---|
| **0** | Codebase index + ye plan | done | — |
| **1** | Backend hardening: Postgres+Alembic, scheduler fix, security (webhook auth, SECRET_KEY, HTTPS, rate-limit), region resolution, observability, Docker, CI/CD | Week 1–2 | Real telephony, app |
| **2** | Real telephony: Exotel account+KYC, DID(s), ExoML inbound IVR fix, outbound responder alerts, DLT registration, 108/102 handoff design | Week 2–4 | "phone se call" |
| **3** | Android app: Kotlin/Compose, FCM high-priority push, full-screen emergency alert, token auth, ASHA+Operator screens, offline, distribution | Week 3–7 | "live app" |
| **4** | Compliance: DPDP (Section 7 basis, consent, breach, retention), clinical review, emergency-service legal | Week 2–8 (parallel) | Field use |
| **5** | Ops: backups, DR, monitoring/alerting, scale, runbooks, on-call | Week 6+ | Production |

**MVP (pilot) = Phase 1 + 2 + 3 (basic) + 4 (DPDP basics).** Ek region, ek DID,
2–3 ASHA, 1 operator, real calls, FCM alerts. Ye 6–10 weeks mein ho sakta hai.

---

## 9. Honest Limitations & Risks (ye zaroor padho)

1. **Ye ek LIFE-SAFETY service hai.** Bug = jaan ja sakti hai. Isliye:
   - Clinical review ke bina field mein mat utaro.
   - "Help is coming" kabhi mat bolo jab tak human confirm na ho (code already
     honest hai — isko maintain rakho).
   - Exhaustion/operator-alert pe real human follow-up zaroori (automation fail
     ho to insaan).
2. **108/102 ko replace nahi kar sakte** — sirf augment/handoff. Government
   integration ke bina tumhara dispatch "best-effort local chain" hai.
3. **Telephony = external dependency.** Exotel down / network down → fallback
   plan (SMS-only, doosra provider, manual operator dialing).
4. **FCM delivery 100% guaranteed nahi** (Google best-effort high-priority deta
   hai). Critical alerts ke liye **SMS fallback + voice call to ASHA** bhi rakho
   (multi-channel). App killed + OEM battery killer = push miss ho sakta hai →
   onboarding mein battery settings guide karo.
5. **Legal liability:** Emergency routing mein galti (galat region, missed
   escalation) → liability. Insurance + clear terms + audit trail + medical
   advisory board. DPDP penalties ₹250 crore tak (data breach).
6. **Language packs** machine/human-written hain — native + clinical review ke
   bina risk.
7. **Cost + ops burden:** 24×7 on-call, monitoring, DLT renewals, KYC — ye
   "deploy and forget" nahi hai.

---

## 10. Concrete Next Actions (aaj se shuru)

**Week 1 (backend, main abhi kar sakta hoon agar bolo):**
- [x] `Dockerfile` + `docker-compose.yml` (web + scheduler + postgres; TLS proxy ke saath `--proxy-headers`).
- [x] Alembic setup + initial migration + startup `alembic upgrade head` (entrypoint).
- [x] Scheduler ko `JR_RUN_SCHEDULER` flag se gate karo + dedicated worker (`python -m app.services.scheduler_worker`) + DB heartbeat.
- [x] Webhook auth (shared secret + IP allowlist + Twilio signature verify) + replay dedupe + rejection audit log.
- [x] SECRET_KEY production hard-fail + cookie `secure=True` (env-driven).
- [x] `/healthz`, `/readyz` (DB + scheduler heartbeat), optional Sentry. (Structured JSON logging pending — basicConfig hi hai.)
- [x] Region-resolution table (`region_phone_numbers`) + inbound `To`/`CallTo` DID mapping.
- [x] Rate limiting (hand-rolled pure-ASGI, no slowapi dep; SSE-safe) + CSRF double-submit on login/admin form POSTs.
- [x] CI (GitHub Actions: compileall + pytest + fresh-DB migration check).

**Week 2–3 (telephony):**
- [ ] Exotel account + KYC + ek test DID. *(vendor onboarding — user karna hai)*
- [x] `telephony.py` Exotel adapter rewrite (ExoML `<Gather>` inbound,
      `Calls/connect` flow pattern: From=callee, CallerId=ExoPhone, Url=ExoML
      endpoint, StatusCallback; correct response parsing; DLT-aware SMS).
- [x] `webhooks.py` ExoML responses (Gather+Redirect IVR loop, emergency
      status loop with bounded hold, responder-alert + call-status endpoints,
      event-id-precise confirm, mispress-safe digits, CallSid session binding).
- [x] Busy/no-answer/failed outbound legs escalate immediately (attempt_failed).
- [x] Admin UI for DID→region mapping (Regions & routing page).
- [ ] DLT PE registration + SMS templates. *(vendor onboarding — user karna hai)*
- [ ] Real end-to-end test call. *(needs real Exotel credentials + DID)*

**Week 3–7 (Android):**
- [ ] Firebase project + service account + `firebase-admin` in backend.
- [ ] `DeviceToken` model + `/api/devices` + `app/services/push.py`.
- [ ] FCM send hooks in `dispatch.py` (alert/confirm/decline/exhausted).
- [ ] Token auth (`/api/auth/login` JWT) + `get_current_user` Bearer support.
- [ ] Kotlin/Compose app: login, FCM data-message service, full-screen emergency
      intent, ASHA home, Operator desk (SSE live), call detail, offline cache.
- [ ] Play Console internal testing + FSI declaration.

**Parallel (compliance):**
- [ ] DPDP: privacy notice, Section 7 medical-emergency basis document, consent
      flow (jahan needed), grievance contact, retention policy, breach plan.
- [ ] Clinical advisory board + language-pack review.
- [ ] 108/102 handoff design + (later) government MoU.

---

## Appendix A — Source URLs (research)

**Telephony (India):**
- Twilio India Voice Guidelines (domestic = N/A): https://www.twilio.com/en-us/guidelines/in/voice
- Twilio Toll-free restrictions: https://help.twilio.com/articles/115007579027-Toll-free-Phone-Number-Restrictions-and-Limitations
- Exotel Voice APIs (Voice V1 / ExoML / LeadAssist): https://docs.exotel.com/voice-apis
- Exotel Incoming Call + applets: https://developer.exotel.com/docs/voice-v1/api-reference/incoming-call
- Exotel IVR Setup (DTMF): https://developer.exotel.com/docs/call-support/call-features/ivr-setup
- Exotel Passthru Applet: https://developer.exotel.com/docs/app-bazaar/passthru-applet-guide
- Exotel Make-a-Call API (`Calls/connect`): https://developer.exotel.com/api/make-a-call-api
- Plivo India Calling Regulations (KYC, media anchoring, number series, consent): https://www.plivo.com/docs/voice/concepts/india-calling
- Plivo XML (`GetDigits`/`Speak`): https://www.plivo.com/docs/voice/xml/overview
- TRAI Advice to Senders (DLT/PE/header/template): https://trai.gov.in/advice-to-senders
- Infobip India DLT registration: https://www.infobip.com/docs/essentials/asia-registration/dlt-registration

**Android real-time:**
- FCM message priority (high wakes device): https://firebase.google.com/docs/cloud-messaging/android/message-priority
- Android Doze & App Standby: https://developer.android.com/training/monitoring-device-state/doze-standby
- Firebase blog (FCM reach on Android, 2025): https://firebase.blog/posts/2025/04/fcm-on-android/
- Firebase Admin SDK send (Python): https://firebase.google.com/docs/cloud-messaging/send/admin-sdk
- FCM HTTP v1 API: https://firebase.google.com/docs/cloud-messaging/send/v1-api
- Android full-screen intent limits (Android 14): https://source.android.com/docs/core/permissions/fsi-limits
- FSI in Android 14/15 (what changed): https://proandroiddev.com/full-screen-intent-fsi-notifications-in-android-14-15-what-changed-why-its-breaking-and-e5e862a75936

**FastAPI production:**
- FastAPI Deployment (overview): https://fastapi.tiangolo.com/deployment/
- Server Workers (uvicorn --workers): https://fastapi.tiangolo.com/deployment/server-workers/
- Docker (Dockerfile, exec-form CMD, --proxy-headers, deprecated base image): https://fastapi.tiangolo.com/deployment/docker/
- Run manually (fastapi run): https://fastapi.tiangolo.com/deployment/manually/

**DPDP Act 2023:**
- DPDPA brought into force (Section 7 legitimate uses incl. medical emergency): https://www.hlc.com/en/publications/indias-digital-personal-data-protection-act-2023-brought-into-force-
- DPDP compliance guide for businesses: https://www.incorpx.io/guide/dpdp-act-2023-compliance-guide-businesses
- DPDP implications for healthcare: https://pmc.ncbi.nlm.nih.gov/articles/PMC12423081/
- Data protection laws in India (overview): https://www.dlapiperdataprotection.com/?t=law&c=IN

---

*Ye plan deep web research + poore codebase ko padh ke banaya gaya hai. Agar bolo
to main Phase 1 (Dockerfile, Alembic, scheduler fix, webhook auth, region
resolution) aur Phase 2 ka Exotel ExoML rewrite abhi implement karna shuru kar
sakta hoon, aur Phase 3 ka Android app scaffold (Kotlin + FCM) bhi bana sakta hoon.*
