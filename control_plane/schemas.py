from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RunCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    provider_id: str | None = Field(default=None, max_length=128)
    max_turns: int = Field(default=90, ge=1, le=10_000)


class RuntimeRunStart(BaseModel):
    goal: str = Field(min_length=3, max_length=500)
    description: str = Field(default="", max_length=4_000)
    project_id: str | None = Field(default=None, max_length=128)


class RuntimeRunSnapshot(BaseModel):
    """Stable response contract for the operator runtime dashboard."""

    id: str
    project_id: str
    project_name: str
    goal_id: str | None = None
    goal_title: str
    status: str
    current_node: str
    batch: int = 0
    pid: int | None = None
    updated_at: str | None = None
    active_task: str | None = None
    attempt_count: int = 0
    no_progress_count: int = 0
    task_total: int = 0
    task_completed: int = 0
    file_count: int = 0
    test_count: int = 0
    log_tail: list[str] = Field(default_factory=list)
    model: str | None = None


class RuntimeProjectSnapshot(BaseModel):
    id: str
    name: str
    repo_path: str
    goal_count: int = 0
    active_goal: str | None = None
    active_goal_status: str | None = None
    task_total: int = 0
    task_completed: int = 0


class DurableRunOut(BaseModel):
    id: str
    project_id: str
    goal_id: str
    provider_id: str | None = None
    status: str
    current_node: str
    turn: int
    max_turns: int
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class RunEventOut(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    action_type: str
    action_digest: str
    action_preview: dict[str, Any] = Field(default_factory=dict)
    risk: str
    status: str
    requested_by: str
    decided_by: str | None = None
    decision_reason: str | None = None
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    created_at: datetime


class RunStartResultOut(BaseModel):
    status: str
    started: bool
    run_id: str
    pid: int | None = None
    log: str | None = None
    source: str | None = None
    note: str | None = None
    already_running: bool = False


class StopRunResultOut(BaseModel):
    status: str
    project_id: str
    pid: int | None = None


class ProviderProfileOut(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    auth_mode: str | None = None


class ProviderListOut(BaseModel):
    version: int
    profiles: dict[str, ProviderProfileOut]


class LeaseClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=60, ge=10, le=900)


class LeaseHeartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=128)
    lease_seconds: int = Field(default=60, ge=10, le=900)


class ApprovalCreate(BaseModel):
    run_id: str
    action_type: str = Field(min_length=1, max_length=64)
    action_payload: dict[str, Any]
    risk: Literal["low", "medium", "high", "critical"]
    requested_by: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=900, ge=30, le=86_400)


class ApprovalDecision(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    approved: bool
    reason: str = Field(min_length=1, max_length=2_000)


class ExternalActionCreate(BaseModel):
    run_id: str
    approval_id: str
    action_type: str = Field(min_length=1, max_length=64)
    action_payload: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=128)


class BrowserSessionCreate(BaseModel):
    run_id: str
    allowed_hosts: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        normalized = []
        for host in hosts:
            value = host.strip().lower()
            if not value or "/" in value or "://" in value:
                raise ValueError("allowed_hosts must contain hostnames only")
            normalized.append(value)
        return sorted(set(normalized))


class BrowserActionCreate(BaseModel):
    action: Literal["navigate", "snapshot", "click", "type", "press", "scroll", "screenshot"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
