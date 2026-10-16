from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from forge_runtime.credentials import (
    ROLES,
    delete,
    load,
    masked_profile,
    test_connection,
    upsert,
)

from .api import require_control_token
from .schemas import ProviderListOut, ProviderProfileOut

router = APIRouter(prefix="/providers", tags=["providers"], dependencies=[Depends(require_control_token)])


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    api_key: str | None = None
    auth_mode: str | None = Field(default=None, min_length=1)


def _profiles() -> dict[str, dict[str, Any]]:
    return {role: masked_profile(profile) for role, profile in load()["profiles"].items()}


@router.get("", response_model=ProviderListOut)
def providers_list() -> dict[str, Any]:
    return {"version": 1, "profiles": _profiles()}


@router.put("/{role}", response_model=ProviderProfileOut)
def providers_put(role: str, body: ProviderUpdate) -> dict[str, Any]:
    if role not in ROLES:
        raise HTTPException(422, f"unsupported provider role: {role}")
    values = body.model_dump(exclude_none=True)
    if values.get("api_key") == "":
        values.pop("api_key")
    try:
        return masked_profile(upsert(role, values))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{role}", status_code=204)
def providers_delete(role: str) -> None:
    if role not in ROLES:
        raise HTTPException(422, f"unsupported provider role: {role}")
    delete(role)


@router.post("/{role}/test")
def providers_test(role: str, body: ProviderUpdate | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise HTTPException(422, f"unsupported provider role: {role}")
    stored = load()["profiles"].get(role, {})
    values = {**stored, **(body.model_dump(exclude_none=True) if body else {})}
    if not values.get("base_url"):
        raise HTTPException(422, "base_url is required to test a provider")
    return test_connection(values)
