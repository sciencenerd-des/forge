#!/usr/bin/env python3
"""Benchmark Pi's built-in tool execution latency over RPC.

Measures wall time between `tool_execution_start` and `tool_execution_end`
events (model latency excluded) plus each turn's total duration, so the
tool share of turn time is explicit. This is the evidence input for the
forge-tools go/no-go decision recorded in docs/PI_TOOL_BENCHMARK.md.

Usage:
    python3 scripts/pi_tool_bench.py [--provider lmstudio] [--model google/gemma-4-12b]

Requires the pinned Pi executable on PATH and a reachable model backend.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


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

# Small local models often keep generating and never settle; the metric that
# matters is tool-execution latency, so cap each scenario and abort the turn,
# keeping whatever tool samples arrived.
TURN_TIMEOUT_SECONDS = 120


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


class PiBench:
    def __init__(self, cwd: Path, provider: str | None, model: str | None):
        command = ["pi", "--mode", "rpc", "--no-session"]
        if provider:
            command += ["--provider", provider]
        if model:
            command += ["--model", model]
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.tool_samples: dict[str, list[float]] = {}
        self.turn_seconds: list[float] = []
        self._settled = threading.Event()
        self._pending_tools: dict[str, tuple[str, float]] = {}
        self._turn_started: float | None = None
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    def _read_events(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            now = time.monotonic()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "agent_start":
                self._turn_started = now
            elif kind == "tool_execution_start":
                self._pending_tools[event.get("toolCallId", "?")] = (
                    event.get("toolName", "unknown"),
                    now,
                )
            elif kind == "tool_execution_end":
                started = self._pending_tools.pop(event.get("toolCallId", "?"), None)
                if started is not None:
                    name, at = started
                    self.tool_samples.setdefault(name, []).append(now - at)
            elif kind == "agent_settled":
                if self._turn_started is not None:
                    self.turn_seconds.append(now - self._turn_started)
                    self._turn_started = None
                self._settled.set()
        self._settled.set()

    def prompt(self, message: str) -> bool:
        assert self.process.stdin is not None
        self._settled.clear()
        self.process.stdin.write(
            json.dumps({"type": "prompt", "message": message}) + "\n"
        )
        self.process.stdin.flush()
        settled = self._settled.wait(TURN_TIMEOUT_SECONDS)
        if not settled:
            # Abort the runaway turn but keep the tool samples it produced.
            self.process.stdin.write(json.dumps({"type": "abort"}) + "\n")
            self.process.stdin.flush()
            self._settled.wait(15)
        return settled

    def shutdown(self) -> None:
        try:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


def run_once(provider: str | None, model: str | None) -> tuple[dict, list[float]]:
    with tempfile.TemporaryDirectory(prefix="pi-tool-bench-") as scratch:
        root = Path(scratch)
        files = build_workspace(root)
        bench = PiBench(root, provider, model)
        try:
            for name, template in SCENARIOS.items():
                message = template.format(files=", ".join(files))
                if not bench.prompt(message):
                    print(f"warning: scenario '{name}' timed out", file=sys.stderr)
        finally:
            bench.shutdown()
        return bench.tool_samples, bench.turn_seconds


def summarize(samples: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    summary = {}
    for name, values in sorted(samples.items()):
        if not values:
            continue
        summary[name] = {
            "calls": len(values),
            "median_ms": statistics.median(values) * 1000,
            "p95_ms": sorted(values)[max(0, int(len(values) * 0.95) - 1)] * 1000,
            "max_ms": max(values) * 1000,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="lmstudio")
    parser.add_argument("--model", default=None)
    parser.add_argument("--runs", type=int, default=2)
    arguments = parser.parse_args()

    all_runs = []
    for run in range(arguments.runs):
        print(f"run {run + 1}/{arguments.runs}…", file=sys.stderr)
        tools, turns = run_once(arguments.provider, arguments.model)
        all_runs.append({
            "tools": summarize(tools),
            "turn_seconds": [round(value, 2) for value in turns],
            "total_tool_seconds": round(
                sum(sum(values) for values in tools.values()), 3
            ),
            "total_turn_seconds": round(sum(turns), 2),
        })
    print(json.dumps({"provider": arguments.provider, "model": arguments.model,
                      "runs": all_runs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
