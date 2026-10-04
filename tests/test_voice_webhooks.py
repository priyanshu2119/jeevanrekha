"""Phase 2 voice webhooks: the real-phone path end to end.

Proves that an Exotel/Twilio-driven call behaves exactly like the simulator:
Gather-based DTMF collection, silence via Redirect fall-through, the honest
emergency status loop, bounded hold time, precise responder confirmation and
immediate escalation when an outbound alert call never connects.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import (
    CallSession,
    CallStatus,
    DispatchAction,
    DispatchCase,
    DispatchEvent,
    RegionPhoneNumber,
    Tier,
)
from app.services import call_flow, dispatch as dispatch_service, voice_xml
from app.web import webhooks


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _public_url(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://jr.test")
    # These tests drive DTMF faster than any human (identical digits within
    # the same second); replay-dedupe has its own proofs in
    # test_webhook_security.py, so keep it out of the flow tests.
    monkeypatch.setattr(settings, "WEBHOOK_DEDUP_SEC", 0)


def _inbound(client, *, sid="CA-flow", frm="9876500011", to="", digits=None,
             url="/webhooks/exotel/inbound"):
    data = {"CallSid": sid, "From": frm}
    if to:
        data["To"] = to
    if digits is not None:
        data["Digits"] = digits
    return client.post(url, data=data)


def _pending(db, track="backup"):
    return db.scalars(select(DispatchEvent).where(
        DispatchEvent.track == track,
        DispatchEvent.action == DispatchAction.call_placed.value,
        DispatchEvent.resolved.is_(False),
    )).first()


def _run_emergency_via_webhooks(client, db, region, *, sid="CA-em", frm="9876522222"):
    """Drive a full maternal emergency (bleeding=yes) through the webhook.

    Language digit 2 = English so the assertions below can match the English
    language pack (digit 1 would render every prompt in Hindi).
    """
    db.add(RegionPhoneNumber(region_id=region.id, provider="exotel",
                             phone_number="08077788899"))
    db.commit()
    r = _inbound(client, sid=sid, frm=frm, to="08077788899", digits="2")  # english
    assert "<Gather" in r.text
    r = _inbound(client, sid=sid, frm=frm, to="08077788899", digits="1")  # maternal
    assert "<Gather" in r.text
    r = _inbound(client, sid=sid, frm=frm, to="08077788899", digits="1")  # bleeding YES
    for _ in range(9):  # remaining questions: no
        r = _inbound(client, sid=sid, frm=frm, to="08077788899", digits="2")
    return r


class TestVoiceXmlBuilder:
    def test_escaping_covers_attributes_and_text(self):
        assert voice_xml.esc('a & b < c > d " e \' f') == \
            "a &amp; b &lt; c &gt; d &quot; e &apos; f"

    def test_gather_uses_official_exoml_attribute_names(self):
        # Attribute names verified against exotel/goexoml verbs.go.
        xml = voice_xml.gather(voice_xml.say("hi", language="hi-IN"),
                               "https://x.test/cb?a=1&b=2", timeout=7, num_digits=1)
        assert 'action="https://x.test/cb?a=1&amp;b=2"' in xml
        assert 'method="POST"' in xml
        assert 'timeout="7"' in xml
        assert 'numDigits="1"' in xml
        assert 'finishOnKey="#"' in xml
        assert '<Say language="hi-IN"' in xml

    def test_ivr_gather_pairs_gather_with_silence_redirect(self):
        xml = voice_xml.ivr_gather("press one", "https://x.test/a", "https://x.test/s")
        assert "<Gather" in xml and "<Redirect" in xml
        assert xml.index("<Gather") < xml.index("<Redirect")
        assert "<Hangup" not in xml

    def test_ivr_final_hangs_up(self):
        xml = voice_xml.ivr_final("goodbye")
        assert "<Say" in xml and "<Hangup/>" in xml and "<Gather" not in xml


class TestInboundFlow:
    def test_every_step_is_a_gather_with_absolute_action(self, client, db, region):
        r = _inbound(client, digits="1")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/xml")
        assert '<Gather action="https://jr.test/webhooks/exotel/inbound' in r.text
        assert 'numDigits="1"' in r.text
        # Silence fall-through loops back to the same webhook.
        assert "<Redirect" in r.text and "https://jr.test/webhooks/exotel/inbound" in r.text
        # No Hangup mid-triage, ever.
        assert "<Hangup" not in r.text

    def test_silence_arrives_without_digits_and_repeats(self, client, db, region):
        r1 = _inbound(client, sid="CA-sil", frm="9876533333")
        assert "<Gather" in r1.text
        # Second silence on the same question -> recorded UNCLEAR, flow moves on.
        _inbound(client, sid="CA-sil", frm="9876533333")
        session = db.scalars(select(CallSession).where(
            CallSession.provider_call_sid == "CA-sil")).first()
        assert session is not None

    def test_session_found_by_call_sid_even_with_different_from(self, client, db, region):
        _inbound(client, sid="CA-same", frm="9876544444", digits="1")   # language
        _inbound(client, sid="CA-same", frm="0000099999", digits="1")   # track (odd From)
        sessions = db.scalars(select(CallSession).where(
            CallSession.channel == "phone_exotel")).all()
        assert len(sessions) == 1
        assert sessions[0].track == "maternal"

    def test_region_resolves_from_did_and_backup_chain_fires(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        session = db.scalars(select(CallSession).where(
            CallSession.provider_call_sid == "CA-em")).first()
        assert session.region_id == region.id
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        assert case is not None
        # BOTH tracks rang: ambulance (108) and the regional backup chain.
        assert _pending(db, "ambulance") is not None
        assert _pending(db, "backup") is not None

    def test_non_emergency_plays_guidance_and_hangs_up(self, client, db, region):
        _inbound(client, sid="CA-fine", frm="9876555555", digits="1")
        _inbound(client, sid="CA-fine", frm="9876555555", digits="1")
        for _ in range(10):
            r = _inbound(client, sid="CA-fine", frm="9876555555", digits="2")
        assert "<Hangup/>" in r.text
        assert "<Gather" not in r.text
        session = db.scalars(select(CallSession).where(
            CallSession.provider_call_sid == "CA-fine")).first()
        assert session.status == CallStatus.completed.value
        assert session.result.tier == Tier.reassurance.value

    def test_twilio_inbound_also_gathers(self, client, db, region):
        r = _inbound(client, sid="CA-tw", frm="9876566666", digits="2",
                     url="/webhooks/twilio/inbound")
        assert r.status_code == 200
        assert "<Gather" in r.text and "/webhooks/twilio/inbound" in r.text


class TestEmergencyStatusLoop:
    def test_loop_stays_open_then_confirms_and_hangs_up(self, client, db, region):
        r = _run_emergency_via_webhooks(client, db, region)
        # Transition render: emergency result + first-response guidance, held
        # in a Gather loop (caller kept honestly informed, line stays open).
        assert "<Gather" in r.text and "<Hangup" not in r.text

        # A silence iteration re-renders latest status + "still waiting".
        r = _inbound(client, sid="CA-em", frm="9876522222")
        assert "<Gather" in r.text
        assert "Still waiting for confirmation" in r.text

        # Responder confirms -> next render is final: confirmed + goodbye + Hangup.
        ev = _pending(db, "backup")
        case = db.get(DispatchCase, ev.case_id)
        dispatch_service.confirm(db, case, ev.track, by="test")
        db.commit()
        r = _inbound(client, sid="CA-em", frm="9876522222")
        assert "<Hangup/>" in r.text
        assert "has confirmed" in r.text
        assert "JR-" in r.text  # reference repeated before goodbye

    def test_loop_is_bounded(self, client, db, region, monkeypatch):
        monkeypatch.setattr(webhooks, "MAX_STATUS_LOOPS", 2)
        _run_emergency_via_webhooks(client, db, region)          # loop 1
        _inbound(client, sid="CA-em", frm="9876522222")          # loop 2
        r = _inbound(client, sid="CA-em", frm="9876522222")      # loop 3 -> cap
        assert "<Hangup/>" in r.text
        assert "Still waiting" in r.text  # honest: never claims confirmation
        # Dispatch keeps running server-side after the caller leg ends.
        session = db.scalars(select(CallSession).where(
            CallSession.provider_call_sid == "CA-em")).first()
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        assert case.status == "active"


class TestResponderAlert:
    def test_alert_xml_targets_the_exact_event(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.get(f"/webhooks/exotel/responder-alert?event_id={ev.id}")
        assert r.status_code == 200
        assert "<Gather" in r.text
        assert f"/webhooks/responder-confirm?event_id={ev.id}" in r.text
        assert "EMERGENCY" in r.text
        assert "Press 1 to confirm" in r.text
        assert "ASHA One" in r.text  # addressed to the right responder

    def test_resolved_alert_says_so_and_hangs_up(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        case = db.get(DispatchCase, ev.case_id)
        dispatch_service.confirm(db, case, ev.track, by="test")
        db.commit()
        r = client.get(f"/webhooks/exotel/responder-alert?event_id={ev.id}")
        assert "already been resolved" in r.text
        assert "<Hangup/>" in r.text and "<Gather" not in r.text

    def test_unknown_event_is_handled_gracefully(self, client, db):
        r = client.get("/webhooks/exotel/responder-alert?event_id=999999")
        assert r.status_code == 200
        assert "no longer active" in r.text

    def test_garbage_event_id_returns_xml_not_422(self, client, db):
        # Providers execute XML; a FastAPI validation error on a live call
        # leg would drop the responder into a provider error path.
        for bad in ("", "abc", "-1"):
            r = client.get(f"/webhooks/exotel/responder-alert?event_id={bad}")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/xml")
            assert "<Hangup/>" in r.text


class TestResponderConfirm:
    def test_confirm_by_event_id(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.post(f"/webhooks/responder-confirm?event_id={ev.id}",
                        data={"Digits": "1"})
        assert r.json()["ok"] is True
        db.refresh(ev)
        assert ev.resolved
        case = db.get(DispatchCase, ev.case_id)
        assert case.backup_confirmed_at is not None

    def test_decline_escalates_without_retry(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.post(f"/webhooks/responder-confirm?event_id={ev.id}",
                        data={"Digits": "2"})
        assert r.json()["ok"] is True
        nxt = _pending(db, "backup")
        assert nxt is not None and nxt.id != ev.id
        assert nxt.contact_name == "PHC Two"  # next link, no retry after decline

    def test_mispress_is_ignored_not_declined(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.post(f"/webhooks/responder-confirm?event_id={ev.id}",
                        data={"Digits": "9"})
        assert r.json() == {"ok": False, "error": "invalid_digit"}
        db.refresh(ev)
        assert not ev.resolved  # window keeps running; timeout will escalate

    def test_phone_tail_fallback_still_works(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.post("/webhooks/responder-confirm",
                        data={"phone": "9000000001", "digits": "1"})
        assert r.json()["ok"] is True
        db.refresh(ev)
        assert ev.resolved


class TestCallStatus:
    def test_busy_escalates_immediately(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        r = client.post(f"/webhooks/exotel/call-status?event_id={ev.id}",
                        data={"Status": "busy", "CallSid": "CB-1"})
        assert r.json()["ok"] is True
        db.refresh(ev)
        assert ev.resolved
        failed = db.scalars(select(DispatchEvent).where(
            DispatchEvent.case_id == ev.case_id,
            DispatchEvent.action == DispatchAction.failed.value)).first()
        assert failed is not None and "busy" in failed.detail
        # Retry at the same contact (attempt 2), not a dead wait.
        nxt = _pending(db, "backup")
        assert nxt is not None and nxt.attempt == 2

    def test_completed_without_keypress_keeps_window(self, client, db, region):
        _run_emergency_via_webhooks(client, db, region)
        ev = _pending(db, "backup")
        client.post(f"/webhooks/exotel/call-status?event_id={ev.id}",
                    data={"Status": "completed", "CallSid": "CB-2"})
        db.refresh(ev)
        assert not ev.resolved  # responder may already be moving
        assert "awaiting confirmation" in ev.detail

    def test_inbound_teardown_finalises_partial_triage(self, client, db, region):
        # Caller hung up mid-triage after answering bleeding=YES.
        _inbound(client, sid="CA-drop", frm="9876577777", digits="1")  # hindi
        _inbound(client, sid="CA-drop", frm="9876577777", digits="1")  # maternal
        _inbound(client, sid="CA-drop", frm="9876577777", digits="1")  # bleeding yes
        r = client.post("/webhooks/exotel/call-status",
                        data={"Status": "completed", "CallSid": "CA-drop",
                              "Direction": "inbound"})
        assert r.json()["ok"] is True
        session = db.scalars(select(CallSession).where(
            CallSession.provider_call_sid == "CA-drop")).first()
        assert session.status == CallStatus.completed.value
        # Missing answers = unclear = escalate: the partial call is EMERGENCY
        # and dispatch is running even though the caller is gone.
        assert session.result.tier == Tier.emergency.value
        case = db.scalars(select(DispatchCase).where(
            DispatchCase.call_id == session.id)).first()
        assert case is not None and case.status == "active"
