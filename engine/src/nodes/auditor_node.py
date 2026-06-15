"""Auditor node — issues the dual contract AFTER the planner has planned.

Flow: planner generates the plan (task queue) → auditor reads the goal, the
ORIGINAL user request, and the planner's plan → derives:
  - checklist -> ``HermesGoal.success_criteria`` (executor contract)
  - tests     -> HermesMemoryItem(memory_type='audit_tests')
                 (evaluator contract — the evaluator RUNS these itself)

The contract validates the USER REQUEST (the plan only informs it); once
issued it is immutable for the goal's life (PGE_FORCE_AUDIT=1 regenerates).
"""
import os
import json
import sqlite3
from pathlib import Path
from typing import Dict
from src.state.schema import AgentState, Goal
from src.auditor import generate_contract, checklist_to_criteria
from app.database import SessionLocal
from app.services import MemoryService
from app.models import (HermesFileChange, HermesGoal, HermesMemoryItem,
                        HermesTask, HermesTestRun)
from src.runtime import active_goal_query, project_workspace

_AUDIT_MARK = "|| VERIFY:"
_WEB_DOCS_DB = Path(__file__).resolve().parents[3] / "tools_db" / "tool_docs.db"


def _matching_cached_docs(query: str, limit: int = 3) -> list[dict]:
    """Return bounded cached documentation snippets relevant to this failure."""
    words = [w.lower() for w in query.split() if len(w) >= 5][:6]
    if not words or not _WEB_DOCS_DB.exists():
        return []
    docs = []
    try:
        con = sqlite3.connect(str(_WEB_DOCS_DB), timeout=2)
        for word in words:
            row = con.execute(
                "SELECT url, substr(content,1,500) FROM web_docs "
                "WHERE lower(content) LIKE ? ORDER BY fetched_at DESC LIMIT 1",
                (f"%{word}%",),
            ).fetchone()
            if row and row[0] not in {d["url"] for d in docs}:
                docs.append({"url": row[0], "snippet": row[1]})
            if len(docs) >= limit:
                break
        con.close()
    except (OSError, sqlite3.Error):
        return []
    return docs


def build_dynamic_audit_context(project_id: str, test_results: list | None = None,
                                state: dict | None = None) -> dict:
    """Build a fresh bounded repair pack from durable facts, never chat history.

    The immutable contract defines done. This pack changes every cycle and
    explains the smallest current failure, evidence already collected, repeated
    actions to avoid, and which capability should be used next.
    """
    state = state or {}
    db = SessionLocal()
    try:
        goal = active_goal_query(db, project_id)
        task = (db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == goal.id if goal else False,
            HermesTask.status == "active",
        ).order_by(HermesTask.updated_at.desc()).first()) if goal else None
        workspace = project_workspace(db, project_id)
        files = []
        tests = []
        memories = []
        if task:
            files = (db.query(HermesFileChange).filter(
                HermesFileChange.task_id == task.id)
                .order_by(HermesFileChange.created_at.desc()).limit(5).all())
            tests = (db.query(HermesTestRun).filter(
                HermesTestRun.task_id == task.id)
                .order_by(HermesTestRun.created_at.desc()).limit(5).all())
            memories = (db.query(HermesMemoryItem).filter(
                HermesMemoryItem.task_id == task.id,
                HermesMemoryItem.memory_type.in_(("mistake", "learning_distill", "blocker")))
                .order_by(HermesMemoryItem.created_at.desc()).limit(4).all())

        failures = [r for r in (test_results or []) if not r.get("passed")][:4]
        failure_text = " ".join(
            f"{r.get('id')} {r.get('command')} {r.get('output', '')[:300]}" for r in failures)
        query = " ".join(filter(None, [
            goal.title if goal else "", task.title if task else "", failure_text,
        ]))
        cached_docs = _matching_cached_docs(query)
        repeated = [sig for sig, count in (state.get("action_repeats") or {}).items()
                    if count >= 2][:5]

        next_action = "Inspect the active task and perform one concrete implementation action."
        if failures:
            first = failures[0]
            out = (first.get("output") or "").strip()
            # A failing check is one of two kinds, and the right action differs:
            #  (a) MISSING ARTIFACT — the check wants an output/file that does
            #      not exist yet (empty output, "No such file", nothing built).
            #      Re-running the command is useless; the executor must WRITE or
            #      EDIT the source that PRODUCES it. (The brooklyn stall: it ran
            #      the T5 render command 4x instead of writing the raytracer.)
            #  (b) REAL ERROR — there is actual error output to inspect and fix.
            missing_artifact = (not out) or any(s in out.lower() for s in (
                "no such file", "not found", "cannot find", "command not found"))
            if missing_artifact:
                next_action = (
                    f"Acceptance check {first.get('id')} is FAILING because the artifact it verifies "
                    f"does not exist yet. Do NOT just run `{first.get('command')}` — that is only the "
                    "verification. WRITE or EDIT the project's source code so that command will pass "
                    "(use write_file/edit_file to implement the real feature, then bash to build). "
                    "Implement the smallest code change that produces the required artifact."
                )
            else:
                next_action = (
                    f"Acceptance check {first.get('id')} fails with real output. Inspect it, then EDIT "
                    f"the source to fix the smallest root cause so `{first.get('command')}` passes — "
                    "do not change passing behavior. Running the command alone is not progress."
                )
        if repeated:
            next_action += " Do not repeat the listed action signatures; change the approach."

        workspace_files = []
        try:
            root = Path(workspace)
            workspace_files = [str(p.relative_to(root)) for p in root.rglob("*")
                               if p.is_file() and not any(x in p.parts for x in (
                                   ".git", "node_modules", ".venv", "__pycache__"))][:40]
        except OSError:
            pass

        return {
            "goal": {"title": goal.title, "criteria": goal.success_criteria or []} if goal else None,
            "active_task": {
                "id": task.id, "title": task.title, "description": task.description or "",
                "attempts": task.attempt_count, "no_progress": task.no_progress_count,
            } if task else None,
            "failing_checks": failures,
            "recent_task_files": [{"path": f.file_path, "summary": f.change_summary} for f in files],
            "recent_task_commands": [{"command": t.command, "status": t.status,
                                      "output": (t.output_summary or "")[:300]} for t in tests],
            "verified_debug_lessons": [m.content[:500] for m in memories],
            "repeated_actions_to_avoid": repeated,
            "workspace_files": workspace_files,
            "cached_official_docs": cached_docs,
            "next_action": next_action,
            "capability_guidance": [
                "Use notebook_cell for a bounded reproducer or hypothesis check; it is not evidence.",
                "Use fetch_doc with an official documentation URL when API behavior is uncertain.",
                "Use run_command for the narrow failing check, then the full verification suite.",
                "Do not use internet text or model claims as proof of completion.",
            ],
        }
    finally:
        db.close()


