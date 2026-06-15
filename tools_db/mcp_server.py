import sys
import os
import forge_config
import json
from typing import List, Optional, Dict, Any
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# Ensure we can import app modules by adding the parent folder to the sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.services import MemoryService

# Load env variables from parent folder's .env if present
parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(parent_env)

mcp = FastMCP("Hermes Memory")

@mcp.tool()
def create_project(name: str, repo_path: str, description: str = "") -> str:
    """Create a new project in the memory system.
    
    Args:
        name: Name of the project (e.g. 'Hermes Core')
        repo_path: Local absolute path to the repository
        description: Optional description of the project
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        project = service.create_project(name, repo_path, description)
        return json.dumps({
            "status": "success",
            "message": f"Project created successfully: {project.name}",
            "project_id": str(project.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def create_goal(project_id: str, title: str, description: str = "", 
                success_criteria_json: str = "[]", priority: int = 3) -> str:
    """Create a multi-horizon goal for a project.
    
    Args:
        project_id: The UUID of the project
        title: Title of the goal
        description: Optional details about the goal
        success_criteria_json: JSON string of success criteria array (e.g. '["tests pass", "UI matches"]')
        priority: Priority of the goal (1 = highest, 5 = lowest)
    """
    db = SessionLocal()
    try:
        criteria = json.loads(success_criteria_json)
        service = MemoryService(db)
        goal = service.create_goal(project_id, title, description, criteria, priority)
        return json.dumps({
            "status": "success",
            "message": f"Goal created successfully: {goal.title}",
            "goal_id": str(goal.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def create_task(project_id: str, goal_id: str, title: str, 
                description: str = "", status: str = "proposed",
                priority: int = 3, acceptance_criteria_json: str = "[]",
                verification_required: bool = True) -> str:
    """Create a task associated with a goal.
    
    Args:
        project_id: The UUID of the project
        goal_id: The UUID of the goal
        title: Title of the task
        description: Optional details about the task
        status: Initial status ('proposed', 'active', 'completed')
        priority: Priority of the task (1 = highest, 5 = lowest)
        acceptance_criteria_json: JSON string of acceptance criteria array (e.g. '["file edited", "verify tests"]')
        verification_required: If True, task requires successful test run or file change to complete
    """
    db = SessionLocal()
    try:
        criteria = json.loads(acceptance_criteria_json)
        service = MemoryService(db)
        task = service.create_task(project_id, goal_id, title, description, status, priority, criteria, verification_required)
        return json.dumps({
            "status": "success",
            "message": f"Task created successfully: {task.title}",
            "task_id": str(task.id),
            "status": task.status
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def set_active_task(project_id: str, task_id: str) -> str:
    """Set a specific task as the active task.
    
    Args:
        project_id: The UUID of the project
        task_id: The UUID of the task to activate
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        task = service.set_active_task(project_id, task_id)
        if task:
            return json.dumps({
                "status": "success",
                "message": f"Task activated: {task.title}",
                "task_id": str(task.id),
                "status": task.status
            }, indent=2)
        else:
            return json.dumps({"status": "error", "message": "Task not found"}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def complete_task(project_id: str, task_id: str) -> str:
    """Complete a task, performing verification checks if required.
    
    Args:
        project_id: The UUID of the project
        task_id: The UUID of the task to complete
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        task = service.complete_task(project_id, task_id)
        return json.dumps({
            "status": "success",
            "message": f"Task completed: {task.title}",
            "task_id": str(task.id),
            "status": task.status
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def record_event(project_id: str, task_id: Optional[str] = None, 
                 event_type: str = "", actor: str = "hermes", 
                 content: str = "", metadata_json: str = "{}") -> str:
    """Record a process event or action log.
    
    Args:
        project_id: The UUID of the project
        task_id: Optional UUID of the active task
        event_type: Type of event (e.g. 'checkpoint', 'test_run', 'file_modified')
        actor: Entity performing the action (e.g. 'hermes', 'user', 'system')
        content: Description/content of the event
        metadata_json: JSON string of metadata key-values
    """
    db = SessionLocal()
    try:
        meta = json.loads(metadata_json)
        service = MemoryService(db)
        event = service.record_event(project_id, task_id, event_type, actor, content, meta)
        return json.dumps({
            "status": "success",
            "message": "Event recorded",
            "event_id": str(event.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def record_decision(project_id: str, task_id: Optional[str] = None, 
                    content: str = "", context: str = "") -> str:
    """Record an architecture or code implementation decision.
    
    Args:
        project_id: The UUID of the project
        task_id: Optional UUID of the active task
        content: The decision content
        context: Context or reasoning behind the decision
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        decision = service.record_decision(project_id, task_id, content, context)
        return json.dumps({
            "status": "success",
            "message": "Decision recorded",
            "decision_id": str(decision.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def record_constraint(project_id: str, content: str) -> str:
    """Record a system constraint or non-negotiable rule.
    
    Args:
        project_id: The UUID of the project
        content: The constraint rule (e.g. 'Keep context within 8000 tokens')
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        constraint = service.record_constraint(project_id, content)
        return json.dumps({
            "status": "success",
            "message": "Constraint recorded",
            "constraint_id": str(constraint.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def record_file_change(project_id: str, task_id: Optional[str] = None, 
                       file_path: str = "", change_summary: str = "", 
                       reason: str = "") -> str:
    """Record that a file was modified. Used as verification evidence.
    
    Args:
        project_id: The UUID of the project
        task_id: Optional UUID of the active task
        file_path: Absolute or repo-relative path of the file
        change_summary: Brief description of changes made
        reason: Purpose of change
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        change = service.record_file_change(project_id, task_id, file_path, change_summary, reason)
        return json.dumps({
            "status": "success",
            "message": "File change recorded",
            "change_id": str(change.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def record_test_run(project_id: str, task_id: Optional[str] = None, 
                    command: str = "", status: str = "", 
                    output_summary: str = "", failure_summary: str = "") -> str:
    """Record details of a test suite execution. Used as verification evidence.
    
    Args:
        project_id: The UUID of the project
        task_id: Optional UUID of the active task
        command: Test run command (e.g. 'npm test')
        status: Outcome ('success', 'failure')
        output_summary: Summary of stdout/test results
        failure_summary: Error log or failed assertions if status is failure
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        run = service.record_test_run(project_id, task_id, command, status, output_summary, failure_summary)
        return json.dumps({
            "status": "success",
            "message": f"Test run recorded with status: {run.status}",
            "run_id": str(run.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def create_checkpoint(project_id: str, goal_id: Optional[str] = None, 
                      task_id: Optional[str] = None, summary: str = "", 
                      current_state_json: str = "{}", 
                      next_actions_json: str = "[]", 
                      open_risks_json: str = "[]") -> str:
    """Save a checkpoints record to save progress context.
    
    Args:
        project_id: The UUID of the project
        goal_id: Optional UUID of the goal
        task_id: Optional UUID of the task
        summary: State description
        current_state_json: JSON string representing current state dictionaries
        next_actions_json: JSON string of next actions array
        open_risks_json: JSON string of open risks array
    """
    db = SessionLocal()
    try:
        state = json.loads(current_state_json)
        actions = json.loads(next_actions_json)
        risks = json.loads(open_risks_json)
        service = MemoryService(db)
        checkpoint = service.create_checkpoint(project_id, goal_id, task_id, summary, state, actions, risks)
        return json.dumps({
            "status": "success",
            "message": "Checkpoint created successfully",
            "checkpoint_id": str(checkpoint.id)
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def build_context_pack(project_id: str) -> str:
    """Compile active state, goals, tasks, constraints and decisions into a pack.
    
    Args:
        project_id: The UUID of the project to build the context pack for
    """
    import os
    db = SessionLocal()
    try:
        service = MemoryService(db)
        state_db_path = os.getenv("HERMES_STATE_DB_PATH", forge_config.state_db_path())
        pack = service.build_context_pack(project_id, state_db_path=state_db_path)
        return json.dumps(pack, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def classify_task_alignment(project_id: str, user_request: str) -> str:
    """Determine if a request aligns with the active task or is a distraction.
    
    Args:
        project_id: The UUID of the project
        user_request: The incoming request or text
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        alignment = service.classify_task_alignment(project_id, user_request)
        return json.dumps({
            "status": "success",
            "alignment": alignment
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def search_memory(project_id: str, query: str, 
                  memory_type: Optional[str] = None, limit: int = 10) -> str:
    """Full-text search through stored decisions, constraints, events, and notes.
    
    Args:
        project_id: The UUID of the project
        query: Search keywords
        memory_type: Optional filter (e.g. 'decision', 'constraint')
        limit: Max search results to return
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        items = service.search_memory(project_id, query, memory_type, limit)
        results = []
        for item in items:
            results.append({
                "id": str(item.id),
                "memory_type": item.memory_type,
                "content": item.content,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "tags": item.tags
            })
        return json.dumps({
            "status": "success",
            "results": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def consolidate_old_logs(project_id: str, days_threshold: int = 7) -> str:
    """Consolidate events and checkpoints of completed tasks older than N days.
    
    Args:
        project_id: The UUID of the project
        days_threshold: Completed tasks older than this many days will be consolidated (default: 7)
    """
    db = SessionLocal()
    try:
        service = MemoryService(db)
        res = service.consolidate_old_logs(project_id, days_threshold)
        return json.dumps(res, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

@mcp.tool()
def run_autonomy_loop(project_id: str) -> str:
    """Execute the autonomous PGE (Plan-Generate-Evaluate) loop for a project.
    This runs the LangGraph autonomy engine until the active goal is completed or blocked.
    
    Args:
        project_id: The UUID of the project to run the loop for
    """
    import sys
    import os
    import datetime
    
    # Ensure the agent-autonomy-project directory is on sys.path
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_dir = os.path.join(root_dir, "agent-autonomy-project")
    if project_dir not in sys.path:
        sys.path.append(project_dir)
        
    from src.graph import app
    from src.state.schema import Goal
    from app.models import HermesGoal
    
    db = SessionLocal()
    try:
        service = MemoryService(db)
        db_goal = db.query(HermesGoal).filter(HermesGoal.project_id == project_id).first()
        if not db_goal:
            return json.dumps({
                "status": "error",
                "message": f"No goal found for project {project_id}. Please create a goal first."
            }, indent=2)
            
        initial_goal = Goal(
            id=db_goal.id,
            title=db_goal.title,
            description=db_goal.description or "",
            status=db_goal.status,
            success_criteria=db_goal.success_criteria or [],
            priority=db_goal.priority
        )
        
        initial_state = {
            "project_id": project_id,
            "goal": initial_goal,
            "task_queue": [],
            "active_task": None,
            "heartbeat": None,
            "history": [],
            "decision": "continue",
            "task_attempts": {},
            "turn_count": 0,
            "current_run_id": f"run_{project_id[:8]}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}",
            "timestamp": datetime.datetime.now()
        }

        print(f"Starting PGE Autonomy Loop for project {project_id}...")
        from src.graph import MAX_TURNS
        final_state = app.invoke(initial_state, config={"recursion_limit": MAX_TURNS * 3 + 10})
        
        # Clean up database connections inside the loop runner
        try:
            from app.database import engine
            engine.dispose()
        except Exception:
            pass
            
        return json.dumps({
            "status": "success",
            "message": "Autonomy loop executed successfully.",
            "decision": final_state.get("decision"),
            "turn_count": final_state.get("turn_count"),
            "heartbeat": {
                "progress_summary": final_state["heartbeat"].progress_summary,
                "next_task_description": final_state["heartbeat"].next_task_description,
                "blocker": final_state["heartbeat"].blocker,
                "resume_instruction": final_state["heartbeat"].resume_instruction
            } if final_state.get("heartbeat") else None
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        db.close()

import sqlite3

_TOOL_DOCS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_docs.db")


@mcp.tool()
def list_tool_docs() -> str:
    """List every tool/skill/framework the agent has access to (name, category,
    location, when-to-use). Use this to discover capabilities; then call
    get_tool_doc(name) for the full usage of a specific one."""
    try:
        con = sqlite3.connect(_TOOL_DOCS_DB)
        rows = con.execute(
            "SELECT name, category, location, when_to_use FROM tools ORDER BY category, name"
        ).fetchall()
        con.close()
        return json.dumps(
            {"count": len(rows),
             "tools": [{"name": r[0], "category": r[1], "location": r[2], "when_to_use": r[3]} for r in rows]},
            indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


@mcp.tool()
def get_tool_doc(name: str) -> str:
    """Return the FULL documentation for a single tool/skill/framework by name.
    Call this instead of guessing a tool's interface. Names come from
    list_tool_docs (matching is case-insensitive and substring-friendly)."""
    try:
        con = sqlite3.connect(_TOOL_DOCS_DB)
        row = con.execute(
            "SELECT name, category, location, when_to_use, full_doc, updated_at "
            "FROM tools WHERE lower(name)=lower(?)", (name,)
        ).fetchone()
        if not row:
            row = con.execute(
                "SELECT name, category, location, when_to_use, full_doc, updated_at "
                "FROM tools WHERE lower(name) LIKE lower(?) LIMIT 1", (f"%{name}%",)
            ).fetchone()
        con.close()
        if not row:
            return json.dumps({"status": "not_found", "name": name,
                               "hint": "call list_tool_docs to see valid names"}, indent=2)
        return json.dumps(
            {"name": row[0], "category": row[1], "location": row[2],
             "when_to_use": row[3], "full_doc": row[4], "updated_at": row[5]}, indent=2,
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


if __name__ == "__main__":
    mcp.run()
