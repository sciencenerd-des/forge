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

## 8. A binary verdict can't tell thrashing from converging
Contract pass/fail is necessary but not sufficient signal — a run stuck oscillating
between two failing states looks identical to one making steady progress until
either finishes or the run ends. **Invariant:** track a dense distance-to-done
metric per contract test, weight flaky tests down instead of letting them stall
convergence, and steer recovery off the metric's derivative, not off despair
counters. See `specs/convergent-autonomous-harness.html`.
→ `test_convergence_ledger.py`

## 9. Contract strength must be measured, not assumed
Lesson 1 fixed one specific way a contract test got silently dropped. The general
failure mode — a contract that is gameable or was already trivially satisfiable —
needs a standing defense: probe every test against an empty workspace (a test that
passes on nothing is vacuous), content-hash the test list so any later drop or edit
is loud, and mutate a real artifact to confirm the contract actually discriminates
done from broken.
→ `test_contract_immune.py`

## 10. "Done" claimed in the same workspace that built it isn't independently verified
The evaluator's in-workspace pass is real signal, but it can't rule out
workspace-specific state (uncommitted files, cached build output, ambient env vars)
propping up a result that wouldn't reproduce elsewhere. A goal reaches `verified`
only after a hermetic, network-isolated replay from a snapshot — and evidence
(command, exit code, output digest) is retained per test, not just a pass/fail line.
Missing Docker degrades to `complete_unverified`; it must never block the run.
→ `test_hermetic_replay.py`

## 11. Self-modification is only safe under measurement, and never to engine code
Any mechanism that lets the harness change its own configuration needs a benchmark
suite it cannot fabricate results for, and a hard whitelist of what it may propose.
False completions in that benchmark are a harder failure than any performance
regression — a single one means the reward channel itself lied. Engine code is
never a valid proposal target; a human merges every change the gate approves.
→ `test_self_improvement_gate.py`