def load_audit_tests(db, project_id: str) -> list:
    """Latest persisted evaluator test list for a project ([] if none)."""
    goal = active_goal_query(db, project_id)
    if not goal:
        return []
    row = (db.query(HermesMemoryItem)
           .filter(HermesMemoryItem.project_id == project_id,
                   HermesMemoryItem.memory_type == "audit_tests",
                   HermesMemoryItem.status == "active",
                   HermesMemoryItem.tags.any(f"goal:{goal.id}"))
           .order_by(HermesMemoryItem.created_at.desc()).first())
    if not row:
        return []
    try:
        return json.loads(row.content)
    except Exception:
        return []


def contract_in_force(db, project_id: str) -> bool:
    g = active_goal_query(db, project_id)
    if not g:
        return False
    has_criteria = any(_AUDIT_MARK in (c or "") for c in (g.success_criteria or []))
    return has_criteria and bool(load_audit_tests(db, project_id))


def _contract_poisoned(db, project_id: str, goal) -> bool:
    """A persisted contract is POISONED if any of its tests fail the quality
    gate for the goal's detected stack — e.g. a `python3 -c "import raytracing"`
    test left over for a C++ goal. Immutability must never protect a provably
    unsatisfiable contract, or the loop deadlocks forever (the brooklyn 2-day
    hang). When poisoned we retire the bad tests so a fresh (template) contract
    regenerates."""
    tests = load_audit_tests(db, project_id)
    if not tests:
        return False
    try:
        from src.auditor import validate_tests, detect_stack
        stack = detect_stack(f"{goal.title} {goal.description or ''}")
        kept, dropped = validate_tests(tests, stack)
        return bool(dropped)
    except Exception as e:
        print(f"🛡️  contract health-check failed ({str(e)[:60]}) — leaving as-is.")
        return False


def _retire_audit_tests(db, project_id: str, goal) -> None:
    """Mark this goal's persisted audit_tests obsolete so load_audit_tests
    (status=='active' only) ignores them and the auditor regenerates."""
    rows = (db.query(HermesMemoryItem)
            .filter(HermesMemoryItem.project_id == project_id,
                    HermesMemoryItem.memory_type == "audit_tests",
                    HermesMemoryItem.status == "active",
                    HermesMemoryItem.tags.any(f"goal:{goal.id}")).all())
    for r in rows:
        r.status = "obsolete"
    # Drop the contract criteria too so has_criteria flips false and the fresh
    # checklist fully replaces the old one.
    goal.success_criteria = [c for c in (goal.success_criteria or [])
                             if _AUDIT_MARK not in (c or "")]
    db.commit()
    print(f"🛡️  Retired {len(rows)} poisoned audit_test record(s) for goal "
          f"{goal.id} — regenerating a clean contract.")


