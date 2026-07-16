"""Versioned evidence contract for the Forge eval harness.

Raw runs only become comparable evidence once every run declares *under what
conditions it was produced*. This module is the single source of truth for:

* the result schema (``SuiteResultV2`` / ``GoalResultV2``) and its version;
* the mechanical verdict vocabulary (only ``verified`` is a completion);
* the legal launcher/goal lifecycle-state matrix (which pairs are corruption vs
  a legitimately resumable run);
* the comparability fingerprint the gate uses to refuse unlike comparisons;
* atomic, checkpoint-after-every-goal result writing.

Design rule from the plan: a metric that was not measured is stored as ``null``
with a ``*_unavailable_reason``. It is *never* defaulted to zero, because a
favorable zero (0 cycles, 0 cost) silently games any gate.
"""

from __future__ import annotations

import hashlib
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 2


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #
class Verdict(str, Enum):
    """Mechanical per-goal verdicts. Only ``VERIFIED`` counts as completion."""

    VERIFIED = "verified"                    # acceptance contract reproduced real behavior
    COMPLETE_UNVERIFIED = "complete_unverified"  # agent claimed done, contract rejected
    BLOCKED = "blocked"                      # could not verify / resumable non-terminal
    TIMEOUT = "timeout"                      # exceeded the owned budget
    RUNTIME_ERROR = "runtime_error"          # the agent's/artifact's code errored
    HARNESS_ERROR = "harness_error"          # our infra failed (docker, verifier, DB)
    SKIPPED = "skipped"                      # not attempted (no backend/lease)


VALID_VERDICTS = tuple(v.value for v in Verdict)


def is_completion(verdict: str) -> bool:
    return verdict == Verdict.VERIFIED.value


# --------------------------------------------------------------------------- #
# Legal lifecycle-state matrix
# --------------------------------------------------------------------------- #
# A launcher terminal status and the durable goal status can legitimately
# disagree: a run may be ``blocked``/``timeout``/``stopped`` while its goal
# correctly remains ``active`` (it is resumable). The *only* impossible claim is
# a launcher that reports ``completed`` while the durable goal is not completed —
# that is the false-green corruption we must flag, not string inequality.
_GOAL_NOT_DONE = frozenset({"active", "proposed", "blocked", "pending"})


def is_legal_state_pair(launcher_status: str, goal_status: str | None) -> bool:
    """Return True when (launcher_status, goal_status) is a legitimate pairing."""
    launcher = (launcher_status or "").lower()
    goal = (goal_status or "").lower()
    if launcher == "completed":
        # Launcher claims completion; the durable goal must agree.
        return goal == "completed"
    # Any non-completed launcher status is compatible with any goal status:
    # blocked/timeout/stopped/failed + active is resumable; + completed is a
    # run that ended after the goal was already satisfied.
    return True


def state_pair_reason(launcher_status: str, goal_status: str | None) -> str | None:
    """Human-readable reason a pair is inconsistent, or None when legal."""
    if is_legal_state_pair(launcher_status, goal_status):
        return None
    return (f"launcher reported {launcher_status!r} but durable goal is "
            f"{goal_status!r}; a completion claim requires a completed goal")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class EnvironmentFingerprint(BaseModel):
    """Everything that must match for two runs to be comparable evidence."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    git_sha: str | None = None
    dirty_diff_hash: str | None = None       # hash of uncommitted diff, or None if clean
    suite_hash: str | None = None            # hash of the goal catalog
    contract_hash: str | None = None         # hash of acceptance contracts + drivers
    model: str | None = None
    provider: str | None = None
    dialect: str | None = None
    endpoint_identity: str | None = None     # redacted host:port, never full URL w/ creds
    sandbox_mode: str | None = None          # "container" | "host-unsafe" | ...
    sandbox_image_digest: str | None = None
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    python_version: str | None = None
    pi_version: str | None = None
    host_arch: str | None = None
    max_turns: int | None = None
    reasoning_effort: str | None = None
    llm_request_timeout_s: float | None = None
    llm_max_retries: int | None = None
    replicate_count: int = 1


class GoalResultV2(BaseModel):
    """A single goal's auditable, reproducible terminal verdict."""

    model_config = ConfigDict(extra="allow")

    goal_id: str
    slug: str | None = None
    goal: str | None = None

    # Lifecycle evidence
    run_id: str | None = None
    project_id: str | None = None
    launcher_status: str | None = None
    goal_status: str | None = None            # durable goal row status
    terminal_reason: str | None = None

    # Verdicts (mechanical)
    outcome: str = Verdict.SKIPPED.value       # gate-facing verdict (see Verdict)
    acceptance_verdict: str | None = None      # from the goal-specific contract
    infrastructure_verdict: str | None = None  # verifier container health
    false_completion: bool = False

    # Artifact evidence
    snapshot_digest: str | None = None
    acceptance: dict[str, Any] | None = None   # full contract report (commands/output)
    error: str | None = None
    evidence_tail: str | None = None           # clipped stdout/stderr tail

    # Metrics — null when unmeasured, with a reason; NEVER defaulted to zero.
    turns_used: int | None = None
    batches_used: int | None = None
    cycles_to_done: int | None = None
    distance_auc: float | None = None
    token_cost: int | None = None
    elapsed_s: float | None = None
    measurement_unavailable_reason: str | None = None


