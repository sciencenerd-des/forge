"""Pins Phase 3 of specs/convergent-autonomous-harness.html: verifier hierarchy.

Runs entirely without Docker in CI (degradation path). The real container
replay is exercised only when FORGE_TEST_DOCKER=1 and docker is present.
"""
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from forge_runtime.contract_immune import ContractTest
from forge_runtime.verifier_hierarchy import (
    DEFAULT_STACK_IMAGES,
    docker_available,
    replay,
    snapshot_workspace,
)


def test_degrades_gracefully_when_docker_missing():
    with mock.patch("forge_runtime.verifier_hierarchy.docker_available", return_value=False):
        verdict = replay(Path("."), [ContractTest("t1", "true")])
    assert verdict.tier == "skipped"
    assert verdict.reason == "docker_unavailable"
    assert verdict.passed is False  # caller must treat as complete_unverified, not verified


def test_snapshot_excludes_vcs_internals(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ".git").mkdir()
    (src / "keep.txt").write_text("hi")
    dest = tmp_path / "dest"
    snapshot_workspace(src, dest)
    assert (dest / "keep.txt").exists()
    assert not (dest / ".git").exists()


def test_default_stack_images_cover_templated_stacks():
    for stack in ("python", "node", "cpp", "rust"):
        assert stack in DEFAULT_STACK_IMAGES


def test_replay_honors_contract_exit_and_output_expectations(tmp_path: Path, monkeypatch):
    class _Completed:
        returncode = 0
        stdout = "wrong output"
        stderr = ""

    monkeypatch.setattr("forge_runtime.verifier_hierarchy.docker_available", lambda: True)
    monkeypatch.setattr("forge_runtime.verifier_hierarchy.subprocess.run", lambda *args, **kwargs: _Completed())
    verdict = replay(
        tmp_path,
        [ContractTest("expected-output", "echo wrong", expect_substring="expected")],
    )
    assert verdict.tier == "T2"
    assert not verdict.passed


@pytest.mark.skipif(
    os.environ.get("FORGE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set FORGE_TEST_DOCKER=1 with Docker available to run real container replay",
)
def test_integration_real_docker_replay(tmp_path: Path):
    assert docker_available()
    (tmp_path / "out.txt").write_text("built")
    tests = [ContractTest("artifact-exists", "test -f out.txt")]
    verdict = replay(tmp_path, tests, stack="python")
    assert verdict.tier == "T2"
    assert verdict.passed
    assert verdict.evidence[0].test_id == "artifact-exists"
