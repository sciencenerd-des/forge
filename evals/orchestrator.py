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
import hashlib
import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import forge_config
from evals.contracts import (
    EnvironmentFingerprint,
    GoalResultV2,
    SuiteResultV2,
    Verdict,
    is_legal_state_pair,
    state_pair_reason,
    write_result_atomic,
)

METRIC_SEMANTICS_VERSION = "cycles:v1;distance_auc:trapezoid-v1;token_cost:provider-usage-v1"


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


@dataclass(frozen=True)
class SnapshotArtifact:
    """Authoritative snapshot exported after a run reaches a terminal state."""

    path: str
    digest: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_payload(report: SuiteResultV2) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    # ``status`` is a compatibility alias for older dashboards; ``outcome``
    # remains the canonical V2 field consumed by the gate.
    for goal in payload.get("goals", []):
        goal.setdefault("status", goal.get("outcome"))
    return payload


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=forge_config.repo_root(), capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _dirty_diff_hash() -> str | None:
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"], cwd=forge_config.repo_root(),
            capture_output=True, text=False, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not diff:
        return None
    return hashlib.sha256(diff).hexdigest()[:16]


def _endpoint_identity(base_url: str | None) -> str | None:
    if not base_url:
        return None
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url)
    return parsed.netloc or parsed.path.split("/", 1)[0] or None


def image_digest(image: str | None) -> str | None:
    """Return the local Docker image ID used by a qualifying run."""
    if not image:
        return None
    try:
        value = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=8, check=True,
        ).stdout.strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_environment(*, model: str | None, base_url: str | None,
                      max_turns: int, suite_hash: str,
                      sandbox_image_digest: str | None = None,
                      verifier_image_digest: str | None = None,
                      sandbox_mode: str | None = None,
                      reasoning_effort: str | None = None,
                      llm_request_timeout_s: float | None = None,
                      llm_max_retries: int | None = None,
                      replicate_count: int = 1) -> EnvironmentFingerprint:
    """Build the comparable experiment fingerprint once per suite."""
    from evals.acceptance import contract_hash

    dialect = "ollama" if base_url and ":11434" in base_url else "openai"
    return EnvironmentFingerprint(
        git_sha=_git_sha(), dirty_diff_hash=_dirty_diff_hash(),
        suite_hash=suite_hash, contract_hash=contract_hash(), model=model,
        provider=_endpoint_identity(base_url), dialect=dialect,
        endpoint_identity=_endpoint_identity(base_url), sandbox_mode=sandbox_mode,
        sandbox_image_digest=sandbox_image_digest,
        verifier_image_digest=verifier_image_digest,
        python_version=platform.python_version(), host_arch=platform.machine(),
        max_turns=max_turns, reasoning_effort=reasoning_effort,
        llm_request_timeout_s=llm_request_timeout_s,
        llm_max_retries=llm_max_retries, replicate_count=replicate_count,
    )


def build_provider_environment(*, model: str, base_url: str, max_turns: int) -> dict[str, str]:
    """Build child-only provider overrides for a qualifying suite run.

    The parent CLI process remains untouched. This matters when preflight or the
    global lease refuses a run: a failed evaluation must not silently change the
    provider used by later, unrelated Forge commands in the same process.
    """
    normalized = base_url.rstrip("/")
    if not normalized:
        raise ValueError("base_url must not be empty")
    dialect = "ollama" if ":11434" in normalized or "ollama" in normalized.lower() else "openai"
    child = {
        "LLM_MODEL": model,
        "LLM_BASE_URL": normalized,
        "FORGE_LLM_BASE_URL": normalized,
        "FORGE_LLM_DIALECT": dialect,
        "FORGE_LLM_REASONING_EFFORT": "none",
        "FORGE_LLM_TIMEOUT": str(forge_config.DEFAULT_TIMEOUT),
        "FORGE_LLM_MAX_RETRIES": "0",
        "PGE_AUDITOR_MODELS": f"lmstudio:{model}",
        "PGE_AUDITOR_BASE_URL": normalized,
        "PGE_AUDITOR_REASONING_EFFORT": "none",
        "PGE_AUDITOR_CODEX": "0",
        "PGE_LMSTUDIO_URL": normalized,
        "PGE_STEWARD_BASE_URL": normalized,
        "PGE_MAX_TURNS": str(max_turns),
    }
    for role in forge_config.ROLES:
        upper = role.upper()
        child[f"PGE_{upper}_MODEL"] = model
        child[f"FORGE_{upper}_BASE_URL"] = normalized
        child[f"FORGE_{upper}_DIALECT"] = dialect
    return child