## 12. A safety net can undo a fix; a fail-open gate can hide that it did
Three interactions from live reruns (2026-07-05), each individually reasonable,
composed into false completions: (a) the vanishing-test guard's auto-revert
restored workspace content a semantic review had just rejected — nine
consecutive times, in one case — and completion then proceeded against it;
(b) the LLM review gates swallowed cloud-call failures as `{}`, byte-identical
to "reviewed and passed", so nobody could tell an outage from a verdict; (c)
whole-file source-vs-test classification and unfiltered build directories fed
the reviewers the wrong code entirely (an implementation hidden inside
`test.js`; CMake's compiler probe instead of the real evaluator). The
defenses: review-payload content hashes with durable flag memory in `.git/`
(rejected content cannot complete unchanged, regardless of what the reviewer
says next time), an explicit `review_error` sentinel so fail-open is loud, a
co-located-file fallback so the reviews always see the code that exists, and
build-artifact dirs in the skip list. The meta-lesson: every gate that fails
open must be *visibly* open, and every guard that restores old state must
re-face the judgments that state already lost.
→ `test_contract_immune.py`

## 13. A tool-availability fallback (`||`) must never share an exit path with the tool's own failure
Building the stack-aware run_tests/lint tools (forge_runtime/sandbox.py,
engine/src/nodes/executor_node.py), the first draft used `real_tool ... ||
echo 'not available'` to handle a linter/test-runner that might be missing.
Smoke-testing against a real sandboxed container before wiring it into the
goal loop caught the bug: `||` fires on ANY nonzero exit, including the tool
running successfully and finding a genuine failure — `pytest` with a real
failing test, or `clang-format --Werror` with a real formatting violation.
The fallback branch (`echo ...`) always exits 0, so that became the whole
command's reported exit code, silently converting a real test/lint failure
into an apparent pass. The fix is a structural one: an explicit
`if command -v tool; then tool ...; else echo 'unavailable'; false; fi` so
"tool missing" and "tool ran and failed" are on non-overlapping exit paths,
never both able to fire off the same invocation. This is the same failure
shape as the pipefail exit-code masking fixed earlier in auditor.py's
contract templates (`cmd1 | cmd2` reporting cmd2's exit code, not cmd1's real
one), one level up the stack: any `A || B` where B is a graceful fallback
must be proven to trigger ONLY on A's absence, never on A's legitimate
failure — checked here by actually running the failing case through Docker,
not by reading the shell logic and assuming.
→ `test_new_tools.py`

**Addendum (2026-07-06):** the identical bug was independently found in
`auditor.py`'s own deterministic python T3 template — `pytest ... | tail -3
|| unittest discover ... | tail -3` — while restarting the 8 goals inside
the new sandbox container. The LRU-cache goal's real implementation had a
genuine eviction bug (2 of 3 tests failing), but T3 reported PASS: pipefail
correctly made the pytest pipeline itself exit nonzero, but the outer `||`
still fell through to `unittest discover` finding zero pytest-style tests
("Ran 0 tests ... OK"), which became the reported result — and the goal was
marked verified complete over broken code. This had been silently possible
on every Python goal run all session, sandbox or not; it was surfaced now
only because re-verifying the goal's own test file independently is part of
this session's standing practice. Fixed with the same if/then/else
structure. This is why the lesson is "prove `A \|\| B` triggers only on A's
absence" and not "fix the one place it happened" — the shape recurs
wherever a fallback is bolted onto a command whose real exit code matters.
→ `test_pipefail_hardening.py`

## 14. A file-classification fallback needs BOTH directions covered, not just the one that broke first
Lesson 12's co-located-file fix handled "no file classified as SOURCE" (an
implementation hidden inside a bare `test.js`). Restarting the 8 goals in
the new sandbox container found the mirror case: "no file classified as
TEST at all" — legitimate for the CMake+CTest convention where a test is
just running a program and checking its exit code/stdout, not a separate
file. A `main.cpp` printing `"Test Passed"` and exiting 0, with an unused
stub `evaluator.cpp` never linked into the build and zero postfix-evaluation
logic anywhere, passed every mechanical gate AND both semantic reviews —
because `collect_review_content` found empty `test_text` and both reviews
silently returned `{}` before ever calling the LLM, exactly like the
original bug, just with source/test swapped. Fixed by adding the symmetric
fallback: when source exists but nothing is classified as test, review the
source AS the test too. The meta-lesson: a two-sided classification (source
vs. test) that only got a one-sided regression test will resurface on the
side nobody checked — the fix for one direction is a strong hint to
immediately verify the other, not a reason to consider the class of bug closed.
→ `test_contract_immune.py`

## 15. A contract that discards diagnostics blinds the whole loop by design
Analysis of the goal-4 stall (2026-07-06/07, C++ postfix, 27 tasks blocked,
T4 failed 32 straight attempts) found the failure was not model capability:
the audit command itself was `cmake --build build >/dev/null 2>&1 && echo
BUILD_OK` — on failure `echo` never runs and ALL compiler output is
discarded, so the evaluator captured `''`, every repair task read
`current output: ''`, and the executor was told "T4 fails" with zero
diagnostic for a one-line missing `#include <string>` whose fix GCC named
verbatim in the output the contract threw away. Worse, the executor copies
the audit command when self-verifying, so it reproduced its own blindness.
The fix: success prints only the marker (clean substring check), failure
prints the captured output with `error:` lines LAST — because every
downstream consumer (evaluator output[-700:], repair descriptions) truncates
keeping the end. Plus `_failure_evidence()`: repair descriptions now
prioritize error-bearing lines over tail noise and label empty output
explicitly instead of quoting `''` as if it were information.
→ `test_pipefail_hardening.py`, `test_evaluator_feedback.py`

## 16. Never revert to a state a judge has already rejected
The goal-6 churn (2026-07-07, JSON parser, 29+ batches) was a perfect
three-state cycle between two harness protections, each correct alone: the
semantic gate rejects the primitives-only parser (all tests green, so it
was also the ratchet's checkpoint) → the executor writes the fuller parser
→ the fuller parser regresses T3 → the monotonic ratchet auto-reverts to
the checkpoint — which IS the rejected parser — → the gate rejects it
again. The model never got more than one evaluation cycle of forward
progress on the semantic gap. Since the flag-memory gate (Lesson 12)
guarantees semantically-rejected content can never complete, reverting to
it is provably futile. The ratchet now grants ONE forward-fix grace cycle
when the revert target was just semantically blocked: the regressing (more
complete) attempt is kept, the baseline resets to what currently passes,
and the executor is told to fix forward. Self-limiting by construction — a
semantic block only happens on an all-green state, so consecutive grace
cycles are impossible. General form: when two guards individually enforce
"don't go backward" and "don't stay here," verify their composition has an
exit; ours didn't.
→ `test_evaluator_feedback.py`

## 17. An agent won't pull external knowledge at an impasse — push it
Forensics across 26 logged runs (~1300 tool calls): the executor NEVER
invoked its research tools — zero fetch_doc/browser_fetch calls ever,
including 32 consecutive attempts on a single compile error whose fix was
the top web-search hit. This matches the literature: models don't reliably
self-correct without external information (Huang et al., ICLR 2024; Gou et
al., CRITIC), and whether an agent uses a tool tracks how it's surfaced at
the decision point, not whether it exists in a menu (Yang et al.,
SWE-agent) — our own evidence agrees: run_tests/build, surfaced in the CORE
list with explicit "prefer this" language, were adopted immediately, while
fetch_doc, buried in an ON-DEMAND list and requiring the model to already
know a URL, was never touched. Worse, the harness's one research mechanism
(the auditor's research_and_cache) had never worked: `ddgs` was missing
from requirements, every search failed, the failure was LOGGED AS SUCCESS
("📚 Auditor researched & cached docs"), and the error string itself was
cached under the research key — permanently poisoning each term (17 such
entries found live). The fixes, in order of importance: (1) the harness
now pushes research to the impasse — after a test fails twice, the
evaluator searches the error signature itself and embeds the findings in
the repair task (model needs no initiative at all); (2) a web_search tool
in the CORE list with a mandatory-reflex trigger rule ("same error twice →
search the exact error text"); (3) failures are never cached and never
logged as success. Meta-lesson: for small local models especially,
"available" is not an affordance — either put the knowledge in the prompt
at the moment it's needed, or accept it will never be used.
→ `test_failure_research.py`

## 18. A fail-open gate plus novel content equals a bypass — fall back, then defer
Lesson 12 made review outages LOUD; Lesson 17's flag memory made re-judging
REJECTED content unnecessary. The remaining hole, found by an actual
ollama.com DNS outage landing at exactly the wrong cycle (2026-07-08, ML
experiment-loop goal): never-reviewed NOVEL content sails through when the
reviewer is down — flag memory has no opinion on hashes it has never seen,
the mechanical template tests are goal-agnostic by design, and a goal with
none of its deliverables (no results.csv, no summary.md, no training loop)
completed as "verified". Two-layer fix: (1) reviews fall back from the
cloud model to the LOCAL model — degraded judgment beats no judgment, and
the local model is definitionally reachable while the loop runs at all;
(2) if BOTH fail at a would-complete moment, completion is DEFERRED to the
next cycle rather than granted. Withholding the completion CLAIM is not
blocking the loop (Lesson 10's verified/complete_unverified distinction):
work continues, verification retries, and a truly persistent outage ends
the run at the turn ceiling as honestly-incomplete instead of falsely
complete. General form: fail-open is only safe when some OTHER layer still
covers the opened gap; enumerate what each outage exposes, and make the
last layer fail closed on the claim, not on the work.
→ `test_evaluator_feedback.py`

## 19. Fix the class, not the instance — a whitelist is a promise to have the same bug again
Reviewing Lessons 13-18: five of them were single instances of just three
underlying classes, each class re-biting in a new costume after every
per-site patch. (A) Contract commands that destroy their own failure
evidence — pipefail masking, `||` fallbacks, `>/dev/null` discards: now
fixed at the CONSUMPTION point, generically — any failing audit test with
empty output is re-run once by the evaluator with output-discarding
redirects stripped (strip_output_discards), covering every stack and every
author including LLM-written contracts that will keep reinventing the
pattern. (B) Reviewers judging an extension-whitelisted keyhole view —
hidden implementations, invisible manifests, invisible deliverables: the
whitelist itself was the bug; reviews now see ALL text files (binary-
sniffed, size-capped, generated dirs excluded), so a stack or file kind
nobody predicted cannot be invisible. (C) Goal-named deliverables invisible
to judgment: filenames are extracted from the goal text itself and each is
reported EXISTS/MISSING with size into every review — goal-driven, so no
per-goal or per-language configuration exists to be missing. The test of a
real class fix: all the instance-level regression tests pass UNCHANGED
under the general mechanism, plus new tests exercising members of the class
that never occurred live (a Go file, a yaml config, a goal-named CSV).
→ `test_root_cause_generalizations.py`

**Addendum to 19 (same day):** the ML-loop rerun under all three class
fixes STILL falsely completed — revealing the class had a deterministic
half nobody had separated out. Artifact evidence made `results.csv:
MISSING` visible to the reviewer, but review philosophy judges whether a
capability was ATTEMPTED (code that would produce the file counts), while
the goal demanded the file EXIST. The executor even wrote a
fabricate-assert-delete test (`df.to_csv(...); assert exists; os.remove()`)
— self-satisfying evidence no semantic judgment reliably catches. The
split that closes it: existence is checked DETERMINISTICALLY as a
completion gate (missing_goal_artifacts — goal-named files must exist in
the workspace at completion, full stop); the LLM reviews only ever judge
qualities that genuinely require judgment. Never delegate to a judge what
a filesystem call can answer.
→ `test_root_cause_generalizations.py`

## 20. A sandbox that can starve its judge isn't contained
The container sandbox dropped every capability, ran non-root, and had no
host filesystem path — and still took down the harness (2026-07-09, ML
experiment-loop goal): an unbounded sklearn training loop pinned the Docker
VM at 300%+ CPU and exhausted host swap on a machine also carrying a ~17GB
GPU-resident LLM. `docker exec` — and `docker ps` itself — hung, so every
audit test reported runner errors: the verification infrastructure was
wedged by the very workload it existed to judge. Privilege isolation and
resource isolation are different axes; the threat model must include the
merely-expensive, not just the malicious. Sandboxes now carry --memory /
--memory-swap (equal, so no swap thrash) / --cpus / --pids-limit,
env-tunable. Corollary for any agent infrastructure: compute-heavy goals
(ML training, rendering, fuzzing) are the NORMAL case that finds this,
no adversary required.
→ `test_sandbox.py`

## 21. Context that only grows is a memory leak with two addresses
Two compounding forms, both found live (2026-07-09). (1) Prompt bloat: the
executor's transcript grew monotonically within a batch while its
compression layer silently no-op'd — every turn logged "Headroom fallback:
ModuleNotFoundError" and shipped the full pack anyway. A compression layer
whose failure mode is "uncompressed, but keep going" will fail invisibly
forever; the fallback itself must compress (deterministic, dependency-free
`_local_compact`), and the executor now folds older tool chatter past a
token threshold, salvaging only errors/commands/paths
(PGE_CONTEXT_COMPACT_THRESHOLD). (2) Server-side KV growth: at num_ctx=200k
Ollama's KV cache grows with the longest prompt seen and never shrinks
(7.7GB -> 17GB across a session — the other half of the swap-exhaustion
incident in Lesson 20). The loop now unloads the model (keep_alive=0) at
regular batch boundaries (PGE_KV_PRUNE_INTERVAL), releasing the cache while
leaving num_ctx untouched. Both compactions are deterministic and
non-fatal by construction: an optimization that can crash the loop is a
net negative.
→ `test_context_compaction.py`

## 22. Install and execute must share one interpreter

install_deps deterministically installs into /workspace/.venv — but the
container bash tool ran commands with env=None, so a bare `python main.py`
resolved the SYSTEM interpreter and got ModuleNotFoundError for packages
the harness had just "successfully" installed. The model looped on
install → run → import error for four batches; each half worked, the seam
between them was the bug. Host mode already prepended .venv/bin via
_venv_env; ContainerSandbox.run now prepends /workspace/.venv/bin (and
node_modules/.bin) to PATH for every string command, so install and
execution always agree, in every language, without the model naming paths.
Second half of the same seam (REST API run, same day): argv-form commands
(`["python3", "-c", ...]`) went straight to `docker exec` with no shell,
silently keeping the default PATH — same ModuleNotFoundError, different
entry point. Both forms are now wrapped through bash with the prepend.
→ `test_sandbox.py::test_container_run_prepends_workspace_venv_to_path`

## 23. Byte-fingerprint stagnation cannot see semantic thrash

REST API goal, 2026-07-10: the goal required empty-title POSTs to return
422. The executor kept WEAKENING its own test (assert 201) instead of
adding validation. Defense-in-depth held — the scope gate blocked
completion 11 times with the same verdict, so no fake completion escaped —
but nothing terminated the run: every weaken/revert flip changed workspace
bytes, so `stagnant_batches` never advanced, and the loop thrashed for an
hour. Two class-level fixes: (1) the scope-gate feedback now explicitly
forbids weakening test expectations (the review judges tests against the
GOAL TEXT, so a weakened test is itself a scope gap); (2) run_pge counts
consecutive semantic completion blocks and stops honestly incomplete after
PGE_MAX_SEMANTIC_BLOCKS (default 4). Corollary from the validation rerun:
never key thrash detection on LLM-verdict TEXT — the reviewer rewords the
same verdict every cycle ("ensuring that…422" vs "validating that…422"),
so an exact-match signature never fires; being semantically blocked at the
finish line N batches in a row is non-convergence regardless of wording.
An honest "blocked" beats an infinite green-red oscillation.
→ `test_semantic_block_thrash.py`
