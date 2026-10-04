# JeevanRekha (जीवनरेखा)

A phone-first triage and **parallel emergency-routing** service for maternal and
newborn danger signs, built for rural India. A caller answers a short, fixed set
of questions in their own language; the system deterministically classifies the
call into one of three tiers — and on the emergency tier it does not merely
advise, it **starts help moving on two tracks at once** (official ambulance +
locally configured backup chain), tells the caller the honest live status, and
**auto-escalates on every timeout until a human confirms** or the chain is
exhausted and an operator is paged.

It is not a diagnostic tool, not a chatbot, and contains **no LLM anywhere near
a decision**. Triage is a pure, auditable rules engine; every unclear answer
escalates by design.

---

## Quick start

```bash
./run.sh                 # venv + deps + seed + serve on :8300
```

or manually:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/seed.py        # regions, contacts, staff, 14 days of history
.venv/bin/python -m uvicorn app.main:app --port 8300
```

Then open:

| URL | What it is |
|---|---|
| `http://127.0.0.1:8300/` | Public landing page |
| `http://127.0.0.1:8300/triage` | **Web companion** — the caller flow with buttons + browser voice |
| `http://127.0.0.1:8300/sim` | **Phone simulator + dispatch desk** — DTMF handset on the left; on the right you play every responder the system rings |
| `http://127.0.0.1:8300/login` | Staff sign-in |

Staff accounts created by the seed script:

| User | Password | Role |
|---|---|---|
| `admin` | `admin123` | Administrator — dashboards, region & routing config |
| `sunita` | `asha123` | ASHA worker — Ashta, Sehore (MP) call log |
| `kavita` | `asha123` | ASHA worker — Osmanabad Rural (MH) call log |
| `operator` | `op123` | Dispatch desk operator |

## Verifying the definition of done (5-minute script)

1. **Emergency + parallel routing + honest status** — Open `/sim`, pick any
   region, *Place call*. Press `1` (Hindi) or `2` (English), `1` (pregnant
   woman), then `1` on the first question (heavy bleeding) and `2` for the
   rest. Watch: the emergency result, first-response guidance, and **two**
   alerts appear on the dispatch desk (Track A ambulance, Track B backup).
   The caller transcript says *"requested — we have NOT yet received
   confirmation"*. Never a fake "help is coming".
2. **Ambiguity escalates, never passes as safe** — Start another call and press
   `3` ("cannot say") on any red-flag question, or just keep hitting *No
   input*: after one repeat the answer is recorded UNCLEAR and treated as
   positive. Hang up mid-triage and the watchdog still finalises the call with
   the answers so far (missing = unclear = escalate).
3. **Low urgency reassures without dismissal** — Answer `2` to everything:
   calm outcome, real self-care guidance (rest, meals, iron-folic acid as
   prescribed, danger-sign awareness), and an explicit "you are welcome to
   call again".
4. **Timeout → active re-contact, visible in the log** — On an emergency call,
   do **not** confirm on the desk. When the window closes (480 s default; the
   seeded *Nasrullaganj (test)*-style region you can add with a 30 s window
   makes this instant), the engine retries, then walks the chain
   (ASHA → PHC → transport → district control room), narrating every step to
   the caller, until confirmation or full exhaustion + operator alert. The
   whole ladder is on the call-detail timeline (`/admin/calls/<ref>`).
5. **Every call retrievable + rolls up** — Sign in as `sunita` (`/asha`) to see
   her region's calls, repeat callers and recurring danger signs; as `admin`
   (`/admin`) for aggregate volume, tier distribution and — front and centre —
   **confirmed-within-window vs escalated vs exhausted** dispatch rates per
   region.
6. **Multilingual, no smartphone needed** — The phone path is pure DTMF
   (`1/2/3`) in Hindi, English, Marathi, Bengali, Tamil and Telugu; the
   simulator reproduces it exactly. The web companion adds optional browser
   TTS in the same six languages.

Run the automated proof any time:

```bash
.venv/bin/python -m pytest        # 128 tests: engine, flow, dispatch, HTTP,
                                  # webhook auth, CSRF, rate limits, probes,
                                  # ExoML voice IVR, provider adapters
```

## Architecture

```
caller ── DTMF/buttons ──► call_flow state machine (services/call_flow.py)
                              │  state derived from DB rows → restart-safe
                              ▼
                        rules engine (engine/rules.py)   PURE · DETERMINISTIC
                              │  yes/no/unclear → emergency | urgent | fine
                              │  unclear/missing/invalid ⇒ red flag, always
                              ▼
                        dispatch (services/dispatch.py)
                          Track A: ambulance (region.emergency_number)
                          Track B: backup chain (priority-ordered contacts)
                              │  every attempt = immutable event + due_at
                              ▼
                        scheduler (services/scheduler.py)  asyncio, 2 s tick
                          timeout → retry → next contact → district control
                          → operator alert → exhausted (never silent)
                              │
                              ▼
                        honest live status → caller (SSE / IVR playback)
                        full audit trail   → ASHA log / admin dashboard
```

