import os
import json
from pathlib import Path
from typing import Dict
from src.state.schema import AgentState
from hermes_tools import REFLECTION_SCHEMA, evaluator_llm

from src.state.schema import Task, Goal
from hermes_tools import EVALUATOR_SCHEMA
from app.database import SessionLocal
from app.services import MemoryService
from app.models import HermesGoal, HermesTask, HermesMemoryItem
from src.runtime import active_goal_query, project_workspace
from datetime import datetime, timezone
from forge_runtime.telemetry import record_gate_block as _otel_gate_block


def _utcnow() -> datetime:
    """Naive UTC now. DB columns are TIMESTAMP WITHOUT TIME ZONE, so we keep
    timestamps naive while avoiding the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _failure_evidence(output: str, budget: int = 400) -> str:
    """The most actionable slice of a failing test's output for a repair-task
    description. Prioritizes lines containing 'error' (a compiler/test runner
    diagnostic usually names the exact fix), then falls back to the tail.
    An empty output is labeled explicitly — reproduced live (2026-07-06/07,
    C++ postfix sandbox rerun): 32 repair attempts each read
    ``current output: ''`` because the audit command discarded diagnostics,
    and nobody (executor, steward, human reading the log) could tell whether
    the command produced nothing or the harness dropped it."""
    output = (output or "").strip()
    if not output:
        return ("<the command produced NO output — its redirects may be "
                "discarding diagnostics; run the underlying tool without "
                ">/dev/null to see the real error>")
    error_lines = [ln for ln in output.splitlines() if "error" in ln.lower()]
    if error_lines:
        picked = "\n".join(error_lines[:6])[:budget]
        if picked:
            return picked
    return output[-budget:]


def _review_call_with_fallback(prompt: str, schema, max_tokens: int,
                               primary=None, fallback=None) -> dict:
    """Run an LLM review on the cloud model, falling back to the LOCAL model
    on any failure. The local model is always reachable while the loop runs
    at all (it is the executor's own backend), so a cloud outage degrades
    review QUALITY, never review EXISTENCE. Raises only when both fail.

    Reproduced live (2026-07-08, ML experiment-loop goal): ollama.com went
    down (DNS failure) at exactly the final evaluation cycle — both reviews
    returned review_error, failed open (loudly, per Lesson 12), the flag
    memory couldn't help because the executor's last edit produced a NEW
    never-reviewed hash, and a goal with NO results.csv, NO summary.md, and
    NO training loop was marked verified complete on the goal-agnostic
    mechanical template tests alone. Reviewer downtime must not be a review
    bypass for novel content."""
    if primary is None or fallback is None:
        from hermes_tools import evaluator_llm as _p, llm as _f
        primary = primary or _p
        fallback = fallback or _f
    try:
        return primary.generate_json(prompt, schema, max_tokens=max_tokens)
    except Exception as primary_err:
        print(f"🛡️  cloud reviewer unavailable ({str(primary_err)[:80]}) — retrying on the local model.")
        return fallback.generate_json(prompt, schema, max_tokens=max_tokens)


def _semantic_revert_grace(last_eval) -> bool:
    """True when the monotonic ratchet should KEEP a regressing change
    instead of reverting: the state it would restore was itself just blocked
    by a semantic review (scope-incomplete / hardcoded / flag-memory).

    Reverting to semantically-rejected content is provably pointless — the
    flag-memory gate guarantees that exact content can never complete — yet
    that revert is precisely what the ratchet did on every attempt to fix
    the semantic gap that happened to break a test mid-transition.
    Reproduced live (2026-07-07, JSON parser sandbox rerun, 29+ batches):
    scope gate rejects the primitives-only parser → executor writes the
    fuller parser → new parser regresses T3 → ratchet reverts BACK to the
    rejected primitives-only parser → scope gate rejects it again — a
    perfect three-state cycle with the model never getting more than one
    evaluation cycle of forward progress.

    Self-limiting by construction: a semantic block only ever happens on an
    all-tests-green state, so after one grace cycle (tests now red) the next
    evaluation's ``last_eval`` is no longer a semantic block and the normal
    ratchet applies again — this cannot degenerate into never reverting.
    """
    return bool((last_eval or {}).get("semantic_block"))


def _container_mutation_non_discrimination_check(sandbox, workspace: Path, tests: list[dict],
                                                 stack: str, env: dict | None = None) -> tuple[str, ...]:
    """Run the mutation probe inside the active container, never on the host."""
    from forge_runtime.contract_immune import find_source_files

    source_files = find_source_files(workspace, stack)
    if not source_files:
        return ()
    relative_sources = [str(path.relative_to(workspace)) for path in source_files]
    flagged = []
    with sandbox.copied_workspace_for_probe() as probe:
        sandbox.remove_files_from_probe(probe, relative_sources)
        for test in tests:
            result = sandbox.run(test.get("command", ""), cwd=sandbox._p(probe), timeout=60, env=env)
            output = (result.stdout or "") + (result.stderr or "")
            passed = (result.returncode == int(test.get("expect_exit") or 0)
                      and (test.get("expect_substring") or "") in output)
            if passed:
                flagged.append(str(test.get("id", "?")))
    return tuple(flagged)


def evaluator_node(state: AgentState) -> Dict:
    """
    Evaluator Node: Determines the state transition of the graph.
    This node acts as the 'QC / Judge'.
    
    The goal is to ensure the loop persists until the success criteria are met
    or a hard blocker is identified.
    """
    project_id = state.get("project_id")
    if not project_id:
        raise ValueError("evaluator_node requires 'project_id' in state")
    heartbeat = state.get('heartbeat')
    active_task = state.get('active_task')

    if not active_task:
        return {"decision": "continue"}
    # NOTE: even with no heartbeat we do NOT return yet — the independent
    # audit tests below run FIRST, every cycle, and their result decides.

    db = SessionLocal()
    try:
        service = MemoryService(db)
        db_goal = active_goal_query(db, project_id)
        goal = Goal(
            id=db_goal.id,
            title=db_goal.title,
            description=db_goal.description or "",
            status=db_goal.status,
            success_criteria=db_goal.success_criteria or [],
            priority=db_goal.priority
        ) if db_goal else state.get("goal")
    except Exception as e:
        print(f"Error loading goal in evaluator: {e}")
        goal = state.get("goal")
    finally:
        db.close()

    # Goal-level tests describe the final deliverable, not every intermediate
    # task. While planned work remains, a successful fresh task action advances
    # the queue without requiring future files/tests to exist already.
    db = SessionLocal()
    try:
        remaining_planned = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == goal.id,
            HermesTask.id != active_task.id,
            HermesTask.status.in_(("active", "proposed")),
        ).count()
        task_text = f"{active_task.title} {active_task.description}".lower()
        real_write = heartbeat and "TOOL SUCCEEDED: write_file" in heartbeat.progress_summary
        real_test = (heartbeat and "TOOL SUCCEEDED: run_command" in heartbeat.progress_summary
                     and any(word in task_text for word in ("test", "verify", "validation")))
        if remaining_planned and (real_write or real_test):
            try:
                completed = MemoryService(db).complete_task(project_id, active_task.id)
            except ValueError as completion_error:
                return {
                    "decision": "continue",
                    "last_eval": {"reason": str(completion_error), "missing_items": []},
                }
            queue_rows = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
            ).order_by(HermesTask.created_at).all()
            queue = [Task(
                id=t.id, title=t.title, description=t.description or "",
                status=t.status, priority=t.priority,
                next_step=t.description or "",
                acceptance_criteria=t.acceptance_criteria or [],
                attempts=t.attempt_count or 0,
            ) for t in queue_rows]
            done = next(t for t in queue if t.id == completed.id)
            print(f"✅ Atomic task advanced on fresh evidence; {remaining_planned} planned task(s) remain.")
            return {
                "decision": "continue", "active_task": done, "task_queue": queue,
                "last_eval": {
                    "reason": "Atomic task produced fresh durable evidence; advance the planned queue.",
                    "missing_items": [],
                },
            }
    finally:
        db.close()

    # ---- INDEPENDENT VERIFICATION: run the auditor's test list ourselves ----
    # The evaluator does not ask for evidence and does not trust claims: it
    # executes the deterministic test commands from the audit contract and
    # judges from their real output. All pass -> the loop may terminate.
    import subprocess
    from src.nodes.auditor_node import load_audit_tests
    from forge_runtime.sandbox import get_workspace as _get_sandbox
    test_results, tests_all_pass, tests_exist = [], False, False
    db = SessionLocal()
    try:
        audit_tests = load_audit_tests(db, project_id)
        workdir = project_workspace(db, project_id)
    finally:
        db.close()
    # THE SAME sandbox the executor writes into (container by default — see
    # forge_runtime/sandbox.py). Reproduced live (2026-07-06, sandbox rerun):
    # this used to run tests via subprocess directly against the HOST path,
    # which the executor no longer writes to at all once sandboxed — every
    # audit test failed with "No such file or directory" against an empty
    # host directory while the executor's own files sat safely in the
    # container. The evaluator's "ground truth" must look at the same
    # filesystem the executor actually used.
    _sandbox = _get_sandbox(project_id, workdir)
    # Make the project's .venv (and toolchains) visible to every test command
    # in HOST mode only — a ContainerSandbox already has its own PATH with the
    # sandbox image's baked-in toolchains; passing a host env there would be
    # meaningless (and wrong) inside `docker exec`.
    def _test_env(ws):
        from forge_runtime.sandbox import ContainerSandbox as _CS
        if isinstance(_sandbox, _CS):
            return None
        import os as _o
        e = dict(_o.environ)
        ex = [_o.path.join(ws, ".venv", "bin"), _o.path.join(ws, "node_modules", ".bin"),
              "/opt/homebrew/bin", _o.path.expanduser("~/.local/bin"), _o.path.expanduser("~/.cargo/bin")]
        e["PATH"] = ":".join(ex) + ":" + e.get("PATH", "")
        return e
    _tenv = _test_env(workdir)
    if audit_tests:
        tests_exist = True
        for t in audit_tests:
            tid, cmd = t.get("id", "?"), t.get("command", "")
            want_sub = t.get("expect_substring") or ""
            want_exit = int(t.get("expect_exit") or 0)
            try:
                r = _sandbox.run(cmd, timeout=60, env=_tenv)
                out = (r.stdout or "") + (r.stderr or "")
                passed = (r.returncode == want_exit) and (want_sub in out if want_sub else True)
                if not passed and not out.strip():
                    # DIAGNOSTICS RECOVERY (general, all stacks/authors): a
                    # failing test that produced NO output has discarded its
                    # own evidence (`>/dev/null`-style plumbing) — the
                    # failure class behind five separate template patches
                    # (see strip_output_discards). Re-run once with the
                    # discards stripped so the repair loop sees the real
                    # error instead of `current output: ''`.
                    from forge_runtime.contract_immune import strip_output_discards as _strip
                    bare = _strip(cmd)
                    if bare != cmd:
                        try:
                            r2 = _sandbox.run(bare, timeout=60, env=_tenv)
                            recovered = ((r2.stdout or "") + (r2.stderr or "")).strip()
                            if recovered:
                                out = f"[recovered diagnostics — the test's own redirects discard output]\n{recovered}"
                        except Exception:
                            pass
                test_results.append({"id": tid, "command": cmd, "passed": passed,
                                     "exit": r.returncode, "output": out[-700:]})
            except Exception as run_err:
                test_results.append({"id": tid, "command": cmd, "passed": False,
                                     "exit": -1, "output": f"runner error: {run_err}"[:250]})
        # (recomputed below after the sub-test scan may override a parent)
        tests_all_pass = all(tr["passed"] for tr in test_results)

        # Sub-test granularity for the ratchet: a unittest-based audit test is
        # really N unit tests. Without this, swapping WHICH unit test fails
        # (food_growth <-> movement_directions, observed for hours) never
        # registers as a regression. Run verbose discovery fresh and track
        # each unit test as its own ratchet entry.
        import re as _re
        for t in audit_tests:
            if "unittest" not in t.get("command", ""):
                continue
            try:
                rv = _sandbox.run("python3 -m unittest discover -v 2>&1", timeout=60, env=_tenv)
                for m in _re.finditer(r"^(\w+) \([^)]+\) \.\.\. (ok|FAIL|ERROR)",
                                      (rv.stdout or "") + (rv.stderr or ""), _re.M):
                    name, verdict = m.group(1), m.group(2)
                    test_results.append({"id": f"{t.get('id','T?')}::{name}",
                                         "command": f"unit:{name}",
                                         "passed": verdict == "ok",
                                         "exit": 0 if verdict == "ok" else 1,
                                         "output": verdict})
            except Exception as sub_err:
                print(f"sub-test scan failed: {sub_err}")
            # GROUND-TRUTH OVERRIDE: if every unit test passes in OUR verbose
            # scan but the contract's parent command failed, the parent COMMAND
            # is broken (wrong module form, invented test counts) — we trust
            # what we executed ourselves.
            subs = [r for r in test_results if r["id"].startswith(f"{t.get('id','T?')}::")]
            parent = next((r for r in test_results if r["id"] == t.get("id")), None)
            if parent and not parent["passed"] and subs and all(r["passed"] for r in subs):
                print(f"🔧 Parent test {parent['id']} command is broken (all {len(subs)} "
                      "unit tests pass in direct execution) — overriding to PASS.")
                parent["passed"] = True
                parent["output"] = f"overridden: {len(subs)} unit tests pass via direct discover -v"
            break
        # Persist every run as durable evidence.
        db = SessionLocal()
        try:
            svc_ev = MemoryService(db)
            for tr in test_results:
                svc_ev.record_test_run(
                    project_id=project_id, task_id=None,
                    command=tr["command"],
                    status="success" if tr["passed"] else "failure",
                    output_summary=tr["output"])
            db.commit()
        except Exception as ev_err:
            print(f"Could not persist test evidence: {ev_err}")
        finally:
            db.close()
        tests_all_pass = all(tr["passed"] for tr in test_results)
        summary = ", ".join(f"{tr['id']}:{'PASS' if tr['passed'] else 'FAIL'}" for tr in test_results)
        print(f"🔬 Evaluator ran {len(test_results)} audit tests itself -> {summary}")

        # ---- MONOTONIC RATCHET (regression rollback) ----
        # The 12B executor rewrites whole files, re-rolling the dice on every
        # previously-fixed bug (observed: T4 green -> regressed -> T2 green for
        # hours -> regressed by an architecture rewrite). Git-checkpoint the
        # workspace at each evaluation; if a change makes a previously-passing
        # test fail, REVERT it deterministically and tell the executor.
        def _git(*a):
            # Runs INSIDE the same sandbox as the executor's files — a
            # host-side `git -C workdir` would checkpoint/revert an empty
            # host directory once sandboxed (see the audit-test fix above for
            # the identical failure mode). The ratchet's history now lives in
            # the sandbox's own persistent volume for the container's lifetime.
            return _sandbox.run(["git", *a], timeout=30)
        try:
            if _git("rev-parse", "--git-dir").returncode != 0:
                _git("init"); _git("add", "-A")
                _git("-c", "user.email=pge@local", "-c", "user.name=pge", "commit", "-m", "baseline", "--allow-empty")
            prev_pass = set(state.get("last_pass_ids") or [])
            now_pass = {tr["id"] for tr in test_results if tr["passed"]}
            regressed = sorted(prev_pass - now_pass)
            has_commit = _git("rev-parse", "HEAD").returncode == 0
            if regressed and has_commit:
                if _semantic_revert_grace(state.get("last_eval")):
                    # FORWARD-FIX GRACE: the state this revert would restore
                    # was just rejected by a semantic review — restoring it
                    # cannot ever complete (the flag-memory gate guarantees
                    # that), so keep the more complete attempt and drive the
                    # executor to repair the tests it broke. Ratchet baseline
                    # resets to what currently passes; the old checkpoint
                    # stays in git history, it just isn't auto-restored.
                    _failing = [tr for tr in test_results if not tr["passed"]]
                    _diags = "; ".join(f"{tr['id']}: {_failure_evidence(tr['output'], 250)}" for tr in _failing[:3])
                    print(f"↷ KEEPING forward attempt despite regressed {regressed}: the revert target "
                          "was just semantically rejected (incomplete/hardcoded) — reverting to it cannot converge.")
                    return {"decision": "continue", "last_pass_ids": sorted(now_pass),
                            "test_fail_streaks": dict(state.get("test_fail_streaks") or {}),
                            "last_eval": {"reason": (
                                f"Your previous (test-passing) version was REJECTED as incomplete/hardcoded, "
                                f"so it has NOT been restored — your new, more complete implementation is KEPT "
                                f"even though it currently fails {regressed}. Do NOT go back to the old version. "
                                f"Fix the failures in the NEW code now: {_diags}"),
                                "missing_items": [tr["id"] for tr in _failing]}}
                _git("checkout", "--", "."); _git("clean", "-fd")
                print(f"⏪ REVERTED workspace: change regressed previously-passing {regressed}.")
                # Re-run only bookkeeping: previous state restored, so the old
                # pass-set stands; tell the executor what happened.
                return {"decision": "continue", "last_pass_ids": sorted(prev_pass),
                        "test_fail_streaks": dict(state.get("test_fail_streaks") or {}),
                        "last_eval": {"reason": (
                            f"Your last change was AUTOMATICALLY REVERTED because it broke "
                            f"previously-passing audit test(s) {regressed}. The workspace is "
                            f"back to the last good state. Make a MINIMAL, targeted change "
                            f"for the still-failing tests ONLY — do not rewrite working files. "
                            f"If the failing tests are YOUR unit tests, update the unit tests' "
                            f"expectations to match the current (acceptance-passing) program "
                            f"behavior — acceptance tests outrank unit tests."),
                            "missing_items": [tr["id"] for tr in test_results if not tr["passed"]]}}
            # Commit a new baseline ONLY when no previously-passing entry has
            # vanished — a transient deletion state once became the baseline
            # and legitimized everything after it.
            now_ids_all = {tr["id"] for tr in test_results}
            if now_pass >= prev_pass and not (prev_pass - now_ids_all):
                _git("add", "-A")
                _git("-c", "user.email=pge@local", "-c", "user.name=pge", "commit", "-m",
                     f"checkpoint pass={sorted(now_pass)}", "--allow-empty")
        except Exception as ratchet_err:
            print(f"Ratchet error (non-fatal): {ratchet_err}")

    # Suspect-test circuit breaker: a test failing with IDENTICAL output for
    # 4 consecutive evaluations, while at least one other test passes, is
    # almost certainly an unsatisfiable contract item (platform quirk,
    # impossible expectation). Spinning on it burns the whole budget — block
    # for human steer instead and name the suspect.
    # DETERMINISTIC STEERING: a test that failed 2+ consecutive cycles gets an
    # explicit repair task. Without this the executor keeps polishing its
    # planner-assigned task while the actual contract gap has no owner (seen
    # live: T4 "NO TESTS RAN" failed 16 cycles because no task said
    # "write the test files").
    streaks = dict(state.get("test_fail_streaks") or {})
    if test_results:
        any_pass = any(tr["passed"] for tr in test_results)
        suspects = []
        for tr in test_results:
            if tr["passed"]:
                streaks.pop(tr["id"], None)
                continue
            prev = streaks.get(tr["id"]) or {"count": 0, "output": None}
            count = prev["count"] + 1 if prev["output"] == tr["output"] else 1
            streaks[tr["id"]] = {"count": count, "output": tr["output"]}
            if count >= 4 and any_pass:
                suspects.append(tr)
        repair_candidates = [tr for tr in test_results
                             if not tr["passed"] and (streaks.get(tr["id"]) or {}).get("count", 0) >= 2]
        if repair_candidates:
            db = SessionLocal()
            try:
                from app.models import HermesGoal as _HG
                _g = db.query(_HG).filter(_HG.project_id == project_id).first()
                existing_titles = {t.title: t.status for t in
                                   db.query(HermesTask).filter(HermesTask.project_id == project_id).all()}
                made = 0
                for tr in repair_candidates[:3]:
                    title = f"Make audit test {tr['id']} pass"
                    prior = existing_titles.get(title)
                    if prior in ("proposed", "active"):
                        continue
                    # FAILURE-TRIGGERED RESEARCH (harness-initiated): search
                    # the web for this error signature and embed the findings
                    # directly in the repair task. Models don't reliably
                    # self-correct without external information (Huang et
                    # al., ICLR 2024; CRITIC, Gou et al.) and this executor
                    # empirically NEVER initiates research on its own (0
                    # invocations across 26 runs / ~1300 tool calls, incl. 32
                    # attempts on one error) — so the harness pushes the
                    # knowledge to the impasse instead of waiting for the
                    # model to pull it. Cached by query: repeat cycles on the
                    # same error cost nothing.
                    _research = ""
                    try:
                        from src.auditor import web_search_cached as _wsc
                        _sig = _failure_evidence(tr["output"], 120)
                        if _sig and not _sig.startswith("<the command produced NO output"):
                            _hits = _wsc(_sig)
                            if _hits:
                                _research = f"\nWEB RESEARCH on this error (use web_search/fetch_doc for more):\n{_hits[:900]}"
                    except Exception:
                        pass
                    if prior == "blocked":
                        # Resurrect: a retired repair task must come back while
                        # its test still fails, or contract repair dies after
                        # the first attempt-cap retirement (observed live).
                        row = (db.query(HermesTask)
                               .filter(HermesTask.project_id == project_id,
                                       HermesTask.title == title).first())
                        if row:
                            row.status = "proposed"
                            row.description = (
                                f"STILL FAILING. Audit test {tr['id']} MUST pass: "
                                f"command `{tr['command']}` — current output: "
                                f"{_failure_evidence(tr['output'])!r}. Make the MINIMAL change; "
                                "do not rewrite working files." + _research)
                            made += 1
                        continue
                    if "::" in tr["id"]:
                        # A unit test failing while acceptance criteria hold:
                        # the FIX TARGET is the test file, never the program —
                        # the 12B otherwise anchors on changing the program and
                        # the ratchet revert-loops forever (observed live).
                        desc = (f"Unit test {tr['id'].split('::')[1]} fails: {tr['output'][:150]!r}. "
                                "EDIT ONLY THE TEST FILE (test_*.py): change ITS expectations "
                                "to match the program's CURRENT output (which already passes the "
                                "acceptance tests). You are FORBIDDEN from modifying any non-test "
                                "file for this task — such changes get auto-reverted.")
                    else:
                        desc = (f"The audit contract test {tr['id']} keeps failing and MUST pass: "
                                f"command `{tr['command']}` must succeed"
                                f" — current output: {_failure_evidence(tr['output'])!r}. "
                                "Create or modify whatever files are needed to make this real "
                                "command pass in the project workspace." + _research)
                    MemoryService(db).create_task(
                        project_id=project_id, goal_id=_g.id if _g else None, title=title,
                        description=desc, status="proposed", priority=1)
                    made += 1
                if made:
                    db.commit()
                    print(f"⚖️  Spawned {made} contract-repair task(s) for failing tests.")
            except Exception as rt_err:
                print(f"⚖️  Could not create repair task: {rt_err}")
            finally:
                db.close()

        if suspects:
            names = ", ".join(f"{tr['id']} `{tr['command']}`" for tr in suspects)
            print(f"🚧 Suspect unsatisfiable test(s): {names} — blocking for human review.")
            return {"decision": "blocked", "test_fail_streaks": streaks,
                    "last_eval": {"reason": f"Contract test(s) {names} failed identically 4+ "
                                            "cycles while other tests pass — likely unsatisfiable "
                                            "as written. Human should fix or void these items "
                                            "(PGE_FORCE_AUDIT=1 regenerates the contract).",
                                  "missing_items": [tr["id"] for tr in suspects]}}

        failing = [tr for tr in test_results if not tr["passed"]]
        if failing:
            db = SessionLocal()
            try:
                existing = db.query(HermesMemoryItem).filter(
                    HermesMemoryItem.project_id == project_id,
                    HermesMemoryItem.task_id == active_task.id,
                    HermesMemoryItem.memory_type == "mistake",
                ).order_by(HermesMemoryItem.created_at.desc()).first()
                content = "FAILED VERIFICATION: " + "; ".join(
                    f"{tr['id']} `{tr['command']}` exit={tr['exit']} output={tr['output'][:160]}"
                    for tr in failing[:4])
                if not existing or existing.content != content:
                    service = MemoryService(db)
                    service.record_memory_item(
                        project_id=project_id, task_id=active_task.id,
                        memory_type="mistake", content=content,
                        importance=5, tags=["auto", "verification-failure"],
                    )
                    service.record_learning_failure(project_id, active_task.id, failing)
                # Durable lesson extraction: on the SECOND-plus occurrence of
                # the same failure fingerprint, distill observation+prevention
                # deterministically and upsert (bump, never duplicate) so the
                # next run starts already knowing this failure mode.
                try:
                    from forge_runtime.lessons import (
                        FailureEvent,
                        extract_lesson,
                        fingerprint,
                        should_extract,
                    )
                    from forge_runtime.lesson_store_db import DbLessonStore
                    from forge_runtime.steering import Lesson
                    import json as _json
                    store = DbLessonStore(db, project_id)
                    for tr in failing[:4]:
                        sig = (tr.get("output") or "")[:160]
                        event = FailureEvent(
                            task_id=active_task.id,
                            failure_type="verification",
                            test_id=tr["id"],
                            error_signature=sig,
                        )
                        fp = fingerprint(event.failure_type, event.test_id, event.error_signature)
                        # Reconstruct exact failure events from durable
                        # learning_fail evidence. Counting by test id alone
                        # incorrectly triggered reflection when unrelated
                        # errors happened in the same test.
                        observed_events = []
                        prior_rows = db.query(HermesMemoryItem).filter(
                            HermesMemoryItem.project_id == project_id,
                            HermesMemoryItem.memory_type == "learning_fail",
                        ).all()
                        for row in prior_rows:
                            try:
                                evidence = _json.loads(row.content).get("evidence", {})
                                for failed in evidence.get("failed_tests", []):
                                    candidate = FailureEvent(
                                        task_id=active_task.id,
                                        failure_type="verification",
                                        test_id=str(failed.get("id", "")),
                                        error_signature=(failed.get("output") or "")[:160],
                                    )
                                    observed_events.append(candidate)
                            except (TypeError, ValueError, AttributeError, _json.JSONDecodeError):
                                continue
                        if not should_extract(observed_events, fp):
                            continue  # extract only on recurrence (bounds cost)
                        def _extract(_event):
                            prompt = (
                                "In two sentences, explain why this coding attempt failed "
                                "and what the next attempt should do differently. Do not "
                                "restate the error.\n"
                                f"Test: {_event.test_id}\nCommand: {tr['command']}\n"
                                f"Failure output: {_event.error_signature}\n"
                                f"Last executor actions: {(state.get('last_actions') or [])[-4:]}"
                            )
                            return evaluator_llm.generate_json(prompt, REFLECTION_SCHEMA, max_tokens=300)

                        lesson = None
                        try:
                            lesson = extract_lesson(event, _extract, evidence_ids=(tr["id"],))
                        except Exception:
                            lesson = None
                        if lesson is None:
                            # The shipped template is the deterministic,
                            # never-blocking fallback when the model is down or
                            # returns a vacuous reflection.
                            lesson = Lesson(
                                fingerprint=fp,
                                task_id=active_task.id,
                                failure_type="verification",
                                observation=f"Test {tr['id']} `{tr['command']}` keeps failing.",
                                prevention=f"Before claiming done, run `{tr['command']}` and fix this exact failure first.",
                                evidence_ids=(tr["id"],),
                            )
                        store.upsert(lesson)
                except Exception as lesson_err:
                    print(f"Lesson extraction skipped: {lesson_err}")
            except Exception as memory_err:
                print(f"Could not persist verification mistake: {memory_err}")
            finally:
                db.close()

    # ---- DETERMINISTIC-FIRST: commit the verdict the tests force, before any
    # LLM involvement. The tests have already run for THIS cycle.
    if tests_exist and tests_all_pass:
        db = SessionLocal()
        try:
            MemoryService(db).promote_verified_learning(
                project_id, active_task.id, test_results)
        finally:
            db.close()
        # VANISHING-TEST GUARD: completion requires every PREVIOUSLY-passing
        # ratchet entry to still exist and pass. The 12B once DELETED the two
        # unreconcilable unit tests — the suite shrank past this gate and the
        # goal closed while the real suite failed. Absent = regression.
        _prev_ok = set(state.get("last_pass_ids") or [])
        _now_ids = {tr["id"] for tr in test_results}
        _vanished = sorted(_prev_ok - _now_ids)
        if _vanished:
            _otel_gate_block("vanishing_test_guard")
            print(f"🛑 Completion BLOCKED: previously-passing test(s) vanished: {_vanished} — reverting.")
            _sandbox.run(["git", "checkout", "--", "."], timeout=30)
            _sandbox.run(["git", "clean", "-fd"], timeout=30)
            return {"decision": "continue", "last_pass_ids": sorted(_prev_ok),
                    "last_eval": {"reason": (
                        f"Your change DELETED previously-passing tests {_vanished} — forbidden, "
                        "auto-reverted. Make failing tests pass by FIXING THE SOURCE so the "
                        "goal-stated behavior holds — never by removing tests or weakening "
                        "their expectations."),
                        "semantic_block": True,
                        "missing_items": _vanished}}
        print("✅ All audit tests PASSED for the current state.")

        # Everything below (mutation gate, vacuous-test scan, overfitting/
        # scope-completeness reviews, semantic-flag hashing) reads the
        # workspace via plain Path objects (forge_runtime/contract_immune.py)
        # — it has no notion of the sandbox. Mirror the container's current
        # content down to the host path once here so those gates see what
        # was actually built, not an empty (or stale) host directory.
        try:
            _sandbox.sync_to_host(workdir)
        except Exception as _sync_err:
            _otel_gate_block("sandbox_sync")
            print(f"🛑 Completion DEFERRED: sandbox->host sync failed ({str(_sync_err)[:120]}).")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        "The sandbox result could not be synchronized for downstream verification. "
                        "The host workspace was preserved; completion is deferred until a staged sync succeeds."),
                        "missing_items": []}}

        # CONTRACT MUTATION GATE (specs/convergent-autonomous-harness.html
        # Phase 2). Reproduced live 2026-07-03: fizzbuzz.py was correct, but
        # the executor's own test file was `def test_pass(): assert True` —
        # every audit test (file exists / compiles / pytest exits 0) passed,
        # and the goal was declared verified without a single test actually
        # exercising fizzbuzz's logic. Before trusting an all-pass verdict,
        # delete the non-test source files in a COPY of the workspace and
        # rerun the same tests there.
        #
        # BLOCK ONLY IF NOT A SINGLE TEST FAILS on the mutant — matching the
        # Phase 2 spec ("assert >=1 test fails on the mutant; if none do,
        # flag non-discriminating"), not "any individual test still passes".
        # A first version of this gate blocked on ANY flagged test and
        # deadlocked on every python goal in practice: T1 (`ls *.py`) and T2
        # (compiles) are template-generated PRECONDITION checks that will
        # always still pass once the mutant's own test file remains (it is
        # itself a valid, compiling .py file) — that is expected, not a
        # defect, as long as the test-EXECUTION check (T3: pytest/unittest)
        # correctly fails once the code it imports is gone. Reproduced live
        # 2026-07-03 (LRU cache goal): T1/T2 survived, T3 correctly died on
        # import — the per-test-strict gate blocked completion forever on a
        # genuinely correct implementation with genuinely meaningful tests.
        try:
            from src.auditor import detect_stack as _detect_stack
            from forge_runtime.sandbox import ContainerSandbox as _ContainerSandbox
            _mstack = _detect_stack(f"{goal.title} {goal.description or ''}").get("language") or "python"
            if not isinstance(_sandbox, _ContainerSandbox):
                raise RuntimeError("mutation gate requires container sandbox mode")
            _flagged = _container_mutation_non_discrimination_check(
                _sandbox, Path(workdir), audit_tests, stack=_mstack, env=_tenv)
        except Exception as _mutation_err:
            _otel_gate_block("mutation_unavailable")
            print(f"🛑 Completion DEFERRED: mutation gate unavailable ({str(_mutation_err)[:80]}).")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        "The mutation gate requires a healthy container sandbox and was not run. "
                        "Completion is deferred rather than executing generated tests on the host."),
                        "missing_items": []}}
        if _flagged and len(_flagged) >= len(audit_tests):
            _otel_gate_block("mutation")
            print(f"🛑 Completion BLOCKED: every audit test {list(_flagged)} still passes with the source "
                  "deleted — the contract cannot tell done from broken.")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"All audit tests {list(_flagged)} passed independently, but ALSO all pass with "
                        "the implementation source files deleted — none of them actually exercise the "
                        "deliverable (e.g. a test body like `assert True`). Rewrite at least one test to "
                        "import/exercise the real code and assert on real behavior before the goal can "
                        "be marked complete."),
                        "semantic_block": True,
                        "missing_items": list(_flagged)}}
        elif _flagged:
            print(f"🛡️  mutation gate note: test(s) {list(_flagged)} are precondition-style checks that "
                  "survive source deletion — not blocking, since at least one other test correctly failed.")

        # VACUOUS-TEST GATE (specs/convergent-autonomous-harness.html Phase 2,
        # complementary to the mutation gate above). Reproduced live
        # 2026-07-03 (RPN calculator goal): main() was `pass` — no parsing,
        # no arithmetic, none of the actual goal — yet the mutation gate
        # above did NOT block it. Its test did `from main import main`, so
        # deletion broke the import and the test correctly failed on the
        # mutant — which makes "fails once the source is gone" a NECESSARY
        # but not SUFFICIENT proof of a meaningful test: `assert True` and
        # `assert 1 + 1 == 2` could pass against ANY implementation, correct
        # or not, as long as the module merely imports. This statically
        # proves (never guesses) two independent failure modes: python
        # assertions whose truth value is fixed at parse time from literals
        # alone (AST-based), and — reproduced live again 2026-07-03 (email
        # validator goal) — a JS/TS test file that registers ZERO actual
        # test cases at all (regex-based; `test.js` was a bare
        # `process.exit(0)`, no `test()`/`it()`/`describe()` call anywhere).
        try:
            from forge_runtime.contract_immune import scan_workspace_for_vacuous_tests as _vacuous_scan
            _vacuous = _vacuous_scan(Path(workdir), stack=_mstack)
        except Exception as _vacuous_err:
            print(f"🛡️  vacuous-test gate failed to run ({str(_vacuous_err)[:80]}) — not blocking completion.")
            _vacuous = {}
        if _vacuous:
            _summary = ", ".join(f"{f}: {list(markers)}" for f, markers in _vacuous.items())
            _otel_gate_block("vacuous_test")
            print(f"🛑 Completion BLOCKED: vacuous test(s) found — {_summary}. "
                  "None of these can ever fail regardless of whether the implementation is correct.")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"Test(s) flagged as vacuous: {_summary}. Depending on the marker: a python "
                        "function whose every assertion is a compile-time constant (e.g. `assert True`, "
                        "`assert 1 + 1 == 2`) can never fail no matter what the implementation does; a "
                        "`<no test cases registered>` JS/TS file never calls `test()`/`it()`/`describe()` "
                        "at all (e.g. it's just a script that exits 0); a `<no assertion calls found>` "
                        "JS/TS file (or `<crate>` for rust) registers tests but never calls "
                        "`assert`/`expect`/`assert!`/`assert_eq!`; a `<no #[test] functions found>` "
                        "rust crate has zero test functions anywhere, so `cargo test` trivially reports "
                        "`0 passed; 0 failed` and exits 0. Rewrite these to call the real code under test "
                        "and assert on its actual return value or behavior before the goal can be marked "
                        "complete."),
                        "semantic_block": True,
                        "missing_items": [f"{f}::{m}" for f, markers in _vacuous.items() for m in markers]}}

        # OVERFITTING REVIEW (specs/convergent-autonomous-harness.html Phase
        # 2, LLM-based — complementary to every mechanical gate above).
        # Reproduced live (2026-07-04, JSON parser goal): the "parser" only
        # handled two literal strings copied from its own test file and
        # crashed on every real JSON input — mechanically indistinguishable
        # from genuine logic to the mutation gate (deletion breaks the real
        # import) and the vacuous-assertion gate (the assertion depends on a
        # real function call). Needs semantic judgment: ask evaluator_llm — a
        # DIFFERENT, stronger cloud model than whatever wrote the code, with
        # fresh context — whether the implementation is hardcoded to the
        # visible tests rather than a genuine general algorithm.
        # GOAL-ARTIFACT EXISTENCE (deterministic, before any LLM review —
        # cheap, and no judgment involved). Reproduced live (2026-07-08, ML
        # loop v3 — the incident that survived every review improvement):
        # the goal named results.csv and summary.md; the code that WOULD
        # produce them existed (so scope review counted the capability as
        # attempted), but the files never did — the executor's test even
        # fabricated results.csv, asserted on it, and deleted it. Whether a
        # goal-named file exists is not a judgment call.
        try:
            from forge_runtime.contract_immune import missing_goal_artifacts as _missing_artifacts
            _missing = _missing_artifacts(Path(workdir), f"{goal.title} {goal.description or ''}")
        except Exception as _ma_err:
            print(f"🛡️  goal-artifact check failed to run ({str(_ma_err)[:80]}) — not blocking completion.")
            _missing = ()
        if _missing:
            _otel_gate_block("goal_artifact_missing")
            print(f"🛑 Completion BLOCKED: goal-named artifact(s) missing from the workspace: {list(_missing)}")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"The goal explicitly names deliverable file(s) that do not exist anywhere in "
                        f"the workspace: {list(_missing)}. Having code that WOULD produce them is not "
                        "enough — actually RUN your pipeline so the files exist and persist (do not "
                        "create them inside a test that deletes them afterwards)."),
                        "semantic_block": True,
                        "missing_items": list(_missing)}}

        # SEMANTIC-FLAG MEMORY (shared by both LLM reviews below). Content a
        # semantic gate has already rejected must not complete unchanged —
        # regardless of whether this cycle's cloud call fails or the model
        # answers differently. Reproduced live twice (2026-07-05, LRU-cache
        # and JSON-parser reruns): the vanishing-test guard's auto-revert
        # restored exactly the content the scope gate had blocked (9
        # consecutive times, in the JSON case), and the goal then completed
        # against it. The hash covers precisely what the reviews are shown,
        # so any real fix changes it and clears the block.
        try:
            from forge_runtime.contract_immune import (
                review_content_hash as _review_hash_fn,
                load_semantic_flags as _load_flags,
                record_semantic_flag as _record_flag,
            )
            _review_hash = _review_hash_fn(Path(workdir), stack=_mstack)
            _known_flags = _load_flags(Path(workdir))
        except Exception as _flag_err:
            print(f"🛡️  semantic-flag memory unavailable ({str(_flag_err)[:80]}).")
            _review_hash, _known_flags = "", {}
            _record_flag = lambda *a, **k: None  # noqa: E731

        _review_outage = False
        try:
            from forge_runtime.contract_immune import review_for_hardcoded_implementation as _overfit_review
            from hermes_tools import OVERFIT_SCHEMA as _OVERFIT_SCHEMA

            def _llm_review(prompt: str) -> dict:
                return _review_call_with_fallback(prompt, _OVERFIT_SCHEMA, 1000)

            _overfit = _overfit_review(Path(workdir), goal.title, goal.description or "", _llm_review, stack=_mstack)
        except Exception as _overfit_err:
            print(f"🛡️  overfitting review failed to run ({str(_overfit_err)[:80]}) — not blocking completion.")
            _overfit = {}
        if _overfit.get("review_error"):
            # Visible fail-open: the reviewer never answered — and BOTH the
            # cloud and local fallback models failed, which normally means
            # the loop itself is about to die (the local model is the
            # executor's own backend). The flag-memory check below still
            # refuses known-rejected content, and the outage flag defers
            # completion of never-reviewed content (live incident 2026-07-08,
            # ML experiment-loop goal: an ollama.com outage at the final
            # cycle let a goal with none of its deliverables complete).
            _review_outage = True
            print(f"🛡️  overfitting review UNAVAILABLE ({_overfit['review_error'][:120]}) — "
                  "no verdict this cycle; completion will be deferred.")
        elif _overfit.get("is_hardcoded"):
            _otel_gate_block("overfitting")
            print(f"🛑 Completion BLOCKED: implementation flagged as hardcoded to the tests — {_overfit.get('reasoning', '')[:200]}")
            _record_flag(Path(workdir), _review_hash, f"hardcoded: {_overfit.get('reasoning', '')}")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"An independent cloud-model review flagged this implementation as hardcoded to "
                        f"the visible test cases rather than a genuine general solution: "
                        f"{_overfit.get('reasoning', '')} Suspicious code: "
                        f"{_overfit.get('suspicious_snippets', [])}. Rewrite the implementation to handle "
                        "the general case, not just the literal values used in the tests."),
                        "semantic_block": True,
                        "missing_items": list(_overfit.get("suspicious_snippets") or [])}}

        # SCOPE-COMPLETENESS REVIEW (specs/convergent-autonomous-harness.html
        # Phase 2, LLM-based — same infrastructure as the overfitting review
        # above, a different question). The deterministic per-stack
        # templates are intentionally GOAL-AGNOSTIC ("some source compiles,
        # some test suite passes") for reliability. Reproduced live
        # (2026-07-04, Dijkstra goal): the goal explicitly asked for
        # "Dijkstra's shortest path algorithm... returning shortest distance
        # and path" with tests for "multiple paths, disconnected nodes, and
        # a single-node graph" — the loop instead built and independently
        # verified only a Graph data structure (itself genuine, non-hardcoded
        # work — the overfitting review above correctly does NOT catch this)
        # and stopped, because the contract never required Dijkstra's
        # algorithm to exist. Ask evaluator_llm whether a core capability the
        # goal explicitly asked for is completely missing from the
        # implementation — never flags gaps in something that IS attempted
        # (that's test-quality territory, covered by the gates above).
        try:
            from forge_runtime.contract_immune import review_for_goal_scope_completeness as _scope_review
            from hermes_tools import SCOPE_SCHEMA as _SCOPE_SCHEMA

            def _llm_scope_review(prompt: str) -> dict:
                return _review_call_with_fallback(prompt, _SCOPE_SCHEMA, 1200)

            _scope = _scope_review(Path(workdir), goal.title, goal.description or "", _llm_scope_review, stack=_mstack)
        except Exception as _scope_err:
            print(f"🛡️  scope-completeness review failed to run ({str(_scope_err)[:80]}) — not blocking completion.")
            _scope = {}
        if _scope.get("review_error"):
            _review_outage = True
            print(f"🛡️  scope-completeness review UNAVAILABLE ({_scope['review_error'][:120]}) — "
                  "no verdict this cycle; completion will be deferred.")
        elif _scope.get("is_scope_incomplete"):
            _otel_gate_block("scope_completeness")
            print(f"🛑 Completion BLOCKED: goal scope incomplete — {_scope.get('reasoning', '')[:200]}")
            _record_flag(Path(workdir), _review_hash, f"scope incomplete: {_scope.get('reasoning', '')}")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"An independent cloud-model review found the implementation missing core "
                        f"capabilities the goal explicitly asked for: {_scope.get('reasoning', '')} "
                        f"Missing: {_scope.get('missing_requirements', [])}. Implement these before "
                        "the goal can be marked complete — the tests passing so far only cover what "
                        "was already built, not the full stated goal. Do NOT edit or weaken test "
                        "expectations to match current behavior: this review compares the tests "
                        "against the GOAL TEXT, so a weakened test is itself a scope gap and will "
                        "keep blocking. Change the SOURCE so the goal-stated behavior holds."),
                        "semantic_block": True,
                        "missing_items": list(_scope.get("missing_requirements") or [])}}

        # FLAG-MEMORY GATE: neither review blocked this cycle — but if the
        # exact review payload matches content a semantic gate rejected
        # earlier (typically because the vanishing-test guard reverted to
        # it), refuse completion until the content actually changes. This is
        # what turns "the reviewer was down / changed its mind" from a
        # completion path into a retry.
        if _review_hash and _review_hash in _known_flags:
            _prior_reason = _known_flags[_review_hash]
            _otel_gate_block("semantic_flag_memory")
            print(f"🛑 Completion BLOCKED: workspace content is byte-identical to a state a semantic "
                  f"review already rejected — prior verdict: {_prior_reason[:200]}")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        f"This exact source+test content was previously rejected by an independent "
                        f"review and has not changed since (an automatic revert may have restored it): "
                        f"{_prior_reason} Address that verdict with a NEW, minimal change — if your "
                        "earlier fix was auto-reverted for regressing an audit test, redo the fix "
                        "without breaking that test (e.g. keep function signatures compatible)."),
                        "semantic_block": True,
                        "missing_items": []}}

        # REVIEW-OUTAGE DEFERRAL: the mechanical tests pass and no gate
        # blocked — but if the semantic reviews never actually ran this
        # cycle (both cloud AND local reviewer failed), completing now would
        # mean shipping never-reviewed content on the goal-agnostic template
        # tests alone. Reproduced live (2026-07-08, ML experiment-loop
        # goal): an ollama.com DNS outage at exactly the final cycle let a
        # goal with NO results.csv, NO summary.md, and NO training loop
        # complete as "verified". Withholding the completion CLAIM is not
        # blocking the loop (Lesson 10's complete_unverified distinction):
        # the run continues, and the review retries next cycle — a
        # persistent total outage ends the run at the turn ceiling as
        # incomplete, which is the honest answer.
        if _review_outage:
            _otel_gate_block("review_outage")
            print("🛑 Completion DEFERRED: semantic reviews unavailable this cycle (cloud and local "
                  "reviewer both failed) — never-reviewed content cannot complete; retrying next cycle.")
            return {"decision": "continue", "last_pass_ids": sorted({tr["id"] for tr in test_results}),
                    "last_eval": {"reason": (
                        "All mechanical audit tests pass, but the independent semantic verification "
                        "could not run this cycle (reviewer backend unavailable). Completion is "
                        "deferred — no action needed on the code; the verification retries "
                        "automatically next cycle."),
                        "missing_items": []}}

        db = SessionLocal()
        try:
            try:
                MemoryService(db).complete_task(project_id, active_task.id)
            except Exception:
                row = db.query(HermesTask).filter(HermesTask.id == active_task.id).first()
                if row:
                    row.status = "completed"
                    row.completed_at = _utcnow()
                    db.commit()
            db_tasks = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
            ).all()
            queue = [Task(id=t.id, title=t.title, description=t.description or "",
                          status=t.status, priority=t.priority,
                          next_step=t.description or "") for t in db_tasks]
            done = next((t for t in queue if t.id == active_task.id), active_task)
            remaining = [t for t in db_tasks
                         if t.id != active_task.id and t.status in ("active", "proposed")]
            # THE AUDIT TESTS ARE THE GOAL'S DEFINITION OF DONE. When every
            # test passes, leftover planner tasks are obsolete means, not
            # ends — continuing to "finish" them makes the executor edit
            # working code and REGRESS passing tests (observed live: all 5
            # tests green at turn 4, regressed by turn 6). Retire them.
            for t in remaining:
                t.status = "obsolete"
            g = active_goal_query(db, project_id)
            if g:
                g.status = "completed"
            db.commit()
        finally:
            db.close()
        if remaining:
            print(f"🏁 GOAL verified complete (all audit tests pass) — "
                  f"{len(remaining)} leftover task(s) marked obsolete.")
        else:
            print("🏁 GOAL verified complete — all audit tests pass.")
        return {"decision": "complete",
                "goal_verified": True,
                "task_queue": queue, "active_task": done,
                "last_eval": {"reason": "All audit tests passed on independent execution: "
                                        + ", ".join(tr["id"] for tr in test_results),
                              "missing_items": []}}

    if not heartbeat:
        # No executor report this turn — but the tests above already ran, so
        # feed their REAL results forward instead of repeating blindly.
        failing = [tr for tr in test_results if not tr["passed"]]
        reason = "Executor produced no heartbeat this turn (treat as zero progress). "
        if failing:
            reason += "Independent tests still failing: " + "; ".join(
                f"{tr['id']} `{tr['command']}` exit={tr['exit']} out={tr['output'][:100]!r}"
                for tr in failing[:4])
        else:
            reason += "Try a DIFFERENT, smaller concrete action."
        return {"decision": "continue",
                "last_eval": {"reason": reason,
                              "missing_items": [tr["id"] for tr in failing]}}

    # Gather REAL evidence for the active task: recorded file changes (verified
    # against disk) and test runs. The evaluator judges THIS, not the
    # executor's self-report.
    from app.models import HermesFileChange, HermesTestRun
    db = SessionLocal()
    try:
        # Project-wide: contract items are usually satisfied by work done under
        # EARLIER tasks, so judging only the active task's evidence wrongly
        # reports "no evidence" for completed work.
        fcs = (db.query(HermesFileChange)
               .filter(HermesFileChange.project_id == project_id)
               .order_by(HermesFileChange.created_at.desc()).limit(15).all())
        trs = (db.query(HermesTestRun)
               .filter(HermesTestRun.project_id == project_id)
               .order_by(HermesTestRun.created_at.desc()).limit(15).all())
        evidence_lines = []
        for fc in fcs:
            on_disk = os.path.exists(fc.file_path or "")
            line = f"- FILE {fc.file_path} ({fc.change_summary}) — exists on disk: {on_disk}"
            if on_disk:
                try:
                    if os.path.getsize(fc.file_path) <= 2048:
                        line += f" — ACTUAL CONTENT: {open(fc.file_path).read()[:300]!r}"
                except Exception:
                    pass
            evidence_lines.append(line)
        for tr in trs:
            evidence_lines.append(
                f"- TEST `{tr.command}` -> {tr.status}: {(tr.output_summary or '')[:150]}")
        evidence = "\n".join(evidence_lines) or "(NO recorded evidence for this task yet)"
        evidence_count = len(fcs) + len(trs)
    except Exception as ev_err:
        print(f"Evidence gathering failed: {ev_err}")
        evidence, evidence_count = "(evidence unavailable)", 0
    finally:
        db.close()

    # Construct the Evaluation Prompt. The success criteria are the CONTRACT
    # issued by the independent auditor: the evaluator may only judge evidence
    # against it — it may not reinterpret, weaken, or rewrite it.
    contract = "\n".join(f"- {c}" for c in (goal.success_criteria or [])) or "(no contract — judge the task on its own description)"
    prompt = f"""
    You are a Quality Assurance Evaluator bound to an externally issued contract.

    GOAL: {goal.title}

    CONTRACT (immutable checklist; each line is one item with how to verify it):
    {contract}

    CURRENT TASK: {active_task.title}
    EXECUTOR CLAIM (untrusted self-report): {heartbeat.progress_summary}
    BLOCKER: {heartbeat.blocker}

    INDEPENDENT TEST RESULTS (the evaluator ran these commands ITSELF just now —
    this is ground truth, not a claim):
    {json.dumps(test_results, indent=2) if test_results else "(no audit test list exists yet)"}

    RECORDED EVIDENCE (ground truth from the database and disk):
    {evidence}

    Rules — apply mechanically, do not be generous:
    1. Judge ONLY by RECORDED EVIDENCE. The executor claim is untrusted; if the
       evidence list is empty or does not support the claim, the task is NOT done
       and no contract item is satisfied.
    2. decision="complete" ONLY when the current task is finished AND every contract
       item is satisfied. List ids of unsatisfied items in missing_items (empty if none).
    3. decision="blocked" only for things requiring a human (credentials, permissions,
       impossible request).
    4. Otherwise decision="continue", with missing_items listing what remains.

    Return JSON: {{"decision": "complete"|"blocked"|"continue", "task_completed": true|false,
    "reason": "...", "missing_items": ["C1", ...]}}
    """
    
    # EFFICIENCY: when an audit test list exists, the DETERMINISTIC CONTRACT
    # GATE below fully decides the verdict and overwrites whatever the model
    # says — so the 12B evaluator call was pure waste (one full inference per
    # cycle). Synthesize the verdict from the tests we already executed.
    if tests_exist:
        _failing = [tr for tr in test_results if not tr["passed"]]
        response_raw = json.dumps({
            "decision": "continue" if _failing else "complete",
            "task_completed": not _failing,
            "reason": "deterministic verdict from independently executed tests",
            "missing_items": [tr["id"] for tr in _failing]})
        print("⚡ Evaluator verdict: deterministic (no LLM call — tests are ground truth).")
    else:
        try:
            response_raw = evaluator_llm.generate(prompt, schema=EVALUATOR_SCHEMA)
        except Exception as llm_err:
            print(f"💥 Evaluator LLM call failed ({llm_err}); judging from test results only.")
            failing = [tr for tr in test_results if not tr["passed"]]
            return {"decision": "continue",
                    "last_eval": {"reason": "Evaluator LLM unavailable this cycle; independent tests "
                                            + ("failing: " + ", ".join(tr["id"] for tr in failing)
                                               if failing else "ran without full verdict"),
                                  "missing_items": [tr["id"] for tr in failing]}}
        print(f"--- Evaluator Raw Response ---\n{response_raw}\n------------------------------")
    
    db = SessionLocal()
    try:
        clean_raw = response_raw.strip()
        if "<think>" in clean_raw:
            clean_raw = clean_raw.split("</think>")[-1].strip()
        if "</think>" in clean_raw:
            clean_raw = clean_raw.split("</think>")[-1].strip()
            
        if "```json" in clean_raw:
            json_str = clean_raw.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_raw:
            json_str = clean_raw.split("```")[1].split("```")[0].strip()
        else:
            first_brace = clean_raw.find("{")
            last_brace = clean_raw.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                json_str = clean_raw[first_brace:last_brace+1].strip()
            else:
                json_str = clean_raw
            
        data = json.loads(json_str)
        decision = data.get("decision", "continue")
        task_completed = data.get("task_completed", False)
        missing_items = [m for m in (data.get("missing_items") or []) if isinstance(m, str) and m.strip()]

        # ---- DETERMINISTIC CONTRACT GATE (overrides the LLM verdict) ----
        goal_verified = False
        if tests_exist:
            failed = [tr for tr in test_results if not tr["passed"]]
            if tests_all_pass and decision != "blocked":
                print("🏁 All audit tests PASSED — GOAL verified complete. Breaking the loop.")
                decision, task_completed, missing_items = "complete", True, []
                goal_verified = True
                data["reason"] = ("All audit tests passed on independent execution: "
                                  + ", ".join(tr["id"] for tr in test_results))
            elif failed:
                if decision == "complete" or task_completed:
                    print(f"🔬 Overruled completion: {len(failed)} audit test(s) failed.")
                decision, task_completed = "continue", False
                missing_items = [tr["id"] for tr in failed]
                data["reason"] = ("Independent test execution failed: " + "; ".join(
                    f"{tr['id']} `{tr['command']}` exit={tr['exit']} out={tr['output'][-400:]!r}"
                    for tr in failed[:4]))

        # Mechanical floor: with zero recorded evidence nothing can complete,
        # whatever the LLM judge said.
        if (task_completed or decision == "complete") and evidence_count == 0 and not (tests_exist and tests_all_pass):
            print("⚖️  Overruled: completion claimed with ZERO recorded evidence — forcing continue.")
            decision, task_completed = "continue", False
            data["reason"] = ("No recorded evidence (file change / test run) exists for this task. "
                              "Execute real work and record it before claiming completion.")

        # CONTRACT ENFORCEMENT (the upgraded Ralph loop): the goal cannot close
        # while contract items are unmet. Unmet items become new proposed tasks
        # and control returns to the planner instead of ending.
        if decision == "complete" and missing_items and (goal.success_criteria or []):
            print(f"⚖️  Contract gate: {len(missing_items)} item(s) unmet {missing_items} — goal stays open.")
            existing_titles = {t.title for t in db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
                HermesTask.status != "completed").all()}
            created = 0
            for mid in missing_items[:5]:
                item_text = next((c for c in goal.success_criteria if c.startswith(f"[{mid}]")), mid)
                title = f"Satisfy contract item {mid}"
                if title in existing_titles:
                    continue
                try:
                    MemoryService(db).create_task(
                        project_id=project_id, goal_id=goal.id, title=title,
                        description=f"Produce verifiable evidence for: {item_text}",
                        status="proposed", priority=2)
                    created += 1
                except Exception as task_err:
                    print(f"⚖️  Could not create contract task for {mid}: {task_err}")
            if created:
                db.commit()
                print(f"⚖️  Spawned {created} contract-repair task(s) — re-planning.")
            decision = "continue"  # current task may still complete below

        updated_active_task = active_task
        
        if task_completed or decision == "complete":
            try:
                # Attempt standard verified completion
                service = MemoryService(db)
                service.complete_task(project_id, active_task.id)
            except Exception as db_err:
                print(f"Could not perform verified complete_task: {db_err}. Doing fallback direct complete status update.")
                db_task = db.query(HermesTask).filter(HermesTask.id == active_task.id).first()
                if db_task:
                    db_task.status = "completed"
                    db_task.completed_at = _utcnow()
                    db.commit()
            
            # Retrieve updated active task with status="completed"
            db_task = db.query(HermesTask).filter(HermesTask.id == active_task.id).first()
            if db_task:
                updated_active_task = Task(
                    id=db_task.id,
                    title=db_task.title,
                    description=db_task.description or "",
                    status=db_task.status,
                    priority=db_task.priority,
                    next_step=db_task.description or ""
                )
        
        # Load up to date task queue from DB
        db_tasks = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == goal.id,
        ).all()
        updated_queue = []
        for t in db_tasks:
            updated_queue.append(Task(
                id=t.id,
                title=t.title,
                description=t.description or "",
                status=t.status,
                priority=t.priority,
                next_step=t.description or ""
            ))
            
        if goal_verified:
            g = active_goal_query(db, project_id)
            if g:
                g.status = "completed"
                db.commit()
        # ---- DETERMINISTIC WATCHDOG (computed in code, not by a model) ----
        # Catches the failure patterns that burn the tool budget: the executor
        # repeating the same action across turns, and cycles producing no new
        # evidence. These facts are handed to the steward so its directive
        # names the loop explicitly.
        # last_actions = THIS turn's executor actions (merged before us);
        # prev_actions = the turn before (we persist it each cycle below).
        this_actions = state.get("last_actions") or []
        prev_actions = state.get("prev_actions") or []
        repeats = dict(state.get("action_repeats") or {})
        watchdog_notes = []
        for sig in this_actions:
            if sig in prev_actions:
                repeats[sig] = repeats.get(sig, 1) + 1
            else:
                repeats.pop(sig, None)
        looping = [sig for sig, n in repeats.items() if n >= 3]
        if looping:
            watchdog_notes.append(
                f"LOOP DETECTED: identical action(s) repeated {max(repeats.values())} turns: "
                + ", ".join(looping[:3]) + " — this will exhaust the budget; a different "
                "approach is mandatory.")
        turn_now = state.get("turn_count", 0) + 1
        passing_now = len([tr for tr in test_results if tr.get("passed")])
        # No-progress hard-stop: if neither the passing-test count NOR the set
        # of passing test ids changes for N consecutive cycles, the loop is
        # only producing summaries — block for human steer instead of grinding
        # the budget (this is the anti-"stuck-summarising" guard).
        _now_pass_ids = sorted(tr["id"] for tr in test_results if tr.get("passed"))
        _stall = dict(state.get("progress_stall") or {})
        if _now_pass_ids == (state.get("last_pass_ids") or []):
            _stall_n = int(_stall.get("n", 0)) + 1
        else:
            _stall_n = 0
        _stall = {"n": _stall_n}
        _NOPROG_CAP = int(os.getenv("PGE_NOPROGRESS_CAP", "8"))
        if _stall_n >= _NOPROG_CAP:
            print(f"🛑 NO-PROGRESS HARD-STOP: {_stall_n} cycles with zero new passing tests — "
                  "blocking for human steer (the loop was only summarising).")
            return {"decision": "blocked", "progress_stall": _stall,
                    "last_pass_ids": _now_pass_ids,
                    "last_eval": {"reason": f"Halted after {_stall_n} no-progress cycles. Failing: "
                                  + ", ".join(tr["id"] for tr in test_results if not tr.get("passed"))[:300]
                                  + ". Likely an unsatisfiable contract or a goal beyond the local "
                                  "model's reach — human should scope down or fix the contract.",
                                  "missing_items": [tr["id"] for tr in test_results if not tr.get("passed")]}}
        if turn_now >= 6 and passing_now <= len(state.get("last_pass_ids") or []):
            watchdog_notes.append(
                f"NO PROGRESS: {turn_now} turns used, {_stall_n}/{_NOPROG_CAP} stall cycles.")
        if watchdog_notes:
            print("🚨 Watchdog: " + " | ".join(watchdog_notes)[:160])
            data["reason"] = (data.get("reason", "") + " || WATCHDOG: " + " ".join(watchdog_notes))[:900]

        # End-of-cycle steward: the small local model queries the DB fresh and
        # issues a steering directive for the next cycle.
        steer = ""
        try:
            from src.steward import steering as _steer
            steer = _steer(project_id, test_results, data.get("reason", ""))
            if steer:
                print(f"🧑‍✈️ Steward: {steer[:140]}")
        except Exception:
            pass
        if steer:
            data["reason"] = (data.get("reason", "") + " || STEERING: " + steer)[:900]
        dynamic_audit_context = {}
        try:
            from src.nodes.auditor_node import build_dynamic_audit_context
            dynamic_audit_context = build_dynamic_audit_context(project_id, test_results, state)
            if dynamic_audit_context.get("next_action"):
                print(f"🛡️  Dynamic audit: {dynamic_audit_context['next_action'][:160]}")
        except Exception as audit_context_error:
            print(f"🛡️  Dynamic audit context unavailable: {audit_context_error}")
        return {
            "decision": decision,
            "goal_verified": goal_verified,
            "test_fail_streaks": streaks,
            "last_pass_ids": sorted({tr["id"] for tr in test_results if tr["passed"]}),
            "action_repeats": repeats,
            "prev_actions": this_actions,
            "progress_stall": _stall,
            "dynamic_audit_context": dynamic_audit_context,
            "task_queue": updated_queue,
            "active_task": updated_active_task,
            "last_eval": {
                "reason": data.get("reason", ""),
                "missing_items": missing_items,
            },
        }
    except Exception as e:
        print(f"Error parsing evaluator response: {e}")
        return {"decision": "continue"}
    finally:
        db.close()
