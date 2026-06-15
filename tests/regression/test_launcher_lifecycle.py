"""Regression: the detached launcher's liveness check + manifest are sound.

``launch_pge`` spawns the loop with ``start_new_session=True`` (OS-level detach, so
it survives gateway restarts) and tracks a JSON manifest keyed by project. These
test the pure manifest/liveness helpers without spawning a real run.
"""
import os

import pge_launcher


def test_process_is_alive_truth_table():
    assert pge_launcher.process_is_alive(os.getpid()) is True
    assert pge_launcher.process_is_alive(2_000_000_000) is False  # not a real pid
    assert pge_launcher.process_is_alive(None) is False
    assert pge_launcher.process_is_alive("nope") is False


def test_launcher_spawns_with_new_session():
    """The detach flag must be present in the launch call — OS-level detach is
    what lets a detached run outlive the process that started it."""
    import inspect
    src = inspect.getsource(pge_launcher.launch_pge)
    assert "start_new_session=True" in src


def test_manifest_roundtrip(tmp_path, monkeypatch):
    state_file = tmp_path / "runs.json"
    monkeypatch.setattr(pge_launcher, "RUN_STATE", state_file)
    monkeypatch.setattr(pge_launcher, "RUN_DIR", tmp_path)
    pge_launcher.save_run_state({"proj-1": {"run_id": "r1", "status": "running"}})
    loaded = pge_launcher.load_run_state()
    assert loaded["proj-1"]["status"] == "running"