class SuiteResultV2(BaseModel):
    """The full schema-versioned suite report written atomically per goal."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    environment: EnvironmentFingerprint = Field(default_factory=EnvironmentFingerprint)
    model: str | None = None
    goals: list[GoalResultV2] = Field(default_factory=list)

    # Aggregates
    total: int = 0
    completed: int = 0                         # == verified count
    verified_completion_rate: float | None = None
    false_completion_count: int = 0
    # Comparison metrics — null (with a reason) until Phase 5 measures them; the
    # gate refuses to compare v2 reports whose required metrics are null.
    mean_cycles_to_done: float | None = None
    mean_distance_auc: float | None = None
    mean_turns: float | None = None
    token_cost: int | None = None
    measurement_unavailable_reason: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_s: float | None = None
    summary: dict[str, int] = Field(default_factory=dict)

    def recompute_aggregates(self) -> None:
        self.total = len(self.goals)
        self.completed = sum(1 for g in self.goals if is_completion(g.outcome))
        self.false_completion_count = sum(1 for g in self.goals if g.false_completion)
        self.verified_completion_rate = (self.completed / self.total) if self.total else None
        counts: dict[str, int] = {}
        for g in self.goals:
            counts[g.outcome] = counts.get(g.outcome, 0) + 1
        self.summary = counts
        verified = [g for g in self.goals if is_completion(g.outcome)]

        def mean(field: str) -> float | None:
            values = [getattr(g, field) for g in verified]
            if not verified or any(value is None for value in values):
                return None
            return sum(values) / len(values)

        self.mean_cycles_to_done = mean("cycles_to_done")
        self.mean_distance_auc = mean("distance_auc")
        self.mean_turns = mean("turns_used")
        token_values = [g.token_cost for g in self.goals]
        self.token_cost = (
            sum(token_values) if self.goals and all(value is not None for value in token_values)
            else None
        )
        missing = [
            field for field in ("mean_cycles_to_done", "mean_distance_auc", "token_cost")
            if getattr(self, field) is None
        ]
        self.measurement_unavailable_reason = (
            "required aggregate metrics unavailable: " + ", ".join(missing)
            if missing else None
        )


# --------------------------------------------------------------------------- #
# Comparability fingerprint
# --------------------------------------------------------------------------- #
# Fields that must be identical for a gate delta to be meaningful. Kept narrow
# on purpose: these are conditions of the experiment, not its outcome.
COMPARABILITY_FIELDS = (
    "schema_version", "suite_hash", "contract_hash", "model", "provider",
    "sandbox_image_digest", "max_turns", "reasoning_effort",
    "llm_request_timeout_s", "llm_max_retries", "replicate_count",
)


def _fingerprint(report: dict[str, Any]) -> dict[str, Any]:
    env = report.get("environment") or {}
    out: dict[str, Any] = {}
    for field in COMPARABILITY_FIELDS:
        # top-level takes precedence, then the environment block
        out[field] = report.get(field, env.get(field))
    return out


def is_versioned(report: dict[str, Any]) -> bool:
    """True when a report declares the v2 schema (top-level or environment)."""
    if report.get("schema_version") is not None:
        return True
    env = report.get("environment")
    return isinstance(env, dict) and env.get("schema_version") is not None


def comparability_reasons(candidate: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Reasons the two reports are not comparable evidence.

    Only enforced for versioned (v2) reports; legacy unversioned dicts are left
    to the caller's legacy path so existing gate behavior is preserved.
    """
    if not (is_versioned(candidate) and is_versioned(baseline)):
        return []
    reasons: list[str] = []
    cand_fp, base_fp = _fingerprint(candidate), _fingerprint(baseline)
    for field in COMPARABILITY_FIELDS:
        if cand_fp[field] != base_fp[field]:
            reasons.append(
                f"incomparable {field}: candidate={cand_fp[field]!r} baseline={base_fp[field]!r}"
            )
    return reasons


# --------------------------------------------------------------------------- #
# Hashing helpers (suite / contract / diff fingerprints)
# --------------------------------------------------------------------------- #
def hash_text(*chunks: str) -> str:
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def hash_file(path: str | Path) -> str | None:
    try:
        return hash_text(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Legacy quarantine
# --------------------------------------------------------------------------- #
LEGACY_LABEL = "legacy_uncomparable"


def load_result_readonly(path: str | Path) -> dict[str, Any]:
    """Read a recorded result without rewriting it.

    Unversioned files are labeled ``legacy_uncomparable`` in-memory so callers
    cannot accidentally gate them against a v2 baseline. The file on disk is
    never modified.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"result at {path} is not a JSON object")
    if not is_versioned(data):
        data = {**data, "comparability": LEGACY_LABEL}
    return data


# --------------------------------------------------------------------------- #
# Atomic checkpoint writing
# --------------------------------------------------------------------------- #
def write_result_atomic(path: str | Path, report: SuiteResultV2 | dict[str, Any]) -> None:
    """Write ``report`` via temp file + os.replace so an interruption mid-write
    always leaves a complete, valid previous report (or the new one), never a
    truncated file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(report, SuiteResultV2):
        payload = report.model_dump(mode="json")
    else:
        payload = report
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
