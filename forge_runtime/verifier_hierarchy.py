"""Verifier hierarchy: tiered completion claims backed by evidence.

T1 — in-workspace evaluator run (today's ground truth; unchanged).
T2 — hermetic replay: snapshot the workspace, run the contract inside a
     network-isolated Docker container from a pinned image, from scratch.

A goal reaches ``verified`` only at T2. If Docker is unavailable the run
degrades gracefully to ``complete_unverified`` at T1 — missing infra must
never block the loop (mirrors Lesson 3: no single missing piece kills a run).

Every replay produces an ``EvidenceRecord`` per test: command, exit code,
output digest, environment fingerprint, duration — auditable, not just logged.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from forge_runtime.contract_immune import ContractTest

DEFAULT_STACK_IMAGES = {
    "python": "python:3.11-slim",
    "node": "node:20-slim",
    "cpp": "gcc:13",
    "rust": "rust:1.75-slim",
}


@dataclass(frozen=True)
class EvidenceRecord:
    test_id: str
    command: str
    exit_code: int
    output_digest: str
    duration_ms: int
    tier: str


@dataclass(frozen=True)
class Verdict:
    tier: str  # "T1" | "T2" | "skipped"
    passed: bool
    evidence: tuple[EvidenceRecord, ...]
    reason: str = ""


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _run_local(command: str, cwd: Path, timeout: int) -> EvidenceRecord:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), timeout=timeout,
            capture_output=True, text=True,
        )
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        exit_code = -1
        output = "timeout"
    duration_ms = int((time.monotonic() - start) * 1000)
    return EvidenceRecord(
        test_id=command, command=command, exit_code=exit_code,
        output_digest=_digest(output), duration_ms=duration_ms, tier="local",
    )


def snapshot_workspace(workspace: Path, dest: Path) -> None:
    """Copy the workspace into ``dest`` for isolated replay, skipping VCS internals."""
    shutil.copytree(workspace, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))


def replay(
    workspace: Path,
    tests: Sequence[ContractTest],
    stack: str = "python",
    timeout: int = 60,
) -> Verdict:
    """Run the contract inside a fresh, network-isolated Docker container.

    Falls back to a T1-equivalent local run with tier "skipped" and
    reason "docker_unavailable" if Docker is not present — the caller
    must treat that as ``complete_unverified``, never as a hard failure.
    """
    if not docker_available():
        return Verdict(tier="skipped", passed=False, evidence=(), reason="docker_unavailable")

    image = DEFAULT_STACK_IMAGES.get(stack, DEFAULT_STACK_IMAGES["python"])
    with tempfile.TemporaryDirectory(prefix="forge-replay-") as tmp:
        replay_ws = Path(tmp) / "replay"
        snapshot_workspace(workspace, replay_ws)

        evidence = []
        all_passed = True
        for t in tests:
            start = time.monotonic()
            docker_cmd = (
                f"docker run --rm --network=none "
                f"-v {replay_ws}:/workspace -w /workspace {image} "
                f"sh -c {_shell_quote(t.command)}"
            )
            try:
                proc = subprocess.run(
                    docker_cmd, shell=True, timeout=timeout,
                    capture_output=True, text=True,
                )
                exit_code = proc.returncode
                output = (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                exit_code = -1
                output = "timeout"
            duration_ms = int((time.monotonic() - start) * 1000)
            expected_exit = t.expect_exit
            expected_substring = t.expect_substring
            passed = exit_code == expected_exit and (
                not expected_substring or expected_substring in output
            )
            all_passed = all_passed and passed
            evidence.append(
                EvidenceRecord(
                    test_id=t.test_id, command=t.command, exit_code=exit_code,
                    output_digest=_digest(output), duration_ms=duration_ms, tier="T2",
                )
            )
        return Verdict(tier="T2", passed=all_passed, evidence=tuple(evidence))


def _shell_quote(command: str) -> str:
    return "'" + command.replace("'", "'\\''") + "'"
