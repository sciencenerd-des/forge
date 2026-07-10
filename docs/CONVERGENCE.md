# Convergence — a dense, verifiable progress signal

Implements `specs/convergent-autonomous-harness.html`. This document is the
operator-facing reference for the six layers added on top of the PGE loop.

## 1. Progress ledger & controller (`forge_runtime/convergence.py`)

Every evaluator cycle now yields a `TestOutcome` per contract test rather
than one aggregate verdict. `distance_to_done()` reduces the outcome history
to `D(t) = 1 - (weighted passing / weighted total)`, where a test that has
flipped pass/fail at least twice is a *flake* and counts at half weight so
it can't single-handedly stall convergence.

`ConvergenceController` is an epsilon-greedy bandit over five strategies —
`retry`, `re-decompose`, `escalate-context`, `switch-provider-profile`,
`block` — rewarded by the negative subsequent `ΔD` (a bigger drop in
distance is a better reward). It never explores into `block`; that arm is
only chosen deterministically once the attempt budget is exhausted. Arm
statistics are serializable via `state()` / `from_state()` so a resumed run
keeps its learned preferences.

**Invariant (mirrors Lesson 4):** the controller's `decide()` return value is
always a member of `STRATEGIES`, i.e. a real, pre-mapped graph edge.

## 2. Contract immune system (`forge_runtime/contract_immune.py`)

Hardens against Lesson 1 (a silently weakened contract that becomes
trivially satisfiable):

- `score_contract()` runs every test against an *empty* scratch workspace.
  A test that passes on nothing is `vacuous` — it proves nothing about the
  actual deliverable.
- `contract_hash()` + `detect_drift()` content-hash the ordered test list at
  derivation; any later drop or edit is `drifted` and must be an explicit,
  logged amendment — never a silent gate action.
- `mutate_and_check()` / `is_non_discriminating()` apply a caller-supplied
  mutation (e.g. delete the built artifact) to a workspace copy and confirm
  at least one test now fails — a contract that survives its own artifact's
  destruction can't tell "done" from "broken".

## 3. Verifier hierarchy (`forge_runtime/verifier_hierarchy.py`)

The verifier hierarchy is implemented as a tested module, but it is not yet a
live completion gate. Today, the graph's live completion claim remains T1.
When the integration described below lands, completion will become tiered:

- **T1** — the evaluator runs the contract in the live workspace (unchanged
  behavior; today's ground truth).
- **T2** — `replay()` snapshots the workspace and re-runs the contract from
  scratch inside a network-isolated (`--network=none`), pinned-image Docker
  container.

A future wired goal will reach `verified` only at T2. If Docker isn't
available, `replay()` returns `tier="skipped", reason="docker_unavailable"` —
the future caller must treat this as `complete_unverified`, not as a failure.

Every T2 run produces one `EvidenceRecord` per test (command, exit code,
output digest, duration) — an auditable trail, not a log line.

## 4. Compounding memory (`forge_runtime/lessons.py`)

Activates the `Lesson` dataclass already declared in `steering.py`.
`should_extract()` gates LLM cost: extraction fires only on the *second*
occurrence of a failure fingerprint (`failure_type:test_id:error_signature`).
`extract_lesson()` rejects vacuous output (too short, or a restatement of
the raw error). `LessonStore` dedupes on fingerprint — repeats bump
confidence rather than duplicating — and decays/prunes lessons that go
unmatched, so stale advice doesn't accumulate forever.

## 5. Action search (`forge_runtime/action_search.py`)

For stuck tasks (`attempts >= 2`, or the controller's `escalate-context`
strategy — wire a sixth controller arm `escalate-search` if adopting this
end-to-end), the executor may sample multiple candidate actions and score
them with a cheap cascade — syntax check → path-policy check → relevance —
before any workspace mutation. Scoring runs against a copy-on-write overlay;
the live workspace is untouched until a candidate is selected. All
candidates failing the gate falls back to single-shot execution.

## 6. Eval-gated self-improvement (`evals/`)

`evals/suite/manifest.json` defines small, deterministic benchmark goals
(including two adversarial ones: a vacuous-contract trap for Phase 2, a
flaky-test goal for Phase 1). `evals/runner.py` runs the suite and reports
five metrics: verified-completion rate, mean cycles-to-done, mean
distance-AUC, false-completion count, token cost. `evals/gate.py` compares a
candidate result to the recorded baseline and passes only on non-regression
— any false completion is a hard fail regardless of other metrics.

`CONFIG_WHITELIST` in `evals/gate.py` bounds what an automated proposer may
ever touch: controller epsilon, lesson pack budget, action-search N, prompt
template variant. **Engine code is never a valid proposal target** — this is
a deliberate safety inversion from self-modifying-code patterns; changes
still require a human to merge the PR the gate produces.

## Running it

```bash
uv run pytest tests/regression -v      # all pins, including the six new ones
make evals                             # benchmark suite (fails if any goal is skipped)
python evals/gate.py --candidate evals/results/<sha>.json --baseline evals/results/baseline.json
```

## What is and isn't wired into the live PGE graph

This pass ships all six capabilities as **tested, importable modules** with
full unit coverage. Wiring `ConvergenceController` into `run_pge.py`'s batch
loop, emitting per-test outcomes from `evaluator_node.py`, and adding the
`escalate-search` graph edge require a running Postgres + LLM backend to
validate safely (per Lesson 3, a live-wired but unverified change to the
graph risks exactly the kind of regression this plan exists to prevent).
Those integration points are called out explicitly in each module's
docstring; wiring them is the natural next increment once a backend is
available to test against.
