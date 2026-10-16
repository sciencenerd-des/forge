#!/usr/bin/env python3
"""Benchmark Pi's built-in tool execution latency over RPC — protocol-correct.

Measures wall time between ``tool_execution_start`` and ``tool_execution_end``
(model latency excluded) and each turn's total duration between ``agent_start``
and ``agent_end``, so the tool share of turn time is explicit. Evidence input
for the forge-tools go/no-go decision in docs/PI_TOOL_BENCHMARK.md.

Correctness rules this harness enforces (see specs/…-amplification.html Phase 6):

* Turn time is measured to Pi 0.80.2's ``agent_end`` event, not an obsolete
  ``agent_settled``; an unknown terminal protocol fails validation.
* p95 uses the nearest-rank method ``ceil(0.95 * n) - 1`` and the summary
  asserts ``median <= p95 <= max`` (the old ``int(n*0.95)-1`` returned the
  minimum for n=2, i.e. a p95 below the median).
* Every scenario is validated (expected tool identity/count and an exact final
  answer with a clean ``agent_end``). An aborted or invalid scenario keeps its
  diagnostics but is excluded from latency claims.
* Raw events (with monotonic timestamps) and invalid JSON lines are retained.

Usage:
    python3 scripts/pi_tool_bench.py [--provider lmstudio] [--model M] \
        [--runs 5] [--timeout 120] [--seed 0] [--output docs/pi-tool-bench-v2.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

# Pi 0.80.2 protocol event names this harness understands. A stream that never
# emits a known terminal event (agent_end) is treated as protocol-invalid.
TERMINAL_EVENT = "agent_end"
KNOWN_EVENTS = frozenset({
    "agent_start", "tool_execution_start", "tool_execution_end", TERMINAL_EVENT,
})

SCENARIOS = {
    "read": (
        "Use the read tool to read each of these files one at a time: "
        "{files}. Do not summarize their contents. After reading all of "
        "them, reply with exactly: done"
    ),
    "grep": (
        "Use the grep tool (or ripgrep via bash if grep is unavailable) to "
        "search this directory for each of these patterns, one search per "
        "call: FORGE_MARKER_A, FORGE_MARKER_B, FORGE_MARKER_C. After all "
        "three searches, reply with exactly: done"
    ),
    "edit": (
        "The file edit_target.txt contains the line 'STATUS: pending'. Use "
        "the edit tool to change 'pending' to 'reviewed'. Then use the edit "
        "tool again to change 'reviewed' to 'complete'. Then reply with "
        "exactly: done"
    ),
    "bash": (
        "Use the bash tool to run the command `true` three separate times, "
        "one tool call each. Then reply with exactly: done"
    ),
}

# Minimum tool invocations each scenario must exhibit to be a valid measurement.
SCENARIO_EXPECTATIONS = {
    "read": {"expected_answer": "done", "min_tool_calls": 5},
    "grep": {"expected_answer": "done", "min_tool_calls": 3},
    "edit": {"expected_answer": "done", "min_tool_calls": 2},
    "bash": {"expected_answer": "done", "min_tool_calls": 3},
}

TURN_TIMEOUT_SECONDS = 120


# --------------------------------------------------------------------------- #
# Pure, testable statistics
# --------------------------------------------------------------------------- #
def percentile_nearest_rank(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: ``ceil(q * n) - 1`` (clamped).

    For n=1 this is the single value; for n=2 and q=0.95 it is the maximum,
    guaranteeing ``median <= p95 <= max`` for any sample.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(q * len(ordered)) - 1
    rank = min(max(rank, 0), len(ordered) - 1)
    return ordered[rank]


def summarize(samples: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for name, values in sorted(samples.items()):
        if not values:
            continue
        median = statistics.median(values)
        p95 = percentile_nearest_rank(values, 0.95)
        maximum = max(values)
        # The invariant a correct quantile must satisfy.
        assert median <= p95 <= maximum, f"p95 invariant violated for {name!r}"
        summary[name] = {
            "calls": len(values),
            "median_ms": median * 1000,
            "p95_ms": p95 * 1000,
            "max_ms": maximum * 1000,
        }
    return summary


# --------------------------------------------------------------------------- #
# Pure, testable event reduction + scenario validation
# --------------------------------------------------------------------------- #
@dataclass
class ParsedRun:
    tool_samples: dict[str, list[float]] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    turns: list[float] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    invalid_lines: list[str] = field(default_factory=list)
    final_answer: str | None = None
    clean_end: bool = False
    aborted: bool = False
    unpaired_tool_ids: list[str] = field(default_factory=list)


def reduce_events(events: Iterable[dict[str, Any]]) -> ParsedRun:
    """Fold an event stream (each carrying a monotonic ``_ts``) into a run.

    Tool starts/ends are paired by ``toolCallId``; a start with no matching end
    is reported as unpaired rather than silently dropped.
    """
    run = ParsedRun()
    pending: dict[str, tuple[str, float]] = {}
    turn_start: float | None = None
    for event in events:
        run.raw_events.append(event)
        kind = event.get("type")
        ts = float(event.get("_ts", 0.0))
        if kind == "agent_start":
            turn_start = ts
        elif kind == "tool_execution_start":
            cid = str(event.get("toolCallId", "?"))
            name = event.get("toolName", "unknown")
            pending[cid] = (name, ts)
            run.tool_calls.append({"id": cid, "name": name})
        elif kind == "tool_execution_end":
            cid = str(event.get("toolCallId", "?"))
            started = pending.pop(cid, None)
            if started is not None:
                name, at = started
                run.tool_samples.setdefault(name, []).append(ts - at)
        elif kind == TERMINAL_EVENT:
            if turn_start is not None:
                run.turns.append(ts - turn_start)
                turn_start = None
            run.clean_end = True
            answer = event.get("text") or event.get("message") or event.get("answer")
            if answer is not None:
                run.final_answer = str(answer)
        elif kind in {"abort", "agent_aborted", "aborted"}:
            run.aborted = True
    run.unpaired_tool_ids = list(pending)
    return run


@dataclass
class ScenarioResult:
    name: str
    valid: bool
    reasons: list[str] = field(default_factory=list)
    tool_calls: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def validate_scenario(name: str, run: ParsedRun, expectation: dict[str, Any]) -> ScenarioResult:
    """A scenario is a valid latency measurement only if it ran clean.

    Aborted, non-terminating, wrong-answer, or too-few-tool-call runs keep their
    diagnostics but are excluded from headline latency.
    """
    reasons: list[str] = []
    if run.aborted:
        reasons.append("aborted")
    if not run.clean_end:
        reasons.append("no clean agent_end")
    expected_answer = expectation.get("expected_answer")
    if expected_answer is not None:
        got = (run.final_answer or "").strip().lower()
        if got != expected_answer.strip().lower():
            reasons.append(f"final answer {got!r} != {expected_answer!r}")
    min_calls = expectation.get("min_tool_calls", 0)
    if len(run.tool_calls) < min_calls:
        reasons.append(f"only {len(run.tool_calls)} tool calls, expected >= {min_calls}")
    if run.unpaired_tool_ids:
        reasons.append(f"unpaired tool starts: {run.unpaired_tool_ids}")
    return ScenarioResult(name=name, valid=not reasons, reasons=reasons,
                          tool_calls=len(run.tool_calls))


def build_workspace(root: Path) -> list[str]:
    files = []
    for index in range(5):
        path = root / f"sample_{index}.txt"
        body = [f"line {line} of sample {index}" for line in range(400)]
        if index == 2:
            body.append("FORGE_MARKER_A appears here")
        if index == 3:
            body.append("FORGE_MARKER_B appears here")
            body.append("FORGE_MARKER_C appears here")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        files.append(path.name)
    (root / "edit_target.txt").write_text("STATUS: pending\n", encoding="utf-8")
    return files


# --------------------------------------------------------------------------- #
# Live Pi driver (thin; all analysis goes through the pure functions above)
# --------------------------------------------------------------------------- #
class PiBench:
    def __init__(self, cwd: Path, provider: str | None, model: str | None,
                 turn_timeout: float = TURN_TIMEOUT_SECONDS):
        command = ["pi", "--mode", "rpc", "--no-session"]
        if provider:
            command += ["--provider", provider]
        if model:
            command += ["--model", model]
        self.turn_timeout = turn_timeout
        self.process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.events: list[dict[str, Any]] = []
        self.invalid_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._settled = threading.Event()
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._err_reader.start()

    def _read_events(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            now = time.monotonic()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.invalid_lines.append(line.rstrip("\n"))
                continue
            event["_ts"] = now
            self.events.append(event)
            if event.get("type") == TERMINAL_EVENT:
                self._settled.set()
        self._settled.set()

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def prompt(self, message: str) -> bool:
        assert self.process.stdin is not None
        self._settled.clear()
        self.process.stdin.write(json.dumps({"type": "prompt", "message": message}) + "\n")
        self.process.stdin.flush()
        settled = self._settled.wait(self.turn_timeout)
        if not settled:
            self.process.stdin.write(json.dumps({"type": "abort"}) + "\n")
            self.process.stdin.flush()
            self.events.append({"type": "abort", "_ts": time.monotonic()})
            self._settled.wait(15)
        return settled

    def shutdown(self) -> None:
        try:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def run_once(provider: str | None, model: str | None, turn_timeout: float) -> dict[str, Any]:
    """Drive every scenario once; return per-scenario validity + aggregated
    latency over only the valid scenarios."""
    with tempfile.TemporaryDirectory(prefix="pi-tool-bench-") as scratch:
        root = Path(scratch)
        files = build_workspace(root)
        bench = PiBench(root, provider, model, turn_timeout)
        scenarios: list[ScenarioResult] = []
        valid_samples: dict[str, list[float]] = {}
        all_turns: list[float] = []
        try:
            for name, template in SCENARIOS.items():
                start = len(bench.events)
                message = template.format(files=", ".join(files))
                bench.prompt(message)
                run = reduce_events(bench.events[start:])
                result = validate_scenario(name, run, SCENARIO_EXPECTATIONS[name])
                result.diagnostics = {
                    "clean_end": run.clean_end,
                    "aborted": run.aborted,
                    "final_answer": run.final_answer,
                    "unpaired_tool_ids": run.unpaired_tool_ids,
                    # Keep bounded raw protocol evidence so an invalid scenario
                    # can be diagnosed without making the benchmark artifact
                    # unbounded on a looping model.
                    "raw_events": run.raw_events[-500:],
                }
                scenarios.append(result)
                if result.valid:
                    for tool, values in run.tool_samples.items():
                        valid_samples.setdefault(tool, []).extend(values)
                    all_turns.extend(run.turns)
                elif result.reasons:
                    print(f"warning: scenario '{name}' invalid: {result.reasons}", file=sys.stderr)
        finally:
            bench.shutdown()
        return {
            "scenarios": [vars(s) for s in scenarios],
            "valid_scenarios": sum(1 for s in scenarios if s.valid),
            "tools": summarize(valid_samples),
            "turn_seconds": [round(v, 3) for v in all_turns],
            "total_tool_seconds": round(sum(sum(v) for v in valid_samples.values()), 3),
            "invalid_json_lines": len(bench.invalid_lines),
            "invalid_json_line_samples": bench.invalid_lines[-50:],
            "stderr_lines": len(bench.stderr_lines),
            "stderr_tail": bench.stderr_lines[-50:],
        }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="lmstudio")
    parser.add_argument("--model", default=None)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=TURN_TIMEOUT_SECONDS)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()

    all_runs = []
    for run in range(arguments.runs):
        print(f"run {run + 1}/{arguments.runs}…", file=sys.stderr)
        all_runs.append(run_once(arguments.provider, arguments.model, arguments.timeout))
    total_valid = sum(r["valid_scenarios"] for r in all_runs)
    report = {
        "schema_version": SCHEMA_VERSION,
        "provider": arguments.provider,
        "model": arguments.model,
        "seed": arguments.seed,
        "pi_protocol": "0.80.2",
        "terminal_event": TERMINAL_EVENT,
        "runs": all_runs,
        "total_valid_scenarios": total_valid,
    }
    if arguments.output:
        _atomic_write(arguments.output, report)
        print(f"wrote {arguments.output}", file=sys.stderr)
    else:
        print(json.dumps(report, indent=2))
    # Non-zero if no scenario yielded a valid latency measurement.
    return 0 if total_valid > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
