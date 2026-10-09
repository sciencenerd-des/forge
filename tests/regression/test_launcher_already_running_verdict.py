"""launch_pge's already-running response must keep its "success" verdict.

Failure mode (found during Rust CLI cycle-2 live validation): the early
return spread ``**existing`` after the literal keys, so the manifest's stale
lifecycle status ("running", "stopped") clobbered ``"status": "success"``.
Every caller that gates on the verdict — the A2A task bridge and the
control-plane 409 path — then treated a healthy already-running project as a
failed launch ("run failed to start").
"""

from __future__ import annotations

import pge_launcher


def test_already_running_launch_reports_success(monkeypatch) -> None:
    manifest = {
        "run_id": "existing-run",
        "project_id": "project-1",
        "pid": 4242,
        "status": "running",  # stale lifecycle field that used to clobber the verdict
    }
    monkeypatch.setattr(pge_launcher, "load_run_state", lambda: {"project-1": manifest})
    monkeypatch.setattr(pge_launcher, "process_is_alive", lambda _pid: True)

    result = pge_launcher.launch_pge("project-1", source="test")

    assert result["status"] == "success"
    assert result["already_running"] is True
    assert result["started"] is False
    assert result["run_id"] == "existing-run"


def test_stopped_manifest_status_does_not_leak_into_verdict(monkeypatch) -> None:
    manifest = {"run_id": "r", "project_id": "p", "pid": 1, "status": "stopped"}
    monkeypatch.setattr(pge_launcher, "load_run_state", lambda: {"p": manifest})
    monkeypatch.setattr(pge_launcher, "process_is_alive", lambda _pid: True)

    result = pge_launcher.launch_pge("p", source="test")

    assert result["status"] == "success"
