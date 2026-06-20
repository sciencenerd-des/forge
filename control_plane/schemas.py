from __future__ import annotations

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