def auditor_node(state: AgentState) -> Dict:
    project_id = state.get("project_id")
    db = SessionLocal()
    try:
        db_goal = active_goal_query(db, project_id)
        if not db_goal:
            print("🛡️  Auditor: no goal yet — nothing to audit.")
            return {}

        if contract_in_force(db, project_id) and not os.getenv("PGE_FORCE_AUDIT"):
            if _contract_poisoned(db, project_id, db_goal):
                # Provably-garbage immutable contract — retire it and fall
                # through to regenerate a clean (template) contract.
                _retire_audit_tests(db, project_id, db_goal)
            else:
                print("🛡️  Auditor: dual contract already in force — immutable, skipping.")
                return {}

        # The persisted goal is the source of truth. Telegram/session history
        # is intentionally excluded so stale conversation cannot alter a run.
        user_prompt = db_goal.description or db_goal.title

        # The planner's plan = the current task queue.
        plan_lines = [f"- [{t.status}] {t.title}: {t.description}"
                      for t in (state.get("task_queue") or [])[:15]]
        plan_text = "\n".join(plan_lines) or "(planner produced no tasks yet)"

        # LEARN FROM MISTAKES: feed the auditor the top recorded lessons so it
        # improves its contract each run (user request 2026-06-13).
        lessons_text = ""
        try:
            from app.models import HermesMemoryItem
            rows = (db.query(HermesMemoryItem)
                    .filter(HermesMemoryItem.project_id == project_id,
                            HermesMemoryItem.memory_type == "lesson")
                    .order_by(HermesMemoryItem.importance.desc(),
                              HermesMemoryItem.created_at.desc()).limit(6).all())
            # also pull a few GLOBAL lessons (any project) about contract/hallucination
            glob = (db.query(HermesMemoryItem)
                    .filter(HermesMemoryItem.memory_type == "lesson",
                            HermesMemoryItem.content.ilike("%contract%"))
                    .order_by(HermesMemoryItem.importance.desc()).limit(4).all())
            seen, picked = set(), []
            for r in rows + glob:
                if r.content[:80] not in seen:
                    seen.add(r.content[:80]); picked.append("- " + r.content[:300])
            lessons_text = "\n".join(picked[:8])
        except Exception as le:
            print(f"auditor lesson fetch failed: {le}")

        # PROACTIVE RESEARCH: fetch + cache docs for the goal's technologies so
        # the contract (and future runs) are grounded in real APIs, not guesses.
        research_docs = ""
        try:
            from src.auditor import research_and_cache
            research_docs = research_and_cache(
                f"{db_goal.title} {db_goal.description or ''}")
        except Exception as re_:
            print(f"auditor research failed: {re_}")
        if research_docs:
            lessons_text = (lessons_text + "\n\nCACHED DOCUMENTATION (use real APIs from these, "
                            "do not invent):\n" + research_docs)[:4000]

        contract = generate_contract(
            title=db_goal.title,
            description=db_goal.description or "",
            user_prompt=user_prompt,
            plan=plan_text,
            lessons=lessons_text,
        )
        if not contract["checklist"]:
            print("🛡️  Auditor: could not generate a contract — loop proceeds with existing criteria.")
            return {}

        svc = MemoryService(db)
        criteria = checklist_to_criteria(contract["checklist"])
        db_goal.success_criteria = criteria
        # Mechanically reject tests that can never pass on this machine —
        # an unsatisfiable immutable contract deadlocks the entire loop.
        from src.auditor import validate_tests, detect_stack
        _stack = detect_stack(f"{db_goal.title} {db_goal.description or ''}")
        kept, dropped = validate_tests(contract["tests"], _stack)
        for d in dropped:
            print(f"🛡️  REJECTED test {d.get('id')}: {d.get('rejected')} — `{d.get('command')}`")
        contract["tests"] = kept
        if contract["tests"]:
            svc.record_memory_item(
                project_id=project_id, memory_type="audit_tests",
                content=json.dumps(contract["tests"]), importance=5,
                tags=["contract", f"goal:{db_goal.id}"])
        db.commit()
        print(f"🛡️  Dual contract issued by {contract['auditor_model']} from the planner's plan: "
              f"{len(criteria)} criteria, {len(contract['tests'])} evaluator tests. Immutable.")

        goal = Goal(id=db_goal.id, title=db_goal.title,
                    description=db_goal.description or "",
                    status=db_goal.status, success_criteria=criteria,
                    priority=db_goal.priority)
        return {"goal": goal}
    except Exception as e:
        print(f"🛡️  Auditor node error (loop continues without new contract): {e}")
        return {}
    finally:
        db.close()
