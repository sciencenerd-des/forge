from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    ApprovalRecord,
    BrowserActionRecord,
    BrowserSessionRecord,
    ExternalActionRecord,
    RunEventRecord,
    RunRecord,
)


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def action_digest(action_type: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"type": action_type, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def append_event(db: Session, run_id: str, event_type: str, actor: str, payload: dict[str, Any]) -> RunEventRecord:
    run = db.scalar(select(RunRecord).where(RunRecord.id == run_id).with_for_update())
    if not run:
        raise NotFoundError("run not found")
    sequence = db.scalar(select(func.coalesce(func.max(RunEventRecord.sequence), 0) + 1).where(RunEventRecord.run_id == run_id))
    event = RunEventRecord(run_id=run_id, sequence=sequence, event_type=event_type, actor=actor, payload=payload)
    db.add(event)
    return event


def create_run(db: Session, project_id: str, goal_id: str, provider_id: str | None, max_turns: int) -> RunRecord:
    run = RunRecord(project_id=project_id, goal_id=goal_id, provider_id=provider_id, max_turns=max_turns)
    db.add(run)
    db.flush()
    append_event(db, run.id, "run.created", "control-plane", {"max_turns": max_turns})
    db.commit()
    return run


def claim_run(db: Session, worker_id: str, lease_seconds: int) -> RunRecord | None:
    now = now_utc()
    candidate_id = db.scalar(
        select(RunRecord.id)
        .where(
            RunRecord.status.in_(("queued", "running")),
            or_(RunRecord.lease_expires_at.is_(None), RunRecord.lease_expires_at < now),
        )
        .order_by(RunRecord.created_at, RunRecord.id)
        .limit(1)
    )
    if candidate_id is None:
        return None
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(seconds=lease_seconds)
    claimed = db.execute(
        update(RunRecord)
        .where(
            RunRecord.id == candidate_id,
            RunRecord.status.in_(("queued", "running")),
            or_(RunRecord.lease_expires_at.is_(None), RunRecord.lease_expires_at < now),
        )
        .values(status="running", lease_owner=worker_id, lease_token=token, lease_expires_at=expires, heartbeat_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return None
    append_event(db, candidate_id, "lease.claimed", worker_id, {"expires_at": expires.isoformat()})
    db.commit()
    return db.get(RunRecord, candidate_id)


def heartbeat_run(db: Session, run_id: str, worker_id: str, lease_token: str, lease_seconds: int) -> RunRecord:
    now = now_utc()
    expires = now + timedelta(seconds=lease_seconds)
    result = db.execute(
        update(RunRecord)
        .where(
            RunRecord.id == run_id,
            RunRecord.status == "running",
            RunRecord.lease_owner == worker_id,
            RunRecord.lease_token == lease_token,
            RunRecord.lease_expires_at >= now,
        )
        .values(heartbeat_at=now, lease_expires_at=expires, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ConflictError("lease is missing, expired, or owned by another worker")
    db.commit()
    return db.get(RunRecord, run_id)


def release_run(db: Session, run_id: str, worker_id: str, lease_token: str, *, requeue: bool = True) -> RunRecord:
    status = "queued" if requeue else "paused"
    result = db.execute(
        update(RunRecord)
        .where(RunRecord.id == run_id, RunRecord.lease_owner == worker_id, RunRecord.lease_token == lease_token)
        .values(status=status, lease_owner=None, lease_token=None, lease_expires_at=None, updated_at=now_utc())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ConflictError("lease token does not own this run")
    append_event(db, run_id, "lease.released", worker_id, {"status": status})
    db.commit()
    return db.get(RunRecord, run_id)


def request_approval(db: Session, *, run_id: str, action_type: str, action_payload: dict[str, Any], risk: str, requested_by: str, ttl_seconds: int) -> ApprovalRecord:
    if not db.get(RunRecord, run_id):
        raise NotFoundError("run not found")
    approval = ApprovalRecord(
        run_id=run_id,
        action_type=action_type,
        action_digest=action_digest(action_type, action_payload),
        action_preview=_redact(action_payload),
        risk=risk,
        requested_by=requested_by,
        expires_at=now_utc() + timedelta(seconds=ttl_seconds),
    )
    db.add(approval)
    db.flush()
    append_event(db, run_id, "approval.requested", requested_by, {"approval_id": approval.id, "action_type": action_type, "risk": risk})
    db.commit()
    return approval


def decide_approval(db: Session, approval_id: str, actor: str, approved: bool, reason: str) -> ApprovalRecord:
    approval = db.get(ApprovalRecord, approval_id)
    if not approval:
        raise NotFoundError("approval not found")
    if approval.status != "pending" or as_utc(approval.expires_at) <= now_utc():
        raise ConflictError("approval is no longer pending")
    approval.status = "approved" if approved else "denied"
    approval.decided_by = actor
    approval.decision_reason = reason
    approval.decided_at = now_utc()
    append_event(db, approval.run_id, f"approval.{approval.status}", actor, {"approval_id": approval.id, "reason": reason})
    db.commit()
    return approval


def consume_approval(db: Session, approval_id: str, run_id: str, action_type: str, payload: dict[str, Any]) -> ApprovalRecord:
    now = now_utc()
    digest = action_digest(action_type, payload)
    result = db.execute(
        update(ApprovalRecord)
        .where(
            ApprovalRecord.id == approval_id,
            ApprovalRecord.run_id == run_id,
            ApprovalRecord.action_type == action_type,
            ApprovalRecord.action_digest == digest,
            ApprovalRecord.status == "approved",
            ApprovalRecord.consumed_at.is_(None),
            ApprovalRecord.expires_at > now,
        )
        .values(status="consumed", consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ConflictError("approval is invalid, expired, mismatched, or already consumed")
    db.flush()
    return db.get(ApprovalRecord, approval_id)


def queue_external_action(db: Session, *, run_id: str, approval_id: str, action_type: str, action_payload: dict[str, Any], idempotency_key: str) -> ExternalActionRecord:
    consume_approval(db, approval_id, run_id, action_type, action_payload)
    record = ExternalActionRecord(run_id=run_id, approval_id=approval_id, action_type=action_type, request_payload=_redact(action_payload), idempotency_key=idempotency_key)
    db.add(record)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise ConflictError("idempotency key or approval has already been used") from error
    append_event(db, run_id, "external_action.queued", "control-plane", {"action_id": record.id, "action_type": action_type})
    db.commit()
    return record


def create_browser_session(db: Session, run_id: str, allowed_hosts: list[str]) -> BrowserSessionRecord:
    if not db.get(RunRecord, run_id):
        raise NotFoundError("run not found")
    session = BrowserSessionRecord(run_id=run_id, allowed_hosts=allowed_hosts)
    db.add(session)
    db.flush()
    append_event(db, run_id, "browser.created", "control-plane", {"session_id": session.id, "allowed_hosts": allowed_hosts})
    db.commit()
    return session


def queue_browser_action(db: Session, session_id: str, action: str, arguments: dict[str, Any], approval_id: str | None) -> BrowserActionRecord:
    session = db.get(BrowserSessionRecord, session_id)
    if not session:
        raise NotFoundError("browser session not found")
    mutating = action in {"click", "type", "press"}
    if action == "navigate":
        host = (urlparse(str(arguments.get("url", ""))).hostname or "").lower()
        if host not in session.allowed_hosts:
            raise ConflictError("navigation host is not allowlisted")
    if mutating:
        if not approval_id:
            raise ConflictError("mutating browser actions require one-time approval")
        consume_approval(db, approval_id, session.run_id, f"browser.{action}", arguments)
    sequence = db.scalar(select(func.coalesce(func.max(BrowserActionRecord.sequence), 0) + 1).where(BrowserActionRecord.session_id == session_id))
    record = BrowserActionRecord(session_id=session_id, sequence=sequence, action=action, arguments=_redact(arguments), mutating=mutating, approval_id=approval_id)
    db.add(record)
    db.flush()
    append_event(db, session.run_id, "browser.action_queued", "control-plane", {"session_id": session_id, "action_id": record.id, "action": action})
    db.commit()
    return record


def serialize_model(record: Any) -> dict[str, Any]:
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def _redact(value: Any) -> Any:
    markers = ("key", "token", "secret", "password", "authorization", "cookie")
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(marker in key.lower() for marker in markers) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
