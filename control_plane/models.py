from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class RunRecord(Base):
    __tablename__ = "forge_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    provider_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    current_node: Mapped[str] = mapped_column(String(32), default="planner")
    turn: Mapped[int] = mapped_column(Integer, default=0)
    max_turns: Mapped[int] = mapped_column(Integer, default=90)
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    __table_args__ = (Index("idx_forge_run_claim", "status", "lease_expires_at", "created_at"),)


class RunEventRecord(Base):
    __tablename__ = "forge_run_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("forge_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_forge_event_sequence"),)


class ApprovalRecord(Base):
    __tablename__ = "forge_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("forge_runs.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    action_digest: Mapped[str] = mapped_column(String(64), index=True)
    action_preview: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ExternalActionRecord(Base):
    __tablename__ = "forge_external_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("forge_runs.id", ondelete="CASCADE"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("forge_approvals.id"), unique=True)
    action_type: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    result_payload: Mapped[dict | None] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserSessionRecord(Base):
    __tablename__ = "forge_browser_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("forge_runs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    current_url: Mapped[str | None] = mapped_column(Text)
    allowed_hosts: Mapped[list] = mapped_column(JSON, default=list)
    storage_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class BrowserActionRecord(Base):
    __tablename__ = "forge_browser_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("forge_browser_sessions.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    mutating: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("forge_approvals.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_forge_browser_action_sequence"),)

