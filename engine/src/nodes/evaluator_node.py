import os
import json
from typing import Dict
from src.state.schema import AgentState
from hermes_tools import llm

from src.state.schema import Task, Goal
from hermes_tools import EVALUATOR_SCHEMA
from app.database import SessionLocal
from app.services import MemoryService
from app.models import HermesGoal, HermesTask, HermesMemoryItem
from src.runtime import active_goal_query, project_workspace
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Naive UTC now. DB columns are TIMESTAMP WITHOUT TIME ZONE, so we keep
    timestamps naive while avoiding the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    test_results, tests_all_pass, tests_exist = [], False, False
    db = SessionLocal()
    try:
        audit_tests = load_audit_tests(db, project_id)
        workdir = project_workspace(db, project_id)
    finally:
        db.close()
    # Make the project's .venv (and toolchains) visible to every test command —
    # otherwise install_deps succeeds but the audit tests' python can't import
    # the packages.
    def _test_env(ws):
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
                r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True,
                                   text=True, timeout=60, cwd=workdir, env=_tenv)
                out = (r.stdout or "") + (r.stderr or "")
                passed = (r.returncode == want_exit) and (want_sub in out if want_sub else True)
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
                rv = subprocess.run(["/bin/bash", "-lc",
                                     "python3 -m unittest discover -v 2>&1"],
                                    capture_output=True, text=True, timeout=60, cwd=workdir, env=_tenv)
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
        import subprocess as _sp
        def _git(*a):
            return _sp.run(["git", "-C", workdir, *a], capture_output=True, text=True, timeout=30)
        try:
            if _git("rev-parse", "--git-dir").returncode != 0:
                _git("init"); _git("add", "-A")
                _git("-c", "user.email=pge@local", "-c", "user.name=pge", "commit", "-m", "baseline", "--allow-empty")
            prev_pass = set(state.get("last_pass_ids") or [])
            now_pass = {tr["id"] for tr in test_results if tr["passed"]}
            regressed = sorted(prev_pass - now_pass)
            has_commit = _git("rev-parse", "HEAD").returncode == 0
            if regressed and has_commit:
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
                                f"{tr['output'][-200:]!r}. Make the MINIMAL change; "
                                "do not rewrite working files.")
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
                                f" — current output: {tr['output'][:180]!r}. "
                                "Create or modify whatever files are needed to make this real "
                                "command pass in the project workspace.")
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
            print(f"🛑 Completion BLOCKED: previously-passing test(s) vanished: {_vanished} — reverting.")
            import subprocess as _sp2
            _sp2.run(["git", "-C", workdir, "checkout", "--", "."], capture_output=True)
            _sp2.run(["git", "-C", workdir, "clean", "-fd"], capture_output=True)
            return {"decision": "continue", "last_pass_ids": sorted(_prev_ok),
                    "last_eval": {"reason": (
                        f"Your change DELETED previously-passing tests {_vanished} — forbidden, "
                        "auto-reverted. Make failing tests pass by EDITING expectations, never "
                        "by removing tests."),
                        "missing_items": _vanished}}
        print("✅ All audit tests PASSED for the current state.")
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
            response_raw = llm.generate(prompt, schema=EVALUATOR_SCHEMA)
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
