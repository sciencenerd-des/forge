"""Phase 2: lifecycle ownership.

Covers real process-group termination (TERM honored, TERM ignored then KILL,
already-dead), the fcntl evaluation lease, and the orchestrator's timeout /
snapshot / legal-state behavior with injected fakes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import pge_launcher
from evals.contracts import Verdict
from evals.orchestrator import (
    EvaluationLease,
    GoalPlan,
    LaunchHandle,
    LeaseHeld,
    SnapshotArtifact,
    orchestrate_goal,
    run_suite,
    suite_exit_code,
)

# --------------------------------------------------------------------------- #
# Real process-group termination
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ({"preflight": {"ok": False}, "completed": 0, "total": 0}, 1),
        ({"completed": 0, "total": 0}, 1),
        ({"preflight": {"ok": True}, "completed": 1, "total": 1}, 0),
        ({"preflight": {"ok": True}, "completed": 0, "total": 1}, 1),
    ],
)
def test_suite_exit_code_requires_a_real_completed_run(report, expected):
    assert suite_exit_code(report) == expected


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def test_terminate_group_terminates_cooperative_child():
    proc = _spawn("import time; time.sleep(30)")
    try:
        disposition = pge_launcher.terminate_process_group(proc.pid, proc.pid, grace_seconds=5)
        assert disposition == "terminated"
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_group_escalates_to_kill_when_term_ignored():
    proc = _spawn("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "time.sleep(30)")
    try:
        time.sleep(1.0)  # let the child install its SIGTERM-ignoring handler
        disposition = pge_launcher.terminate_process_group(proc.pid, proc.pid, grace_seconds=0.5)
        assert disposition == "killed"
        # reaped inside terminate_process_group
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_group_already_dead():
    assert pge_launcher.terminate_process_group(2_000_000_000, 2_000_000_000) == "already_dead"
    assert pge_launcher.terminate_process_group(None, None) == "already_dead"


def test_terminate_run_finalizes_manifest_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(pge_launcher, "RUN_STATE", tmp_path / "runs.json")
    monkeypatch.setattr(pge_launcher, "RUN_DIR", tmp_path)
    monkeypatch.setattr(pge_launcher, "_persist_lifecycle", lambda *a, **k: None)
    proc = _spawn("import time; time.sleep(30)")
    try:
        pge_launcher.save_run_state({"p1": {"run_id": "r1", "pid": proc.pid,
                                            "process_group": proc.pid, "status": "running"}})
        out = pge_launcher.terminate_run("p1", "r1", "eval_timeout_1s", grace_seconds=5,
                                         status="timeout")
        assert out["status"] == "timeout"
        assert out["termination"] in {"terminated", "killed"}
        state = pge_launcher.load_run_state()["p1"]
        assert state["status"] == "timeout"
        assert state["terminal_reason"] == "eval_timeout_1s"
        assert "finished_at" in state
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_run_rejects_stale_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(pge_launcher, "RUN_STATE", tmp_path / "runs.json")
    monkeypatch.setattr(pge_launcher, "RUN_DIR", tmp_path)
    monkeypatch.setattr(pge_launcher, "_persist_lifecycle", lambda *a, **k: None)
    pge_launcher.save_run_state({"p1": {"run_id": "r1", "status": "running"}})
    out = pge_launcher.terminate_run("p1", "OTHER", "x")
    assert out["status"] == "not_found"


# --------------------------------------------------------------------------- #
# Evaluation lease
# --------------------------------------------------------------------------- #
def test_lease_is_exclusive(tmp_path):
    path = tmp_path / "eval.lock"
    lease1 = EvaluationLease(path=path).acquire()
    try:
        with pytest.raises(LeaseHeld):
            EvaluationLease(path=path).acquire()
    finally:
        lease1.release()
    # Released: a fresh acquire now succeeds.
    lease2 = EvaluationLease(path=path).acquire()
    lease2.release()


def test_lease_records_owner(tmp_path):
    path = tmp_path / "eval.lock"
    with EvaluationLease(path=path):
        assert f"pid={os.getpid()}" in path.read_text()


# --------------------------------------------------------------------------- #
# Orchestrator with injected fakes
# --------------------------------------------------------------------------- #
def _handle(started=True, error=None):
    return LaunchHandle(run_id="r1", project_id="p1", started=started, error=error)


def _plan(timeout=10.0):
    return GoalPlan(goal_id="lru", slug="lru", goal="build an LRU", timeout_s=timeout)


def test_normal_completion_is_verified():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "completed", "goal_status": "completed"},
        terminate=lambda *a: {"status": "stopped"},
        snapshot=lambda pid: "digest-abc",
        verify=lambda gid, snap: {"verdict": "accepted", "reason": "ok"},
    )
    assert result.outcome == Verdict.VERIFIED.value
    assert result.snapshot_digest == "digest-abc"
    assert result.false_completion is False


def test_startup_failure_is_harness_error():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(started=False, error="runner exited during startup"),
        poll=lambda pid, rid: {},
        terminate=lambda *a: {},
        snapshot=lambda pid: None,
        verify=lambda gid, snap: None,
    )
    assert result.outcome == Verdict.HARNESS_ERROR.value
    assert "startup" in (result.terminal_reason or "")


def test_timeout_terminates_and_still_verifies():
    clock = iter([0.0, 100.0, 100.0, 100.0])
    terminated = {}

    def fake_terminate(pid, rid, reason):
        terminated["reason"] = reason
        return {"status": "timeout"}

    result = orchestrate_goal(
        _plan(timeout=10.0),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "running", "goal_status": "active"},
        terminate=fake_terminate,
        snapshot=lambda pid: "partial-digest",
        verify=lambda gid, snap: {"verdict": "rejected", "reason": "incomplete"},
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )
    assert result.outcome == Verdict.TIMEOUT.value
    assert terminated["reason"].startswith("eval_timeout_")
    assert result.snapshot_digest == "partial-digest"  # snapshot authority even on timeout


def test_rejected_completion_claim_is_false_completion():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "completed", "goal_status": "completed"},
        terminate=lambda *a: {},
        snapshot=lambda pid: "d",
        verify=lambda gid, snap: {"verdict": "rejected", "reason": "stub get/put"},
    )
    assert result.outcome == Verdict.COMPLETE_UNVERIFIED.value
    assert result.false_completion is True


def test_illegal_state_pair_flags_inconsistent():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "completed", "goal_status": "active"},
        terminate=lambda *a: {},
        snapshot=lambda pid: "d",
        verify=lambda gid, snap: {"verdict": "accepted", "reason": "ok"},
    )
    assert result.infrastructure_verdict == "inconsistent"


def test_blocked_run_with_active_goal_is_not_inconsistent():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "blocked", "goal_status": "active"},
        terminate=lambda *a: {},
        snapshot=lambda pid: "d",
        verify=lambda gid, snap: {"verdict": "rejected", "reason": "not done"},
    )
    assert result.infrastructure_verdict is None  # resumable, legitimate


def test_stopped_active_goal_is_not_verified_by_a_partial_passing_snapshot():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {
            "status": "stopped",
            "goal_status": "active",
            "terminal_reason": "operator_stop",
        },
        terminate=lambda *a: {},
        snapshot=lambda pid: "d",
        verify=lambda gid, snap: {"verdict": "accepted", "reason": "partial works"},
    )

    assert result.acceptance_verdict == "accepted"
    assert result.outcome == Verdict.BLOCKED.value


def test_stopped_active_goal_with_rejected_snapshot_remains_blocked():
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: {"status": "stopped", "goal_status": "active"},
        terminate=lambda *a: {},
        snapshot=lambda pid: "d",
        verify=lambda gid, snap: {"verdict": "rejected", "reason": "partial"},
    )

    assert result.outcome == Verdict.BLOCKED.value
    assert result.false_completion is False


def test_poll_failure_best_effort_terminates_and_becomes_harness_error():
    terminated = []
    result = orchestrate_goal(
        _plan(),
        launch=lambda p: _handle(),
        poll=lambda pid, rid: (_ for _ in ()).throw(RuntimeError("manifest unavailable")),
        terminate=lambda pid, rid, reason: terminated.append((pid, rid, reason)) or {},
        snapshot=lambda pid: SnapshotArtifact("/snapshot", "digest"),
        verify=lambda gid, snap: {"verdict": "accepted", "reason": "ok", "gateable": True},
    )

    assert terminated == [("p1", "r1", "eval_poll_failed")]
    assert result.outcome == Verdict.HARNESS_ERROR.value
    assert result.infrastructure_verdict == "harness_error"
    assert "poll failed" in (result.error or "")
    assert result.snapshot_digest == "digest"


def test_eight_verified_goals_with_metrics_promote_baseline(tmp_path):
    goals = [(f"g{i}", f"goal {i}") for i in range(8)]
    projects = iter((f"name-{i}", f"p{i}") for i in range(8))
    output = tmp_path / "baseline-v2.json"

    report = run_suite(
        goals=goals,
        model="m",
        base_url="http://localhost:1234/v1",
        timeout=1,
        max_turns=2,
        output=output,
        preflight=lambda: {"ok": True, "checks": []},
        create_project=lambda plan: next(projects),
        launch=lambda plan: LaunchHandle("run", "placeholder", True),
        poll=lambda pid, rid: {
            "status": "completed",
            "goal_status": "completed",
            "turns_used": 2,
            "batches_used": 1,
            "cycles_to_done": 1,
            "distance_auc": 0.0,
            "token_cost": 10,
        },
        terminate=lambda *args: {},
        snapshot=lambda pid: SnapshotArtifact(f"/{pid}", f"digest-{pid}"),
        verify=lambda gid, snap: {"verdict": "accepted", "reason": "ok", "gateable": True},
        lease_path=tmp_path / "eval.lock",
        suite_hash="suite",
        sandbox_image_digest="sha256:image",
    )

    assert report["baseline_status"] == "created"
    assert output.exists()
    assert not (tmp_path / "baseline-v2.partial.json").exists()
    assert report["mean_cycles_to_done"] == 1.0
    assert report["mean_distance_auc"] == 0.0
    assert report["token_cost"] == 80


def test_model_failure_never_promotes_baseline(tmp_path):
    goals = [(f"g{i}", f"goal {i}") for i in range(8)]
    projects = iter((f"name-{i}", f"p{i}") for i in range(8))
    output = tmp_path / "baseline-v2.json"

    report = run_suite(
        goals=goals,
        model="m",
        base_url="http://localhost:1234/v1",
        timeout=1,
        max_turns=2,
        output=output,
        preflight=lambda: {"ok": True},
        create_project=lambda plan: next(projects),
        launch=lambda plan: LaunchHandle("run", "placeholder", True),
        poll=lambda pid, rid: {
            "status": "completed", "goal_status": "completed", "turns_used": 2,
            "cycles_to_done": 1, "distance_auc": 0.5, "token_cost": 10,
        },
        terminate=lambda *args: {},
        snapshot=lambda pid: SnapshotArtifact(f"/{pid}", f"digest-{pid}"),
        verify=lambda gid, snap: {
            "verdict": "rejected" if gid == "g7" else "accepted",
            "reason": "fixture", "gateable": True,
        },
        lease_path=tmp_path / "eval.lock",
        sandbox_image_digest="sha256:image",
    )

    assert report["baseline_status"] == "not_created_qualification_failed"
    assert not output.exists()
    assert (tmp_path / "baseline-v2.partial.json").exists()
