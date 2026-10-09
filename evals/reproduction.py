"""Fail-before/pass-after evidence for bug-fix acceptance.

The normal acceptance contracts prove that a completed artifact behaves
correctly.  Bug-fix benchmarks need one additional fact: the regression test
must distinguish the candidate patch from the pre-patch baseline.  This module
replays an explicit test command against isolated baseline and candidate
workspaces, optionally copying the candidate's test patch into the baseline in
the same way SWT-Bench applies generated tests to the buggy revision.

Coverage is evidence, not authority.  When ``coverage_json`` is configured we
read coverage.py's JSON output from both runs and record the delta; a missing or
malformed report stays ``null`` with an explicit reason and never becomes a
favorable zero.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ReproductionSpec:
    """Exact command and test patch used to reproduce a reported defect."""

    command: tuple[str, ...]
    test_paths: tuple[str, ...] = ()
    expect_exit: int = 0
    expect_substring: str = ""
    coverage_json: str | None = None
    timeout_s: int = 120

    @classmethod
    def parse(cls, value: "ReproductionSpec | Mapping[str, Any]") -> "ReproductionSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("reproduction must be a ReproductionSpec or mapping")

        raw_command = value.get("command")
        if not isinstance(raw_command, Sequence) or isinstance(raw_command, (str, bytes)):
            raise ValueError("reproduction command must be a non-empty argv list")
        command = tuple(raw_command)
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("reproduction command must contain non-empty strings")

        raw_paths = value.get("test_paths", ())
        if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
            raise ValueError("reproduction test_paths must be a list of relative paths")
        test_paths = tuple(raw_paths)
        if any(not isinstance(path, str) or not path for path in test_paths):
            raise ValueError("reproduction test_paths must contain non-empty strings")

        expect_exit = value.get("expect_exit", 0)
        timeout_s = value.get("timeout_s", 120)
        if not isinstance(expect_exit, int) or isinstance(expect_exit, bool):
            raise ValueError("reproduction expect_exit must be an integer")
        if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError("reproduction timeout_s must be a positive integer")

        expect_substring = value.get("expect_substring", "")
        coverage_json = value.get("coverage_json")
        if not isinstance(expect_substring, str):
            raise ValueError("reproduction expect_substring must be a string")
        if coverage_json is not None and (
            not isinstance(coverage_json, str) or not coverage_json
        ):
            raise ValueError("reproduction coverage_json must be a non-empty relative path")

        return cls(
            command=command,
            test_paths=test_paths,
            expect_exit=expect_exit,
            expect_substring=expect_substring,
            coverage_json=coverage_json,
            timeout_s=timeout_s,
        )


@dataclass(frozen=True)
class ReproductionRun:
    passed: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    executed_command: list[str] | None
    coverage_percent: float | None = None
    coverage_unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": _clip(self.stdout),
            "stderr": _clip(self.stderr),
            "executed_command": self.executed_command,
            "coverage_percent": self.coverage_percent,
            "coverage_unavailable_reason": self.coverage_unavailable_reason,
        }


@dataclass(frozen=True)
class ReproductionReport:
    status: str
    reason: str
    command: tuple[str, ...]
    test_paths: tuple[str, ...]
    baseline: ReproductionRun | None
    candidate: ReproductionRun | None
    coverage_delta: float | None = None
    coverage_unavailable_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "reproduced"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "reason": self.reason,
            "command": list(self.command),
            "test_paths": list(self.test_paths),
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "coverage_delta": self.coverage_delta,
            "coverage_unavailable_reason": self.coverage_unavailable_reason,
        }


CommandRunner = Callable[
    [list[str], Path, int],
    subprocess.CompletedProcess,
]
CommandRenderer = Callable[[list[str], Path], list[str]]


def _clip(text: str, limit: int = 4000) -> str:
    if len(text or "") > limit:
        return (text or "")[:limit] + f"\n...[{len(text) - limit} bytes truncated]"
    return text or ""


def _safe_path(root: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise ValueError(f"reproduction path must be relative: {relative!r}")
    target = (root / raw).resolve(strict=False)
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"reproduction path escapes workspace: {relative!r}") from exc
    return target


def _stage_test_patch(candidate_root: Path, baseline_root: Path, paths: tuple[str, ...]) -> None:
    for relative in paths:
        source = _safe_path(candidate_root, relative)
        target = _safe_path(baseline_root, relative)
        if not source.is_file():
            raise ValueError(f"reproduction test path is not a file: {relative!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _coverage(root: Path, relative: str | None) -> tuple[float | None, str | None]:
    if relative is None:
        return None, "coverage_json not configured"
    report_path = _safe_path(root, relative)
    if not report_path.is_file():
        return None, f"coverage report missing: {relative}"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        value = payload["totals"]["percent_covered"]
        percent = float(value)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return None, f"invalid coverage.py JSON at {relative}: {type(exc).__name__}: {exc}"
    if not math.isfinite(percent) or not 0.0 <= percent <= 100.0:
        return None, f"invalid coverage percentage at {relative}: {percent!r}"
    return percent, None


def _run_once(
    root: Path,
    spec: ReproductionSpec,
    run_command: CommandRunner,
    render_command: CommandRenderer,
) -> ReproductionRun:
    if spec.coverage_json:
        coverage_path = _safe_path(root, spec.coverage_json)
        coverage_path.unlink(missing_ok=True)
    command = list(spec.command)
    rendered = render_command(command, root)
    try:
        proc = run_command(command, root, spec.timeout_s)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        coverage_percent, coverage_reason = _coverage(root, spec.coverage_json)
        return ReproductionRun(
            passed=False,
            exit_code=None,
            timed_out=True,
            stdout=stdout,
            stderr=stderr,
            executed_command=rendered,
            coverage_percent=coverage_percent,
            coverage_unavailable_reason=coverage_reason,
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = stdout + stderr
    passed = proc.returncode == spec.expect_exit and (
        spec.expect_substring in combined if spec.expect_substring else True
    )
    coverage_percent, coverage_reason = _coverage(root, spec.coverage_json)
    return ReproductionRun(
        passed=passed,
        exit_code=proc.returncode,
        timed_out=False,
        stdout=stdout,
        stderr=stderr,
        executed_command=getattr(proc, "executed_command", rendered),
        coverage_percent=coverage_percent,
        coverage_unavailable_reason=coverage_reason,
    )


def evaluate_reproduction(
    *,
    baseline_root: Path,
    candidate_root: Path,
    spec: ReproductionSpec | Mapping[str, Any],
    run_command: CommandRunner,
    render_command: CommandRenderer,
) -> ReproductionReport:
    """Return mechanical fail-before/pass-after evidence for one test command."""

    parsed = ReproductionSpec.parse(spec)
    baseline_root = baseline_root.resolve()
    candidate_root = candidate_root.resolve()
    _stage_test_patch(candidate_root, baseline_root, parsed.test_paths)

    candidate = _run_once(candidate_root, parsed, run_command, render_command)
    if not candidate.passed:
        return ReproductionReport(
            status="candidate_failed",
            reason="regression test did not pass on the candidate patch",
            command=parsed.command,
            test_paths=parsed.test_paths,
            baseline=None,
            candidate=candidate,
            coverage_unavailable_reason=candidate.coverage_unavailable_reason,
        )

    baseline = _run_once(baseline_root, parsed, run_command, render_command)
    coverage_delta = None
    coverage_reason = None
    if baseline.coverage_percent is not None and candidate.coverage_percent is not None:
        coverage_delta = candidate.coverage_percent - baseline.coverage_percent
    else:
        coverage_reason = "; ".join(
            reason
            for reason in (
                f"baseline: {baseline.coverage_unavailable_reason}"
                if baseline.coverage_unavailable_reason else None,
                f"candidate: {candidate.coverage_unavailable_reason}"
                if candidate.coverage_unavailable_reason else None,
            )
            if reason
        ) or "coverage was not measured"

    if baseline.passed:
        status = "not_reproduced"
        reason = "regression test also passed on the pre-patch baseline"
    else:
        status = "reproduced"
        reason = "regression test failed before the patch and passed after it"
    return ReproductionReport(
        status=status,
        reason=reason,
        command=parsed.command,
        test_paths=parsed.test_paths,
        baseline=baseline,
        candidate=candidate,
        coverage_delta=coverage_delta,
        coverage_unavailable_reason=coverage_reason,
    )