**Stack & why** (research per spec §8): Python 3.11 + FastAPI (async SSE,
webhook-native), SQLAlchemy 2 + SQLite-WAL (zero-ops local-first; swap to
Postgres via `JR_DATABASE_URL` for production), Jinja2 + vanilla JS + custom
SVG (low-bandwidth, no build step, works on old Android browsers), PBKDF2
stdlib auth. Voice: DTMF-first IVR — the pattern India's own 108/banking lines
use; deterministic, works on every handset, no STT to mishear a panicking
caller. Production telephony adapters for **Exotel** (India-first CPaaS) and
**Twilio** are included (`JR_TELEPHONY=exotel|twilio` + credentials); their
webhooks drive the same state machine, and responder "press 1 to confirm"
maps to `POST /webhooks/responder-confirm`.

**Clinical content basis**: danger-sign categories from publicly documented
government/WHO material — MoHFW *Training Manual on Newborn and Child Health
Services for ASHA*, *Notes for ASHA Trainers*, IMNCI newborn danger signs, and
published lists of pregnancy danger signs (bleeding, fits, high fever,
breathing difficulty, severe headache/blurred vision, severe abdominal pain,
reduced fetal movement, water leaking, swelling, persistent vomiting; baby not
feeding, convulsions, fast breathing/chest indrawing, hot-or-cold body,
lethargy, yellow palms/soles, cord infection, watery stools). Guidance text is
public-health-education level — **no dosing, no novel clinical instructions**.

## Safety properties (enforced in code, proven in tests)

- **Ambiguity ⇒ escalation.** `3`, silence, invalid input, unanswered
  questions — all recorded UNCLEAR and scored as positive red flags
  (`tests/test_engine.py`, `tests/test_flow.py`).
- **Every call ends in exactly one tier.** Engine exceptions take the
  fail-safe path (EMERGENCY + incident log); idle sessions are finalised by a
  watchdog; hangups mid-triage finalise with partial answers.
- **No false certainty.** Status strings state only what is known
  ("requested, NOT yet confirmed"); confirmation requires a human.
- **Never silent.** Timeouts retry, escalate down the chain, page the
  operator, and mark the case exhausted — all as append-only events.
- **No LLM in any decision path.** Translation/phrasing is static, reviewed
  language-pack data.
- **DPDP-2023-minded data posture.** Caller phone optional; masked in staff
  views; admin dashboard aggregate/anonymised; ASHA scoped to her region;
  contact details snapshotted onto events so audits survive config changes.

## Configuration

Per-region (admin UI → *Regions & routing*): emergency number, confirmation
window, ordered backup chain (ASHA / PHC / local transport / district control
room). Global env knobs in `.env.example` (`JR_CONFIRM_WINDOW`,
`JR_MAX_ATTEMPTS`, `JR_TELEPHONY`, `JR_DATABASE_URL`, …).

## Project layout

```
app/
  engine/          # pure triage: questions.py, rules.py, i18n.py, lang/{en,hi,mr,bn,ta,te}.py
  services/        # call_flow.py (state machine), dispatch.py (routing/escalation),
                   # scheduler.py (durable worker), telephony.py (providers)
  web/             # public.py (landing/triage/API/SSE), sim.py (simulator+desk),
                   # staff.py (login/ASHA/admin/regions), webhooks.py (Exotel/Twilio)
  templates/       # Jinja2 (base, landing, triage, sim, login, asha, admin*, call_detail)
  static/          # css/app.css (design system), js/{flow,triage,sim}.js, img/
scripts/seed.py    # regions, contacts, staff, engine-computed 14-day history
tests/             # 64 tests covering the definition of done
```

## Production notes

**One command (Docker Compose):** copy `.env.example` to `.env`, set
`JR_SECRET_KEY` and `JR_PG_PASSWORD`, then `docker compose up --build`. This
starts Postgres, the web service (2 uvicorn workers, in-process scheduler
OFF) and exactly one dedicated escalation-scheduler process. The entrypoint
applies `alembic upgrade head` before serving.

- **Migrations**: schema is managed by Alembic (`alembic upgrade head`).
  A database created before migrations existed (e.g. an old `run.sh` dev
  database built by `create_all`) is adopted with:
  `alembic stamp e951365fb128 && alembic upgrade head`.
- **Scheduler**: escalation timing lives in the DB, but the worker is a
  process. Run it exactly once: either in-process (single-process
  deployments, `JR_RUN_SCHEDULER=true` default) or as
  `python -m app.services.scheduler_worker` while web workers set
  `JR_RUN_SCHEDULER=false` (compose does this). N workers with in-process
  schedulers would place N duplicate outbound calls per timeout.
