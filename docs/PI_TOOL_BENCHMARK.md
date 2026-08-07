# Pi built-in tool latency benchmark

Evidence input for the `forge-tools` go/no-go decision deferred by
[specs/forge-rust-tui-pi-harness.html](../specs/forge-rust-tui-pi-harness.html)
and scheduled by [specs/forge-rust-tui-cycle2.html](../specs/forge-rust-tui-cycle2.html)
(Phase 5). Run with [scripts/pi_tool_bench.py](../scripts/pi_tool_bench.py).

## Method

- Pi `@earendil-works/pi-coding-agent@0.80.2` in `--mode rpc --no-session`,
  provider `lmstudio`, model `google/gemma-4-12b-qat` (local LM Studio).
- Four scripted scenarios (read ×5 files, grep ×3 patterns, edit ×2, bash ×3)
  in a throwaway workspace of five 400-line files.
- Tool latency = wall time between Pi's `tool_execution_start` and
  `tool_execution_end` events, so model generation time is excluded.
- Two full runs on 2026-07-12 (Apple Silicon dev machine).

## Results

| Tool | Run 1 median | Run 1 p95 | Run 2 median | Run 2 p95 |
| --- | --- | --- | --- | --- |
| `read` (6 calls/run) | 1.9 ms | 2.8 ms | 3.3 ms | 3.8 ms |
| `edit` (2 calls/run) | 6.1 ms | 8.9 ms (max) | 5.1 ms | 6.7 ms (max) |
| `bash` (6–7 calls/run) | 10.1 ms | 11.3 ms | 15.3 ms | 19.9 ms |

- Total tool-execution time per full run: **0.09 s** (run 1) and **0.12 s** (run 2).
- **Turn-duration result withdrawn:** this historical run used the obsolete
  `agent_settled` event rather than Pi 0.80.2's `agent_end`. The script now
  waits for `agent_end`; rerun it before making an end-to-end share claim.
- The model invoked `bash` for the search scenario instead of the dedicated
  `grep` tool, so grep latency is folded into the bash rows.
- Run-to-run medians agree within ~1.5× at single-digit-to-low-double-digit
  millisecond absolute values — stable for decision purposes.

## Verdict: NO-GO for forge-tools as a performance project

The recorded tool-only data supports a **provisional NO-GO**: Pi's built-in
tools execute in 2–20 ms, so they are unlikely to be the meaningful latency
bottleneck. The stronger end-to-end comparison is intentionally deferred until
the corrected script is rerun and records real `agent_end` turn durations.

Closed accordingly:

- The `forge-tools` backlog entry is closed as a **latency** project. If it
  ever returns, the justification must be something else (sandboxing policy, a
  Rust tool backend for the PGE executor replacing `forge_runtime/tools.py`,
  determinism), not speed.
- The actionable local-model finding is different: small models burn the turn
  budget by never settling. That is a harness-policy problem (stop conditions,
  steering) — exactly the territory the PGE evaluator and Pi's abort/steer
  controls already own.
