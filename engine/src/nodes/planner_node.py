import os
import json
from typing import List, Dict
from src.state.schema import AgentState, Task, Goal
from hermes_tools import planner_llm, PLANNER_SCHEMA
from app.database import SessionLocal
from app.services import MemoryService
from app.models import HermesProject, HermesGoal, HermesTask
from src.runtime import active_goal_query


def _runtime_task(task: HermesTask) -> Task:
    return Task(
        id=task.id,
        title=task.title,
        description=task.description or "",
        status=task.status,
        priority=task.priority,
        next_step=task.description or "",
        parent_id=task.parent_task_id,
        acceptance_criteria=task.acceptance_criteria or [], attempts=task.attempt_count or 0,
    )

def planner_node(state: AgentState) -> Dict:
    """
    Planner Node: Analyzes the Goal and determines the next set of tasks.
    This node acts as the 'Architect'.
    """
    print("🎯 planner_node execution started")
    project_id = state.get("project_id", "2be10944-8429-4a61-ae16-5a8a65b9d7c7")
    print(f"🎯 planner_node: opening database session for project {project_id}")
    db = SessionLocal()
    print("🎯 planner_node: database session opened")
    
    # Load goal and current tasks queue from database
    try:
        print("🎯 planner_node: querying project and goal...")
        service = MemoryService(db)
        db_project = db.query(HermesProject).filter(HermesProject.id == project_id).first()
        if not db_project:
            import forge_config
            db_project = service.create_project(
                project_id=project_id,
                name="default",
                repo_path=str(forge_config.workspaces_root() / project_id),
                description="Forge project workspace"
            )
            
        db_goal = active_goal_query(db, project_id)
        if db_goal is not None and db_goal.status == "completed":
            print("🏁 planner: goal already completed — no further work.")
            return {"active_task": None}
        if not db_goal:
            db_goal = service.create_goal(
                project_id=project_id,
                title=state["goal"].title if state.get("goal") else "Persistent Agent Autonomy Architecture",
                description=state["goal"].description if state.get("goal") else "Build a robust, persistent state machine for Hermes.",
                success_criteria=state["goal"].success_criteria if state.get("goal") else ["State is saved to Postgres at every turn"]
            )
        
        # Ensure state has correct goal
        state["goal"] = Goal(
            id=db_goal.id,
            title=db_goal.title,
            description=db_goal.description or "",
            status=db_goal.status,
            success_criteria=db_goal.success_criteria or [],
            priority=db_goal.priority
        )
        
        # Query task list
        db_tasks = db.query(HermesTask).filter(
            HermesTask.project_id == project_id, HermesTask.goal_id == db_goal.id).all()
        has_active = any(t.status == "active" for t in db_tasks)
        if not has_active:
            db_proposed = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.status == "proposed"
            ).order_by(HermesTask.priority.asc(), HermesTask.created_at.asc()).first()
            if db_proposed:
                service.set_active_task(project_id, db_proposed.id)
                db_tasks = db.query(HermesTask).filter(
                    HermesTask.project_id == project_id, HermesTask.goal_id == db_goal.id).all()
                
        task_queue = []
        for t in db_tasks:
            task_queue.append(_runtime_task(t))
        state["task_queue"] = task_queue
    except Exception as e:
        print(f"Error reading tasks/goal from DB: {e}")
        db_goal = None
    finally:
        db.close()

    # Convergence guard: only invoke the planner LLM when the queue has no
    # actionable work. If a task is already active (or proposed tasks are
    # waiting), select it and proceed instead of endlessly generating new
    # tasks every cycle. The LLM "propose" path runs only when the queue is
    # exhausted (a genuine replan) — when it then proposes nothing new, the
    # graph ends cleanly.
    existing = state.get("task_queue", [])
    active_existing = next((t for t in existing if t.status == "active"), None)

    # Retire tasks that exhausted their executor attempts. Without this, the
    # evaluator router sends control back here after the attempt cap, the
    # still-active task gets re-selected verbatim, and executor<->evaluator
    # spin forever on the same work.
    attempts = {t.id: t.attempts for t in existing}
    attempt_cap = int(os.getenv("PGE_MAX_TASK_ATTEMPTS", "8"))
    retired_titles = set()
    if active_existing and attempts.get(active_existing.id, 0) >= attempt_cap:
        print(f"🛑 planner: task '{active_existing.title}' exhausted "
              f"{attempts.get(active_existing.id)} attempts — retiring it (status=blocked).")
        db_r = SessionLocal()
        try:
            row = db_r.query(HermesTask).filter(HermesTask.id == active_existing.id).first()
            if row:
                row.status = "blocked"
                db_r.commit()
        except Exception as e:
            print(f"Error retiring exhausted task: {e}")
        finally:
            db_r.close()
        retired_titles.add(active_existing.title)
        active_existing.status = "blocked"
        active_existing = None

    # Contract-repair preemption: if a "Make audit test ... pass" task is
    # waiting and the current active task has already had 2+ attempts without
    # finishing, switch to the repair task — the contract IS the goal.
    repair_proposed = [t for t in existing
                       if t.status == "proposed" and t.title.startswith("Make audit test")]
    if (active_existing and repair_proposed
            and attempts.get(active_existing.id, 0) >= 2):
        db_p = SessionLocal()
        try:
            row = db_p.query(HermesTask).filter(HermesTask.id == active_existing.id).first()
            if row and row.status == "active":
                row.status = "proposed"
                db_p.commit()
            active_existing.status = "proposed"
            print(f"⚖️  planner: preempting '{active_existing.title}' for contract repair.")
            active_existing = None
        except Exception as e:
            print(f"Preemption failed: {e}")
        finally:
            db_p.close()

    pending_proposed = sorted(
        [t for t in existing if t.status == "proposed" and t.title not in retired_titles],
        key=lambda t: t.priority
    )
    # EVOLVE the still-active task with the latest loop feedback: fold the
    # evaluator's verdict into the task description (replacing any previous
    # feedback section) so the next executor pass faces an updated brief,
    # not a frozen one.
    last_eval = state.get("last_eval") or {}
    if active_existing and last_eval.get("reason") and attempts.get(active_existing.id, 0) > 0:
        base_desc = (active_existing.description or "").split("\n[LOOP FEEDBACK]")[0].rstrip()
        fb = last_eval["reason"][:400]
        if last_eval.get("missing_items"):
            fb += f" | unmet: {last_eval['missing_items']}"
        new_desc = f"{base_desc}\n[LOOP FEEDBACK] {fb}"
        db_f = SessionLocal()
        try:
            row = db_f.query(HermesTask).filter(HermesTask.id == active_existing.id).first()
            if row:
                row.description = new_desc
                db_f.commit()
            active_existing.description = new_desc
            print("🧬 planner: folded evaluator feedback into the active task brief.")
        except Exception as e:
            print(f"Could not evolve task description: {e}")
        finally:
            db_f.close()

    # On a 'decompose' decision the executor has repeated the SAME action on the
    # active task N times (stagnation). Reusing that task verbatim would just
    # stagnate again, so skip the reuse shortcut and fall through to a fresh LLM
    # decomposition that SPLITS or replaces it. Mark the stuck task blocked so
    # the planner proposes something genuinely different.
    _stagnating = (state.get("decision") or "").lower() == "decompose"
    if _stagnating and active_existing is not None:
        try:
            _dbx = SessionLocal()
            _row = _dbx.query(HermesTask).filter(HermesTask.id == active_existing.id).first()
            if _row and (_row.attempt_count or 0) >= 3:
                _row.status = "blocked"
                _dbx.commit()
                print(f"🧱 planner: stuck task '{active_existing.title}' blocked after repeated "
                      "identical actions — decomposing into a different approach.")
            _dbx.close()
        except Exception as _e:
            print(f"planner: could not block stuck task: {_e}")

    if (active_existing or pending_proposed) and not _stagnating:
        active_task = active_existing
        if active_task is None and pending_proposed:
            nxt = pending_proposed[0]
            db2 = SessionLocal()
            try:
                row = db2.query(HermesTask).filter(HermesTask.id == nxt.id).first()
                if row:
                    MemoryService(db2).set_active_task(project_id, row.id)
                nxt.status = "active"
            except Exception as e:
                print(f"Error activating proposed task: {e}")
            finally:
                db2.close()
            active_task = nxt
        print(f"🎯 planner: using existing task "
              f"'{active_task.title if active_task else None}', skipping LLM proposal")
        return {"task_queue": existing, "active_task": active_task}

    goal_desc = state['goal'].description
    current_tasks = state['task_queue']

    # Evaluator feedback — the planner must know WHY the loop came back to it,
    # otherwise it re-proposes the same failing plan.
    last_eval = state.get("last_eval") or {}
    heartbeat = state.get("heartbeat")
    blocked_tasks = [t for t in current_tasks if t.status == "blocked"]
    feedback_lines = []
    if last_eval.get("reason"):
        feedback_lines.append(f"EVALUATOR VERDICT: {last_eval['reason']}")
    if last_eval.get("missing_items"):
        feedback_lines.append(f"UNMET CONTRACT ITEMS: {last_eval['missing_items']}")
    if heartbeat is not None and getattr(heartbeat, "progress_summary", ""):
        feedback_lines.append(f"LAST EXECUTOR REPORT: {heartbeat.progress_summary[:300]}")
    if blocked_tasks:
        feedback_lines.append("FAILED/BLOCKED TASKS (do NOT re-propose these — design a "
                              "DIFFERENT, smaller approach): "
                              + "; ".join(t.title for t in blocked_tasks))
    feedback_block = ("\n    EXECUTION FEEDBACK SO FAR:\n    "
                      + "\n    ".join(feedback_lines) + "\n") if feedback_lines else ""

    # Construct the Planner Prompt
    prompt = f"""You are a Senior Software Architect. Your goal is to plan the execution of the following objective:
    
    GOAL: {goal_desc}
    
    Current Tasks in Queue: {current_tasks}
    {feedback_block}
    Your task:
    1. Analyze the remaining work based on the goal AND the execution feedback above.
    2. Break it down into a sequence of discrete, atomic tasks. If previous tasks failed,
       propose SMALLER, STRUCTURALLY DIFFERENT tasks that route around the failure —
       never re-propose a failed task with the same approach.
    3. Each task must have a clear 'title', 'description', 'status' (set to 'proposed'), and 'priority' (1-5).
    
    You MUST output ONLY a valid JSON object in the exact format shown below.
    Do NOT write any thinking, explanation, analysis, introduction, markdown codeblocks, or trailing text. Be extremely concise. The output MUST start with '{{' and end with '}}'.
    
    Format:
    {{
      "new_tasks": [
        {{
          "title": "Task Title",
          "description": "Detailed description/next step",
          "priority": 1
        }}
      ]
    }}
    """
    
    # Call the LLM (Gemma 4 12B QAT)
    print("🎯 planner_node: calling LLM generate...")
    try:
        response_raw = planner_llm.generate(prompt, schema=PLANNER_SCHEMA)
    except Exception as llm_err:
        print(f"💥 Planner LLM call failed ({llm_err}); keeping existing queue.")
        return {"task_queue": state.get("task_queue", []),
                "active_task": state.get("active_task")}
    print(f"--- Planner Raw Response ---\n{response_raw}\n----------------------------")
    
    db = SessionLocal()
    try:
        service = MemoryService(db)
        # Extract the JSON block from the LLM response
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
        new_tasks_data = data.get("new_tasks", [])
        
        # Save new tasks to the database
        db_goal = active_goal_query(db, project_id)
        for nt in new_tasks_data:
            # Check if task already exists
            existing = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == db_goal.id,
                HermesTask.title == nt.get("title")
            ).first()
            if existing and existing.status == "blocked":
                print(f"🛑 planner: refusing to re-propose blocked task '{nt.get('title')}'")
                continue
            if not existing:
                # Run an alignment check for the proposed task against the overall goal
                goal_text = (db_goal.title + " " + (db_goal.description or "")).lower()
                task_text = (nt.get("title", "") + " " + (nt.get("description", "") or "")).lower()
                
                # Tokenize keywords
                stop_words = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "is", "this", "project", "task", "run", "do", "how", "why", "what", "with", "from"}
                goal_words = {w for w in "".join(c if c.isalnum() else " " for c in goal_text).split() if w not in stop_words and len(w) > 1}
                task_words = {w for w in "".join(c if c.isalnum() else " " for c in task_text).split() if w not in stop_words and len(w) > 1}
                
                overlap = goal_words.intersection(task_words)
                if not overlap and goal_words:
                    print(f"⚠️ Guardrail Filtered Drifted Proposed Task: {nt.get('title')}")
                    continue
                    
                service.create_task(
                    project_id=project_id,
                    goal_id=db_goal.id,
                    title=nt.get("title"),
                    description=nt.get("description"),
                    status="proposed",
                    priority=nt.get("priority", 3)
                )
                
        # Reload tasks from DB to update task queue
        db_tasks = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == state["goal"].id,
        ).all()
        updated_queue = []
        for t in db_tasks:
            updated_queue.append(_runtime_task(t))
            
        # Select active task
        active_task = None
        # First check if there's already an active task in DB
        db_active = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == db_goal.id,
            HermesTask.status == "active"
        ).first()
        
        if db_active:
            active_task = _runtime_task(db_active)
        else:
            # Find the first proposed task and activate it
            db_proposed = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == db_goal.id,
                HermesTask.status == "proposed"
            ).order_by(HermesTask.priority.asc(), HermesTask.created_at.asc()).first()
            
            if db_proposed:
                service.set_active_task(project_id, db_proposed.id)
                db.refresh(db_proposed)
                active_task = _runtime_task(db_proposed)
                # update in updated_queue status
                for i, t in enumerate(updated_queue):
                    if t.id == active_task.id:
                        updated_queue[i].status = "active"
                        
        print(f"Activated Task: {active_task.title if active_task else 'None'}")
        return {"task_queue": updated_queue, "active_task": active_task}
    except Exception as e:
        print(f"Error parsing planner response: {e}")
        # Return whatever we managed to load
        db_tasks = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == state["goal"].id,
        ).all()
        fallback_queue = [_runtime_task(t) for t in db_tasks]
        
        db_active = db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.goal_id == state["goal"].id,
            HermesTask.status == "active",
        ).first()
        if not db_active:
            db_proposed = db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == state["goal"].id,
                HermesTask.status == "proposed"
            ).order_by(HermesTask.priority.asc(), HermesTask.created_at.asc()).first()
            if db_proposed:
                db_proposed.status = "active"
                db.commit()
                db.refresh(db_proposed)
                db_active = db_proposed
                
        fallback_active = _runtime_task(db_active) if db_active else None
        
        return {"task_queue": fallback_queue, "active_task": fallback_active}
    finally:
        db.close()
