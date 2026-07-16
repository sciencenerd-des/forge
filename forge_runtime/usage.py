"""Durable token accounting for one detached PGE run."""

from __future__ import annotations

import os
from typing import Any


def response_total_tokens(response: Any) -> int | None:
    """Extract provider-reported total tokens without estimating missing usage."""
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is not None:
        value = usage.get("total_tokens") if isinstance(usage, dict) else getattr(usage, "total_tokens", None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    if isinstance(response, dict):
        prompt = response.get("prompt_eval_count")
        completion = response.get("eval_count")
        if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in (prompt, completion)):
            return prompt + completion
    return None


def record_response_usage(response: Any) -> int | None:
    """Persist exact provider usage in the current run manifest when available."""
    total = response_total_tokens(response)
    project_id = os.getenv("FORGE_PROJECT_ID")
    run_id = os.getenv("FORGE_RUN_ID")
    if total is None or not project_id or not run_id:
        return total
    from pge_launcher import increment_run_metric

    increment_run_metric(project_id, run_id, "token_cost", total)
    return total