# Injected seams (real implementations wired by the CLI adapters / later phases).
Launcher = Callable[[GoalPlan], LaunchHandle]
StatusPoller = Callable[[str, str], dict[str, Any]]     # (project_id, run_id) -> manifest
Terminator = Callable[[str, str, str], dict[str, Any]]  # (project_id, run_id, reason)
Snapshotter = Callable[[str], SnapshotArtifact | str | None]  # (project_id) -> artifact
Verifier = Callable[[str, str | None], dict[str, Any]]  # (goal_id, snapshot path) -> report


def _terminal(status: str) -> bool:
    return status in {"completed", "failed", "blocked", "stopped", "timeout", "error"}


def _verdict_from(acceptance: dict[str, Any] | None, launcher_status: str,
                  goal_status: str | None, timed_out: bool) -> tuple[str, bool]:
    """Map raw evidence to a mechanical verdict + false-completion flag.

    The acceptance contract is the behavioral authority, but a resumable active
    goal is not complete merely because a partial snapshot already passes. A
    verified outcome therefore requires both accepted behavior and durable goal
    completion.
    """
    claimed_done = launcher_status == "completed"
    if timed_out:
        return Verdict.TIMEOUT.value, False
    if acceptance is not None:
        verdict = acceptance.get("verdict")
        if verdict == "accepted":
            if goal_status == "completed":
                return Verdict.VERIFIED.value, False
            return Verdict.BLOCKED.value, False
        if verdict == "rejected":
            if claimed_done:
                return Verdict.COMPLETE_UNVERIFIED.value, True
            return Verdict.BLOCKED.value, False
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
    started_at = clock()

    def finish() -> GoalResultV2:
        result.elapsed_s = max(0.0, clock() - started_at)
        return result

    try:
        handle = launch(plan)
    except Exception as exc:
        result.outcome = Verdict.HARNESS_ERROR.value
        result.launcher_status = "failed"
        result.terminal_reason = "launch_callback_failed"
        result.error = f"launch failed: {type(exc).__name__}: {exc}"
        return finish()
    result.run_id, result.project_id = handle.run_id, handle.project_id
    if not handle.started:
        result.outcome = Verdict.HARNESS_ERROR.value
        result.launcher_status = "failed"
        result.terminal_reason = handle.error or "launch failed"
        result.error = handle.error
        return finish()

    deadline = started_at + plan.timeout_s
    timed_out = False
    lifecycle_error: str | None = None
    manifest: dict[str, Any] = {}
    while True:
        try:
            manifest = poll(handle.project_id, handle.run_id)
        except Exception as exc:
            lifecycle_error = f"poll failed: {type(exc).__name__}: {exc}"
            try:
                terminate(handle.project_id, handle.run_id, "eval_poll_failed")
            except Exception as terminate_exc:
                lifecycle_error += (
                    f"; termination failed: {type(terminate_exc).__name__}: {terminate_exc}"
                )
            manifest = {"status": "error", "terminal_reason": "poll_callback_failed"}
            break
        status = manifest.get("status", "")
        if _terminal(status):
            break
        if clock() >= deadline:
            timed_out = True
            try:
                terminate(handle.project_id, handle.run_id, f"eval_timeout_{int(plan.timeout_s)}s")
            except Exception as exc:
                lifecycle_error = f"termination failed: {type(exc).__name__}: {exc}"
            try:
                manifest = poll(handle.project_id, handle.run_id)
            except Exception as exc:
                lifecycle_error = lifecycle_error or f"post-terminate poll failed: {type(exc).__name__}: {exc}"
                manifest = {"status": "error", "terminal_reason": "timeout_poll_failed"}
            break
        sleep(poll_interval)

    result.launcher_status = manifest.get("status")
    result.goal_status = manifest.get("goal_status")
    for field in ("turns_used", "batches_used", "cycles_to_done", "distance_auc", "token_cost"):
        value = manifest.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(result, field, value)
    result.terminal_reason = manifest.get("terminal_reason") or (
        f"eval_timeout_{int(plan.timeout_s)}s" if timed_out else None)

    # Flag impossible lifecycle pairs (completed launcher + unfinished goal) but
    # do not treat a legitimately resumable (blocked + active) pair as corruption.
    if result.launcher_status and not is_legal_state_pair(result.launcher_status, result.goal_status):
        result.infrastructure_verdict = "inconsistent"
        result.error = state_pair_reason(result.launcher_status, result.goal_status)

    try:
        snapshot_value = snapshot(handle.project_id)
    except Exception as exc:  # a verifier/snapshot outage is harness evidence
        result.infrastructure_verdict = "harness_error"
        result.error = f"snapshot export failed: {type(exc).__name__}: {exc}"
        result.outcome = Verdict.HARNESS_ERROR.value
        result.terminal_reason = result.terminal_reason or "snapshot_export_failed"
        return finish()
    if isinstance(snapshot_value, SnapshotArtifact):
        result.snapshot_digest = snapshot_value.digest
        verification_path = snapshot_value.path
    else:
        # String snapshots remain supported for injected/unit-test adapters. A
        # production adapter returns SnapshotArtifact so the digest and the
        # path that was actually judged cannot drift apart.
        result.snapshot_digest = snapshot_value
        verification_path = snapshot_value
    try:
        acceptance = verify(plan.goal_id, verification_path)
    except Exception as exc:  # contract failures must not kill later goals
        acceptance = {"verdict": "error", "reason": f"verifier raised: {type(exc).__name__}: {exc}"}
    result.acceptance = acceptance
    result.acceptance_verdict = acceptance.get("verdict") if acceptance else None
    outcome, false_completion = _verdict_from(
        acceptance, result.launcher_status or "", result.goal_status, timed_out)
    result.outcome, result.false_completion = outcome, false_completion
    if lifecycle_error is not None:
        result.infrastructure_verdict = "harness_error"
        result.error = lifecycle_error
        result.outcome = Verdict.HARNESS_ERROR.value
    if result.measurement_unavailable_reason is None:
        missing = [field for field in ("turns_used", "cycles_to_done", "distance_auc", "token_cost")
                   if getattr(result, field) is None]
        result.measurement_unavailable_reason = (
            "manifest did not expose convergence metrics: " + ", ".join(missing)
            if missing else None
        )
    return finish()