- **Webhook auth**: `/webhooks/*` is the only public door into triage and
  dispatch. Set `JR_WEBHOOK_SECRET` (sent as `X-JR-Webhook-Secret` or
  `?token=` — Exotel dashboards can only set the URL) and/or
  `JR_WEBHOOK_CIDRS` (source-IP allowlist; run uvicorn with
  `--proxy-headers` behind your TLS proxy). Twilio requests are additionally
  verified via `X-Twilio-Signature` (HMAC-SHA1) when `JR_TWILIO_TOKEN` is
  set. Provider webhook retries are deduplicated (`JR_WEBHOOK_DEDUP`) so a
  replayed DTMF digit can never answer the next question by accident.
  `JR_ENV=production` refuses to boot with a real provider and no webhook
  auth, or with the default secret key.
- **Inbound region routing**: real phone calls resolve their region from the
  dialled number via the `region_phone_numbers` table (one DID per region is
  the reliable routing signal — without a region there is no Track B backup
  chain).
- **Probes & observability**: `GET /healthz` (liveness), `GET /readyz`
  (DB + scheduler-heartbeat age; make heartbeat staleness fatal with
  `JR_READYZ_REQUIRE_SCHEDULER=true` on the scheduler container). Optional
  Sentry via `JR_SENTRY_DSN`. Per-IP rate limits protect `/login` and
  `/api/flow/start` (webhooks and probes are exempt by design).
- **CSRF**: staff form POSTs (login, region/contact config) use a
  double-submit cookie (`jr_csrf`); session cookies are `Secure` when
  `JR_ENV=production`.
- Set `JR_TELEPHONY=exotel|twilio` + credentials, point `JR_DATABASE_URL` at
  Postgres, terminate TLS at your reverse proxy.

### Real telephony wiring (Exotel)

The voice path is ExoML (Exotel's event-driven XML, verified against their
official `goexoml` verb set). One-time setup per deployment:

1. **ExoPhone (DID)**: rent a landline-series number (TRAI: service/
   transactional use; the 140-series is promotional-only), complete KYC.
2. **Exophone URL** (dashboard → call flow / app URL):
   `https://<your-host>/webhooks/exotel/inbound?token=<JR_WEBHOOK_SECRET>`.
   Every step of the call is a `<Gather>` served by that endpoint; silence
   falls through a `<Redirect>` back to it (recorded as silence → repeat →
   UNCLEAR → escalate, exactly like the simulator).
3. **Status callback** (dashboard or per-call): point call-status events at
   `https://<your-host>/webhooks/exotel/call-status` — inbound teardowns
   finalise partial triage; outbound alert legs that end busy/no-answer/failed
   escalate immediately instead of burning the confirmation window.
4. **Map the DID to its region**: Admin → *Regions & routing* → *Helpline
   numbers*. This is what makes Track B (the local backup chain) fire for
   real phone calls — an unmapped number gets only the generic 108 alert.
5. **Outbound alerts need no dashboard work**: dispatch calls
   `Calls/connect.json` (From = responder, CallerId = your ExoPhone,
   Url = `/webhooks/exotel/responder-alert?event_id=N`) and the responder
   hears the alert and presses 1 (confirm) / 2 (cannot help — escalates at
   once; any other key is ignored, a mispress is never a decline).
6. **SMS (DLT)**: register your Principal Entity, header and emergency
   template on a DLT portal, then set `JR_EXOTEL_DLT_TEMPLATE` /
   `JR_EXOTEL_DLT_ENTITY` — Indian carriers drop SMS without them.
7. Set `JR_PUBLIC_BASE_URL` to the public origin (ExoML action URLs must be
   absolute; production refuses to boot without it).

Twilio adapter note: Twilio's own India guidelines mark domestic inbound/
outbound voice as N/A, so Twilio is only viable where you hold usable
non-Indian numbers; Exotel is the India path. The TwiML endpoints mirror the
ExoML ones (`/webhooks/twilio/*`).

- The `/sim` desk locks behind operator login automatically once a real
  telephony provider is configured.
- A WhatsApp/low-bandwidth text companion can reuse `/api/flow/*` unchanged —
  it is the same state machine the web companion already uses.

## Honest limitations

- Local mode simulates the PSTN: no real calls are placed (by design — the
  spec's definition-of-done is written against *simulated callers*). The
  Exotel/Twilio adapters are real code paths but need credentials and a
  deployment to exercise end-to-end.
- Language packs are carefully written but should be reviewed by native
  speakers/health educators before field use.
- Seed history is engine-computed from scripted scenarios so dashboards are
  meaningful on day one; delete `data/` and re-seed without history, or just
  start making real calls.
