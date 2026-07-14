"""One orchestration service that owns a run end to end.

Every eval CLI (``goal_suite_tui.py``, ``goal_suite.py``, ``runner.py``) should
be a thin catalog/adapter over this module so provider setup, budget ownership,
timeout termination, snapshot authority, and result shape can never drift apart
again. The orchestrator:

1. acquires an OS-level evaluation lease (no concurrent suites);
2. runs a read-only preflight and refuses to launch on any blocking failure;
3. launches a run and *owns its total wall-clock budget*;
4. on timeout/error, terminates the whole process group via ``terminate_run`` —
   a suite timeout never leaves orphaned work;
5. exports an immutable snapshot from the authoritative workspace;
6. verifies the snapshot with the goal-specific contract; and
7. checkpoints a schema-valid result atomically after every goal.

Steps 3-6 are injected callables so the workflow is unit-testable with fakes and
so later phases (container snapshot, container verifier) slot in without
reworking lifecycle ownership.
"""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import forge_config
from evals.contracts import (
    GoalResultV2,
    Verdict,
    is_legal_state_pair,
    state_pair_reason,
)


class LeaseHeld(RuntimeError):
    """Raised when another evaluation already holds the global lease."""


@dataclass
class EvaluationLease:
    """An ``fcntl.flock`` lease under FORGE_HOME. Not backed by Postgres, so it
    holds even when the DB is down. Records owner pid/start for diagnosis."""

    path: Path
    _fd: int | None = None

    @classmethod
    def default_path(cls) -> Path:
        return forge_config.home() / "locks" / "eval.lock"

    def acquire(self) -> "EvaluationLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            existing = os.read(fd, 4096).decode("utf-8", "replace").strip()
            os.close(fd)
            raise LeaseHeld(f"another evaluation holds the lease ({existing or 'unknown owner'})") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} started_at={time.time():.0f}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "EvaluationLease":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# Single-goal orchestration
# --------------------------------------------------------------------------- #
@dataclass
class GoalPlan:
    goal_id: str
    slug: str
    goal: str
    timeout_s: float


@dataclass
class LaunchHandle:
    """What a launcher returns: enough to poll and to terminate cleanly."""

    run_id: str
    project_id: str
    started: bool
    error: str | None = None


# Injected seams (real implementations wired by the CLI adapters / later phases).
Launcher = Callable[[GoalPlan], LaunchHandle]
StatusPoller = Callable[[str, str], dict[str, Any]]     # (project_id, run_id) -> manifest
Terminator = Callable[[str, str, str], dict[str, Any]]  # (project_id, run_id, reason)
Snapshotter = Callable[[str], str | None]               # (project_id) -> snapshot path
Verifier = Callable[[str, str | None], dict[str, Any]]  # (goal_id, snapshot) -> contract report


def _terminal(status: str) -> bool:
    return status in {"completed", "failed", "blocked", "stopped", "timeout", "error"}


def _verdict_from(acceptance: dict[str, Any] | None, launcher_status: str,
                  goal_status: str | None, timed_out: bool) -> tuple[str, bool]:
    """Map raw evidence to a mechanical verdict + false-completion flag.

    The acceptance contract is the completion authority; the launcher's terminal
    status only decides *non*-completion shades (timeout vs blocked vs error).
    """
    claimed_done = launcher_status == "completed"
    if timed_out:
        return Verdict.TIMEOUT.value, False
    if acceptance is not None:
        verdict = acceptance.get("verdict")
        if verdict == "accepted":
            return Verdict.VERIFIED.value, False
        if verdict == "rejected":
            return Verdict.COMPLETE_UNVERIFIED.value, bool(claimed_done)
        if verdict == "error":
            return Verdict.HARNESS_ERROR.value, False
        return Verdict.BLOCKED.value, False
    if launcher_status in {"failed", "error"}:
        return Verdict.RUNTIME_ERROR.value, False
    return Verdict.BLOCKED.value, False


def orchestrate_goal(plan: GoalPlan, *, launch: Launcher, poll: StatusPoller,
                     terminate: Terminator, snapshot: Snapshotter, verify: Verifier,
                     poll_interval: float = 1.0, clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> GoalResultV2:
    """Own one goal's full lifecycle and return a schema-valid result.

    A launch that never terminates within ``plan.timeout_s`` is terminated (its
    whole process group), then still snapshotted and verified from whatever it
    produced — so even a timeout yields an auditable verdict.
    """
    result = GoalResultV2(goal_id=plan.goal_id, slug=plan.slug, goal=plan.goal)
    handle = launch(plan)
    result.run_id, result.project_id = handle.run_id, handle.project_id
    if not handle.started:
        result.outcome = Verdict.HARNESS_ERROR.value
        result.launcher_status = "failed"
        result.terminal_reason = handle.error or "launch failed"
        result.error = handle.error
        return result

    deadline = clock() + plan.timeout_s
    timed_out = False
    manifest: dict[str, Any] = {}
    while True:
        manifest = poll(handle.project_id, handle.run_id)
        status = manifest.get("status", "")
        if _terminal(status):
            break
        if clock() >= deadline:
            timed_out = True
            terminate(handle.project_id, handle.run_id, f"eval_timeout_{int(plan.timeout_s)}s")
            manifest = poll(handle.project_id, handle.run_id)
            break
        sleep(poll_interval)

    result.launcher_status = manifest.get("status")
    result.goal_status = manifest.get("goal_status")
    result.terminal_reason = manifest.get("terminal_reason") or (
        f"eval_timeout_{int(plan.timeout_s)}s" if timed_out else None)

    # Flag impossible lifecycle pairs (completed launcher + unfinished goal) but
    # do not treat a legitimately resumable (blocked + active) pair as corruption.
    if result.launcher_status and not is_legal_state_pair(result.launcher_status, result.goal_status):
        result.infrastructure_verdict = "inconsistent"
        result.error = state_pair_reason(result.launcher_status, result.goal_status)

    result.snapshot_digest = snapshot(handle.project_id)
    acceptance = verify(plan.goal_id, result.snapshot_digest)
    result.acceptance = acceptance
    result.acceptance_verdict = acceptance.get("verdict") if acceptance else None
    outcome, false_completion = _verdict_from(
        acceptance, result.launcher_status or "", result.goal_status, timed_out)
    result.outcome, result.false_completion = outcome, false_completion
    if result.measurement_unavailable_reason is None:
        result.measurement_unavailable_reason = "convergence ledger not read by this adapter"
    return result
