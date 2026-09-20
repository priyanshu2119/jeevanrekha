"""Persistence model.

Design notes that matter for safety and auditability:

* Every call -- emergency, urgent or reassurance -- produces a ``CallSession``
  row, its ``Answer`` rows and exactly one ``TriageResult``. There is no code
  path that ends a session without a result (see engine fail-safe).
* Dispatch is stored as an append-only ``DispatchEvent`` log plus a small
  ``DispatchCase`` summary. Events are never updated or deleted; the case row
  only moves forward. This is what makes "did we actually get help moving?"
  answerable after the fact.
* Contact details are snapshotted onto each event so the audit trail stays
  correct even if a region's contact configuration changes later.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# UtcDateTime forwards (timezone=True) to its impl and guarantees every
# value read back from the database is tz-aware UTC -- see db.py.
from .db import Base, UtcDateTime as DateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Enumerations (stored as plain strings for SQLite/Postgres portability)
# --------------------------------------------------------------------------

class Role(str, enum.Enum):
    admin = "admin"
    asha = "asha"
    operator = "operator"


class Channel(str, enum.Enum):
    web = "web"                 # companion web/WhatsApp-style flow
    phone_sim = "phone_sim"     # local simulated voice call (DTMF console)
    phone_exotel = "phone_exotel"
    phone_twilio = "phone_twilio"


class CallStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"
    abandoned = "abandoned"


class Track(str, enum.Enum):
    maternal = "maternal"
    newborn = "newborn"


class AnswerValue(str, enum.Enum):
    yes = "yes"
    no = "no"
    unclear = "unclear"   # "don't know", silence, invalid input -> red flag


class Tier(str, enum.Enum):
    emergency = "emergency"
    urgent = "urgent"
    reassurance = "reassurance"


class DispatchStatus(str, enum.Enum):
    active = "active"
    confirmed = "confirmed"        # at least one track confirmed by a human
    exhausted = "exhausted"        # every contact tried, none confirmed
    cancelled = "cancelled"        # e.g. caller reached care on their own


class DispatchTrack(str, enum.Enum):
    ambulance = "ambulance"        # track A: official emergency service
    backup = "backup"              # track B: local backup chain
    operator = "operator"          # human escalation alerts


class DispatchAction(str, enum.Enum):
    call_placed = "call_placed"
    sms_sent = "sms_sent"
    timeout = "timeout"
    escalated = "escalated"
    confirmed = "confirmed"
    declined = "declined"
    failed = "failed"
    exhausted = "exhausted"
    operator_alert = "operator_alert"


class ContactKind(str, enum.Enum):
    ambulance_node = "ambulance_node"
    asha = "asha"
    phc = "phc"
    local_transport = "local_transport"
    district_control = "district_control"


# --------------------------------------------------------------------------
# Configuration layer: regions and their routing tables
# --------------------------------------------------------------------------

class Region(Base):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(80), index=True)
    district: Mapped[str] = mapped_column(String(80), index=True)
    block: Mapped[str] = mapped_column(String(80))
    # Official emergency number for this area. Usually 108, but some states
    # run variants and some blocks have a direct line to an ambulance node --
    # routing must be locally configurable, never one-size-fits-all.
    emergency_number: Mapped[str] = mapped_column(String(32), default="108")
    # Per-region override of the dispatch confirmation window (seconds).
    confirm_window_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    contacts: Mapped[list["BackupContact"]] = relationship(
        back_populates="region", order_by="BackupContact.priority"
    )

    @property
    def label(self) -> str:
        return f"{self.block}, {self.district} ({self.state})"


class BackupContact(Base):
    """One link in a region's parallel-routing chain (track B)."""

    __tablename__ = "backup_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32), default=ContactKind.asha.value)
    phone: Mapped[str] = mapped_column(String(32))
    # Lower number = contacted first when the chain escalates.
    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    region: Mapped[Region] = relationship(back_populates="contacts")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16), default=Role.asha.value)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ASHA workers are scoped to one region; admins/operators see all.
    region_id: Mapped[int | None] = mapped_column(
        ForeignKey("regions.id"), nullable=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(256))
    salt: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(8), default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    region: Mapped[Region | None] = relationship()


# --------------------------------------------------------------------------
# Call lifecycle
# --------------------------------------------------------------------------

class CallSession(Base):
    __tablename__ = "call_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Short human-readable reference the caller can quote to any health
    # worker later ("JR-4821"). Never reused.
    ref_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    channel: Mapped[str] = mapped_column(String(24), default=Channel.web.value)
    # Optional. A frightened caller must not be forced to identify herself
    # to get help; DPDP-minimal by design.
    caller_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), index=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    track: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=CallStatus.in_progress.value, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    region: Mapped[Region | None] = relationship()
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="call", order_by="Answer.id"
    )
    result: Mapped["TriageResult | None"] = relationship(
        back_populates="call", uselist=False
    )
    dispatch: Mapped["DispatchCase | None"] = relationship(
        back_populates="call", uselist=False
    )
    messages: Mapped[list["StatusMessage"]] = relationship(
        back_populates="call", order_by="StatusMessage.id"
    )


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("call_id", "question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("call_sessions.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(8))
    # True when the value was produced by the safety default (silence,
    # invalid input, explicit "don't know") rather than a clear answer.
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    repeats: Mapped[int] = mapped_column(Integer, default=0)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[CallSession] = relationship(back_populates="answers")


class TriageResult(Base):
    __tablename__ = "triage_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call_sessions.id"), unique=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(16), index=True)
    # Danger-sign keys that fired, e.g. ["m_bleeding", "n_fits"].
    flagged: Mapped[list] = mapped_column(JSON, default=list)
    unclear_count: Mapped[int] = mapped_column(Integer, default=0)
    # Ordered guidance keys rendered to the caller in her language.
    guidance: Mapped[list] = mapped_column(JSON, default=list)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    engine_version: Mapped[str] = mapped_column(String(16))
    # Set when the fail-safe path (engine exception) produced this result.
    fail_safe: Mapped[bool] = mapped_column(Boolean, default=False)

    call: Mapped[CallSession] = relationship(back_populates="result")


class DispatchCase(Base):
    __tablename__ = "dispatch_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call_sessions.id"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=DispatchStatus.active.value, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ambulance_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backup_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[CallSession] = relationship(back_populates="dispatch")
    events: Mapped[list["DispatchEvent"]] = relationship(
        back_populates="case", order_by="DispatchEvent.id"
    )


class DispatchEvent(Base):
    __tablename__ = "dispatch_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("dispatch_cases.id"), index=True)
    track: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(24))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    # Snapshot of who was contacted (config may change later; audit must not).
    contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # For timeout scheduling: when this attempt's window closes.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    case: Mapped[DispatchCase] = relationship(back_populates="events")


class StatusMessage(Base):
    """The caller-facing live feed. Deliberately append-only and deliberately
    honest: statuses say what the system knows, never more."""

    __tablename__ = "status_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("call_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="status")  # status|guidance|system
    message_key: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[CallSession] = relationship(back_populates="messages")


class IncidentLog(Base):
    """Fail-safe activations and provider errors. If the engine ever throws,
    the caller still gets the emergency tier and we get a row here."""

    __tablename__ = "incident_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int | None] = mapped_column(
        ForeignKey("call_sessions.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
