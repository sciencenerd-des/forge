# Lessons — why Forge is built the way it is

Every invariant below is scar tissue from a real failure that stalled or silently
broke autonomous runs. Each is now pinned by a test in `tests/regression/`. If you
change the engine, keep these true.

## 1. The contract is the ground truth — and it must never be silently weakened
A goal is "done" only when the **evaluator independently runs the test list** and
every test passes. The single worst failure mode was a **false completion**: the
audit-test quality gate (`validate_tests`) silently *dropped* the render-variety
check, the evaluator ran the surviving trivial tests, declared the goal complete,
and the loop exited after one batch — for two weeks, presenting as "the loop won't
persist."

Two ways the check got dropped: it matched the "python `import` for a C++ goal"
rule (a legitimate `python3 -c "import re; ...read render..."`), and a compound
`if ...; then ...; fi` test's first token `if` looked like a missing binary to a
`shutil.which()` check. **Invariant:** a gate may reject a *provably broken* test,
but it must not quietly turn a real contract into a trivially-satisfiable one.
Prefer pure-shell acceptance tests; keep `validate_tests` narrow.
→ `test_validate_tests_gate.py`

## 2. Acceptance tests must be satisfiable for the artifact actually produced
The render-variety test originally counted distinct *raw bytes* (`od -tu1`) — which
is unsatisfiable for a P3 (ASCII) PPM (only ~13 byte values exist however varied
the image), while the format check permitted P3. The loop chased an impossible
target forever. **Invariant:** a test must measure the thing it claims to, for
every output format the contract allows.
→ `test_render_contract_t7.py`

## 3. One bad batch must not kill the run
Durable state lives in Postgres; the next batch resumes cleanly. So the batch loop
**catches every exception** — transient model timeouts *and* node-level bugs —
logs it, preserves state, backs off, and retries under a consecutive-failure
budget. Only a sustained streak ends the run, gracefully as `blocked`. Re-raising
on the first node exception is what made "fix one bug, the next one kills the loop"
a recurring experience.
→ `test_loop_resilience.py`

## 4. Every router return value must be a real graph edge
A stagnation path once returned an edge name (`"planner"`) that wasn't in the
node's edge map → `KeyError` → dead run. **Invariant:** router functions return
only mapped edges; stagnation is handled by re-decomposing, not by inventing edges.
→ `test_planner_router.py`

## 5. Meet the model where it is (tooling ergonomics)
Small models emit shell lines into an argv-only `run_command`, and only ever
`write_file` (never `edit_file`). Rather than fight them: detect shell-shaped input
and route it through `bash`; allow a `write_file` overwrite of a small file the
model clearly intends to replace. Hard guards that a model can't satisfy become
infinite loops.
→ `test_run_command_shell.py`

## 6. Detach properly; never trust the parent's lifetime
A detached run is spawned with `start_new_session=True` so it outlives the gateway
that launched it. Orphaning was never the problem — uncaught exceptions were (see
#3).
→ `test_launcher_lifecycle.py`

## 7. Nothing machine-specific in the engine
Paths, the database, the default project, and the model backend all resolve through
`forge_config.py` from the environment. A hardcoded home path or project UUID is
how a "works on my machine" harness fails to be plug-and-play.