def suite_exit_code(report: Mapping[str, Any]) -> int:
    """Return success only when preflight passed and every selected goal completed."""
    preflight = report.get("preflight")
    if isinstance(preflight, Mapping) and not preflight.get("ok", False):
        return 1
    baseline_status = report.get("baseline_status")
    if baseline_status is not None and baseline_status != "created":
        return 1
    total = report.get("total")
    return 0 if isinstance(total, int) and total > 0 and report.get("completed") == total else 1


def suite_definition_hash(
    goals: Iterable[tuple[str, str]],
    *,
    suite_label: str = "",
) -> str:
    """Hash the real goal catalog and metric semantics used for comparison."""

    import json

    payload = {
        "suite_label": suite_label,
        "goals": list(goals),
        "metric_semantics": METRIC_SEMANTICS_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def run_suite(*, goals: Iterable[tuple[str, str]], model: str | None,
              base_url: str | None, timeout: float, max_turns: int,
              output: Path, only: int | None = None,
              configure: Callable[[], None] | None = None,
              preflight: Callable[[], Mapping[str, Any]] | None = None,
              create_project: Callable[[GoalPlan], tuple[str, str]] | None = None,
              launch: Launcher | None = None, poll: StatusPoller | None = None,
              terminate: Terminator | None = None,
              snapshot: Snapshotter | None = None, verify: Verifier | None = None,
              lease_path: Path | None = None,
              suite_hash: str = "unknown", sandbox_image_digest: str | None = None,
              verifier_image_digest: str | None = None,
              sandbox_mode: str | None = None,
              reasoning_effort: str | None = None,
              llm_request_timeout_s: float | None = None,
              llm_max_retries: int | None = None,
              require_gateable: bool = True) -> dict[str, Any]:
    """Run a complete suite through the canonical lifecycle.

    CLI modules provide only goal catalogues and these narrow infrastructure
    callbacks. This function owns the lease, preflight refusal, per-goal
    orchestration, atomic checkpointing, and V2 result shape.
    """
    suite_clock_started = time.monotonic()
    if configure:
        configure()
    report = SuiteResultV2(
        model=model,
        environment=build_environment(
            model=model, base_url=base_url, max_turns=max_turns,
            suite_hash=suite_hash, sandbox_image_digest=sandbox_image_digest,
            verifier_image_digest=verifier_image_digest, sandbox_mode=sandbox_mode,
            reasoning_effort=reasoning_effort,
            llm_request_timeout_s=llm_request_timeout_s,
            llm_max_retries=llm_max_retries,
        ),
        started_at=_utcnow(),
        measurement_unavailable_reason="durable convergence ledger not wired",
    )
    goal_catalog = list(goals)
    selected = goal_catalog if only is None else [goal_catalog[only - 1]]
    report.environment.replicate_count = 1
    baseline_target = output.name == "baseline-v2.json"
    checkpoint_path = (output.with_name("baseline-v2.partial.json")
                       if baseline_target else output)

    if not all((launch, poll, terminate, snapshot, verify, create_project)):
        raise ValueError("production suite requires launch, poll, terminate, snapshot, verify, and create_project callbacks")

    preflight_report: dict[str, Any] | None = None
    with EvaluationLease(lease_path or EvaluationLease.default_path()):
        reconciled = reconcile_stale_manifests()
        if preflight:
            preflight_report = dict(preflight())
            preflight_report["reconciled_stale_projects"] = reconciled
            if not preflight_report.get("ok", False):
                report.ended_at = _utcnow()
                report.elapsed_s = time.monotonic() - suite_clock_started
                report.recompute_aggregates()
                payload = _report_payload(report)
                payload["preflight"] = preflight_report
                payload["baseline_status"] = (
                    "not_created_preflight_failed" if baseline_target else None
                )
                write_result_atomic(checkpoint_path, payload)
                return payload
        for index, (slug, goal) in enumerate(selected, start=1):
            plan = GoalPlan(
                goal_id=slug, slug=slug, goal=goal, timeout_s=float(timeout),
            )
            project_name = ""
            project_id = ""

            def launch_one(current: GoalPlan) -> LaunchHandle:
                nonlocal project_name, project_id
                project_name, project_id = create_project(current)
                launched = launch(current)
                if launched.project_id != project_id:
                    launched.project_id = project_id
                return launched

            def verify_one(goal_id: str, snapshot_path: str | None) -> dict[str, Any]:
                if snapshot_path is None:
                    return {"verdict": "unverifiable", "reason": "no authoritative snapshot exported"}
                result = verify(goal_id, snapshot_path)
                if require_gateable and not result.get("gateable", False):
                    result = {**result, "verdict": "error",
                              "reason": "acceptance runner is not gateable: container verifier required"}
                return result

            goal_result = orchestrate_goal(
                plan, launch=launch_one, poll=poll, terminate=terminate,
                snapshot=snapshot, verify=verify_one,
            )
            report.goals.append(goal_result)
            report.recompute_aggregates()
            checkpoint = _report_payload(report)
            if preflight_report is not None:
                checkpoint["preflight"] = preflight_report
            checkpoint["last_project"] = project_name
            checkpoint["last_project_id"] = project_id
            write_result_atomic(checkpoint_path, checkpoint)

    report.ended_at = _utcnow()
    report.elapsed_s = time.monotonic() - suite_clock_started
    report.recompute_aggregates()
    payload = _report_payload(report)
    if preflight_report is not None:
        payload["preflight"] = preflight_report
    if baseline_target:
        required = ("mean_cycles_to_done", "mean_distance_auc", "token_cost")
        qualifying = (
            len(selected) == len(goal_catalog) == 8
            and all((goal.get("acceptance") or {}).get("gateable") is True for goal in payload["goals"])
            and all(goal.get("outcome") == Verdict.VERIFIED.value for goal in payload["goals"])
            and not any(goal.get("outcome") == Verdict.HARNESS_ERROR.value for goal in payload["goals"])
            and all(payload.get(field) is not None for field in required)
        )
        if qualifying:
            payload["baseline_status"] = "created"
            write_result_atomic(output, payload)
            checkpoint_path.unlink(missing_ok=True)
        else:
            payload["baseline_status"] = "not_created_qualification_failed"
            write_result_atomic(checkpoint_path, payload)
    else:
        write_result_atomic(output, payload)
    return payload


def _durable_goal_status(project_id: str) -> str | None:
    try:
        from app.database import SessionLocal
        from app.models import ForgeGoal

        with SessionLocal() as db:
            row = (db.query(ForgeGoal).filter(ForgeGoal.project_id == project_id)
                   .order_by(ForgeGoal.created_at.desc()).first())
            return row.status if row else None
    except Exception:
        return None


def reconcile_stale_manifests() -> list[str]:
    """Mark dead non-terminal manifests stopped before a new suite starts."""
    from pge_launcher import load_run_state, process_is_alive, update_run

    reconciled: list[str] = []
    for project_id, record in load_run_state().items():
        if record.get("status") not in {"launching", "starting", "running", "recovering"}:
            continue
        if process_is_alive(record.get("pid")):
            continue
        run_id = record.get("run_id")
        if not run_id:
            continue
        try:
            if update_run(project_id, run_id, status="stopped",
                          terminal_reason="stale_manifest_reconciled",
                          finished_at=_utcnow()):
                reconciled.append(str(project_id))
        except Exception:
            # The manifest remains visible; inability to mirror the event to
            # Postgres must not prevent the preflight from reporting the run.
            continue
    return reconciled


def production_callbacks(*, verifier_image: str | None = None) -> dict[str, Callable[..., Any]]:
    """Return the real launcher/snapshot/verifier callbacks.

    Kept in the orchestrator so every CLI uses the same authoritative path.
    The callbacks intentionally fail closed when Docker or the database cannot
    provide the project snapshot; no host verifier fallback is permitted.
    """
    from app.database import SessionLocal
    from app.services import MemoryService
    from forge_runtime.sandbox import get_workspace
    from pge_launcher import launch_pge, load_run_state, process_is_alive, terminate_run

    def create_project(plan: GoalPlan) -> tuple[str, str]:
        name = f"suite-{plan.slug}-{uuid.uuid4().hex[:8]}"
        repo_path = str(forge_config.workspaces_root() / name)
        with SessionLocal() as db:
            project = MemoryService(db).create_project(name, repo_path)
            MemoryService(db).create_goal(
                project_id=project.id, title=plan.goal, description=plan.goal,
            )
            return name, project.id

    def poll(project_id: str, run_id: str) -> dict[str, Any]:
        manifest = dict(load_run_state().get(project_id, {}))
        if manifest.get("run_id") != run_id:
            return {"status": "failed", "goal_status": _durable_goal_status(project_id),
                    "failure": "run manifest missing or replaced"}
        status = manifest.get("status", "")
        if status in {"launching", "starting", "running", "recovering"} and not process_is_alive(manifest.get("pid")):
            manifest.setdefault("failure", "launcher process is no longer alive")
            manifest["status"] = "failed"
        manifest["goal_status"] = _durable_goal_status(project_id)
        return manifest

    def terminate(project_id: str, run_id: str, reason: str) -> dict[str, Any]:
        return terminate_run(project_id, run_id, reason, status="timeout")

    def snapshot(project_id: str) -> SnapshotArtifact:
        from app.models import ForgeProject

        with SessionLocal() as db:
            project = db.query(ForgeProject).filter(ForgeProject.id == project_id).first()
            if project is None:
                raise RuntimeError(f"project {project_id} not found")
            repo_path = project.repo_path
        workspace = get_workspace(project_id, repo_path)
        dest = forge_config.home() / "eval_snapshots" / f"{project_id}-{uuid.uuid4().hex[:8]}"
        exported = workspace.export_snapshot(str(dest))
        return SnapshotArtifact(path=exported.path, digest=exported.digest)

    def verify(goal_id: str, snapshot_path: str | None) -> dict[str, Any]:
        from evals.acceptance import ContainerRunner, verify_goal

        if snapshot_path is None:
            return {"verdict": "unverifiable", "reason": "snapshot path is missing", "gateable": True}
        return verify_goal(
            goal_id,
            snapshot_path,
            runner=ContainerRunner(
                image=verifier_image
                or os.getenv("FORGE_VERIFIER_IMAGE", "forge-sandbox:latest")
            ),
        )

    return {
        "create_project": create_project, "poll": poll, "terminate": terminate,
        "snapshot": snapshot, "verify": verify, "launch_pge": launch_pge,
    }


def run_production_suite(*, goals: Iterable[tuple[str, str]], model: str,
                         base_url: str, timeout: float, max_turns: int,
                         output: Path, only: int | None = None,
                         child_env: Mapping[str, str] | None = None,
                         suite_hash: str = "unknown",
                         verify_override: Verifier | None = None,
                         preflight_override: Callable[[], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Production entry point used by all suite CLIs."""
    goal_catalog = list(goals)
    suite_hash = suite_definition_hash(goal_catalog, suite_label=suite_hash)
    sandbox_tag = os.getenv("FORGE_SANDBOX_IMAGE", "forge-sandbox:latest")
    verifier_tag = os.getenv("FORGE_VERIFIER_IMAGE", sandbox_tag)
    # Resolve mutable tags exactly once. The immutable IDs are used by
    # preflight, every sandbox/verifier invocation, and the fingerprint, so a
    # concurrent rebuild cannot make later goals execute unrecorded images.
    sandbox_digest = image_digest(sandbox_tag)
    verifier_digest = image_digest(verifier_tag)
    sandbox_image = sandbox_digest or sandbox_tag
    verifier_image = verifier_digest or verifier_tag
    adapters = production_callbacks(verifier_image=verifier_image)
    from forge_runtime.sandbox import sandbox_mode as configured_sandbox_mode

    create_project = adapters["create_project"]
    launch_pge = adapters["launch_pge"]
    current_project: dict[str, str] = {}

    def create(plan: GoalPlan) -> tuple[str, str]:
        value = create_project(plan)
        current_project.clear()
        current_project.update(name=value[0], id=value[1])
        return value

    def launch(plan: GoalPlan) -> LaunchHandle:
        launch_env = {
            **(child_env or {}),
            "FORGE_EVAL_GOAL_SLUG": plan.slug,
            "FORGE_SANDBOX_IMAGE": sandbox_image,
            "FORGE_VERIFIER_IMAGE": verifier_image,
        }
        launched = launch_pge(current_project["id"], source="eval-orchestrator",
                              invocation={"goal": plan.goal, "slug": plan.slug},
                              env=launch_env)
        return LaunchHandle(
            run_id=str(launched.get("run_id", "")), project_id=current_project["id"],
            started=launched.get("status") == "success" and launched.get("started", True),
            error=launched.get("message"),
        )

    unresolved_images = [
        tag
        for tag, digest in (
            (sandbox_tag, sandbox_digest),
            (verifier_tag, verifier_digest),
        )
        if digest is None
    ]
    if unresolved_images:
        # A qualifying run may never fall back to mutable tags. Persist a
        # blocking preflight result instead of launching with null/misleading
        # comparability evidence.
        preflight = lambda: {
            "ok": False,
            "checks": [{
                "name": "immutable_image_resolution",
                "ok": False,
                "detail": "could not resolve immutable Docker image ID for: "
                + ", ".join(unresolved_images),
                "blocking": True,
            }],
        }
    else:
        preflight = preflight_override or (lambda: __import__("evals.preflight", fromlist=["run_preflight"]).run_preflight(
            database_url=forge_config.database_url(), base_url=base_url, model=model,
            sandbox_image=sandbox_image, verifier_image=verifier_image,
            require_docker=True, require_container_sandbox=True,
        ).as_dict())
    return run_suite(
        goals=goal_catalog, model=model, base_url=base_url, timeout=timeout,
        max_turns=max_turns, output=output, only=only,
        preflight=preflight, create_project=create, launch=launch,
        poll=adapters["poll"], terminate=adapters["terminate"],
        snapshot=adapters["snapshot"], verify=verify_override or adapters["verify"],
        suite_hash=suite_hash,
        sandbox_image_digest=sandbox_digest,
        verifier_image_digest=verifier_digest,
        sandbox_mode=configured_sandbox_mode(),
        reasoning_effort=(child_env or {}).get("FORGE_LLM_REASONING_EFFORT"),
        llm_request_timeout_s=float(
            (child_env or {}).get("FORGE_LLM_TIMEOUT", forge_config.DEFAULT_TIMEOUT)
        ),
        llm_max_retries=int((child_env or {}).get("FORGE_LLM_MAX_RETRIES", "0")),
        require_gateable=True,
    )
