"""Public web layer: landing page, web companion triage, flow API, SSE."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..engine import i18n
from ..models import CallSession, CallStatus, Channel, Region
from ..services import call_flow
from . import serializers
from .deps import get_templates

router = APIRouter()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
    regions = db.scalars(
        select(Region).where(Region.is_active.is_(True)).order_by(Region.state, Region.district)
    ).all()
    return get_templates().TemplateResponse(request, "landing.html", {
        "regions": regions,
        "languages": i18n.LANGUAGE_NAMES,
    })


@router.get("/triage", response_class=HTMLResponse)
def triage_page(request: Request, db: Session = Depends(get_db)):
    regions = db.scalars(
        select(Region).where(Region.is_active.is_(True)).order_by(Region.state, Region.district)
    ).all()
    # Explicit digit order for the language picker: tojson sorts dict keys,
    # which would silently desync the buttons from the DTMF mapping.
    language_order = [
        {"digit": d, "code": code, "name": i18n.LANGUAGE_NAMES[code]}
        for d, code in sorted(i18n.LANGUAGE_BY_DIGIT.items())
    ]
    return get_templates().TemplateResponse(request, "triage.html", {
        "regions": regions,
        "languages": i18n.LANGUAGE_NAMES,
        "language_order": language_order,
        "speech_locales": i18n.SPEECH_LOCALE,
    })


# --------------------------------------------------------------------------
# Flow API (used by the web companion AND the phone simulator -- one state
# machine, many channels)
# --------------------------------------------------------------------------

class StartBody(BaseModel):
    region_id: int | None = None
    caller_phone: str | None = None
    language: str | None = None
    channel: str = "web"


class DigitBody(BaseModel):
    digit: str


@router.post("/api/flow/start")
def flow_start(body: StartBody, db: Session = Depends(get_db)):
    channel = Channel.phone_sim if body.channel == "phone_sim" else Channel.web
    phone = (body.caller_phone or "").strip() or None
    session = call_flow.start_call(
        db,
        channel=channel,
        region_id=body.region_id,
        caller_phone=phone,
        language=body.language if body.language in i18n.PACKS else None,
    )
    db.commit()
    return serializers.flow_snapshot(db, session)


@router.get("/api/flow/{ref}")
def flow_get(ref: str, after: int = 0, db: Session = Depends(get_db)):
    session = serializers.get_session_by_ref(db, ref)
    if session is None:
        return {"error": "not_found"}
    return serializers.flow_snapshot(db, session, after_id=after)


@router.post("/api/flow/{ref}/digit")
def flow_digit(ref: str, body: DigitBody, db: Session = Depends(get_db)):
    session = serializers.get_session_by_ref(db, ref)
    if session is None:
        return {"error": "not_found"}
    step = call_flow.handle_digit(db, session, body.digit)
    db.commit()
    snap = serializers.flow_snapshot(db, session)
    snap["step_prompts"] = serializers.render_prompts(session, step.prompts)
    snap["result_ready"] = step.result_ready
    return snap


@router.post("/api/flow/{ref}/silence")
def flow_silence(ref: str, db: Session = Depends(get_db)):
    session = serializers.get_session_by_ref(db, ref)
    if session is None:
        return {"error": "not_found"}
    step = call_flow.handle_silence(db, session)
    db.commit()
    snap = serializers.flow_snapshot(db, session)
    snap["step_prompts"] = serializers.render_prompts(session, step.prompts)
    return snap


@router.post("/api/flow/{ref}/hangup")
def flow_hangup(ref: str, db: Session = Depends(get_db)):
    session = serializers.get_session_by_ref(db, ref)
    if session is None:
        return {"error": "not_found"}
    call_flow.hangup(db, session)
    db.commit()
    return serializers.flow_snapshot(db, session)


@router.get("/api/i18n")
def api_i18n(lang: str = "en", keys: str = ""):
    """Rendered chrome labels for the web companion, in the caller's language."""
    lang = lang if lang in i18n.PACKS else "en"
    wanted = [k.strip() for k in keys.split(",") if k.strip()] or []
    strings = {k: i18n.t(lang, k) for k in wanted}
    return {"lang": lang, "strings": strings}


# --------------------------------------------------------------------------
# Server-Sent Events: the honest live status feed
# --------------------------------------------------------------------------

@router.get("/api/flow/{ref}/events")
async def flow_events(ref: str):
    """Streams every new caller-facing status message plus dispatch state
    changes until the call ends (and the dispatch case settles)."""

    async def gen():
        from ..db import SessionLocal

        last_msg_id = 0
        last_dispatch_json = ""
        idle_rounds = 0
        while True:
            db = SessionLocal()
            try:
                session = serializers.get_session_by_ref(db, ref)
                if session is None:
                    yield _sse({"type": "error", "error": "not_found"})
                    return
                msgs = serializers.status_messages(db, session, after_id=last_msg_id)
                for m in msgs:
                    last_msg_id = max(last_msg_id, m["id"])
                    yield _sse({"type": "message", **m})
                dispatch = serializers.dispatch_summary(db, session)
                dj = json.dumps(dispatch, sort_keys=True) if dispatch else ""
                if dj and dj != last_dispatch_json:
                    last_dispatch_json = dj
                    yield _sse({"type": "dispatch", "dispatch": dispatch})
                ended = session.status != CallStatus.in_progress.value
                settled = (
                    dispatch is None
                    or dispatch["status"]
                    in ("confirmed", "exhausted", "cancelled")
                )
                if ended and settled:
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        yield _sse({"type": "ended", "ref": ref})
                        return
                else:
                    idle_rounds = 0
            finally:
                db.close()
            await asyncio.sleep(1.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
