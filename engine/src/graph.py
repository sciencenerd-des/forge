import os
import json
import hashlib
from typing import Dict
from langgraph.graph import StateGraph, END
from src.state.schema import AgentState
from src.nodes.planner_node import planner_node
from src.nodes.executor_node import executor_node
from src.nodes.evaluator_node import evaluator_node
from src.nodes.auditor_node import auditor_node
from hermes_tools import mcp_hermes_memory_create_checkpoint

# ---------------------------------------------------------------------------
# Loop-control limits (env-overridable). These are what stop the runaway
# executor<->evaluator ping-pong that produced 5000+ no-progress checkpoints.
# ---------------------------------------------------------------------------
MAX_TURNS = int(os.getenv("PGE_MAX_TURNS", "24"))
MAX_ATTEMPTS_PER_TASK = int(os.getenv("PGE_MAX_TASK_ATTEMPTS", "8"))
MAX_NO_PROGRESS_ATTEMPTS = int(os.getenv("PGE_MAX_NO_PROGRESS_ATTEMPTS", "3"))
STAGNATION_TOLERANCE = int(os.getenv("PGE_STAGNATION_TOLERANCE", "3"))


def _checkpoint(state: AgentState, updated: Dict, label: str):
    """Persist ONE checkpoint per cycle (after the evaluator), not after every
    node — the old per-node checkpointing wrote ~3 rows/turn and ballooned the
    table to 10k+ rows."""
    merged = dict(state)
    if isinstance(updated, dict):
        merged.update(updated)
    project_id = state.get("project_id")
    if not project_id:
        return  # no project context — nothing to checkpoint against
    try:
        mcp_hermes_memory_create_checkpoint(
            project_id=project_id,
            summary=label,
            current_state_json=json.dumps(merged, default=str),
            next_actions_json=json.dumps([{"decision": merged.get("decision", "continue")}]),
        )
    except Exception as e:
        print(f"Checkpointing failed: {e}")


def evaluator_with_control(state: AgentState) -> Dict:
    """Run the evaluator, then advance the loop-control counters and persist a
    checkpoint. Returns merged state updates."""
    result = evaluator_node(state) or {}

    turn = state.get("turn_count", 0) + 1
    decision = (result.get("decision") or state.get("decision") or "continue").lower()
    active = result.get("active_task", state.get("active_task"))

    # Track per-task executor attempts so a task that never "completes" cannot
    # trap the loop forever.
    attempts = dict(state.get("task_attempts") or {})
    if active is not None and "complete" not in decision and getattr(active, "status", None) != "completed":
        from app.database import SessionLocal
        from app.services import MemoryService
        db = SessionLocal()
        try:
            durable = MemoryService(db).record_task_attempt(
                state.get("project_id"), active.id)
            attempts[active.id] = durable.attempt_count
            if (durable.attempt_count >= MAX_ATTEMPTS_PER_TASK or
                    durable.no_progress_count >= MAX_NO_PROGRESS_ATTEMPTS):
                durable.status = "blocked"
                db.commit()
                result["decision"] = "blocked"
                result["steering_reason"] = (
                    f"durable task limit reached: attempts={durable.attempt_count}, "
                    f"no_progress={durable.no_progress_count}")
        finally:
            db.close()

    result["turn_count"] = turn
    result["task_attempts"] = attempts
    result.setdefault("decision", decision)
    heartbeat = result.get("heartbeat") or state.get("heartbeat")
    progress = getattr(heartbeat, "progress_summary", "") if heartbeat else ""
    signature = hashlib.sha256(
        f"{getattr(active, 'id', '')}\0{decision}\0{progress[:500]}".encode()
    ).hexdigest()
    signatures = list(state.get("recent_action_signatures") or [])
    signatures.append(signature)
    result["recent_action_signatures"] = signatures[-STAGNATION_TOLERANCE:]
    if ("blocked" not in (result.get("decision") or "").lower()
            and len(signatures) >= STAGNATION_TOLERANCE
            and len(set(signatures[-STAGNATION_TOLERANCE:])) == 1):
        result["decision"] = "decompose"
        result["steering_reason"] = (
            f"same action signature repeated {STAGNATION_TOLERANCE} times; "
            "planner must split or replace the active task"
        )
        # Reset the detector after firing — otherwise the stale identical
        # signatures persist into the planner pass and re-trigger stagnation,
        # which used to route planner->planner (a non-existent edge -> KeyError
        # crash, observed mid-brooklyn). The planner re-decomposes on this
        # 'decompose' decision and the detector starts fresh.
        result["recent_action_signatures"] = []

    _checkpoint(state, result, "Finished evaluator_node")
    decision = (result.get("decision") or decision).lower()
    print(f"🧭 turn={turn}/{MAX_TURNS} decision={decision} "
          f"active={getattr(active, 'title', None)} "
          f"attempts={attempts.get(getattr(active, 'id', None), 0)}")
    return result


def planner_router(state: AgentState) -> str:
    """After planning: contract missing -> auditor; nothing to work on -> END;
    otherwise -> executor."""
    if state.get("turn_count", 0) >= MAX_TURNS:
        return "end"
    # NOTE: stagnation is handled by the evaluator (sets decision='decompose'
    # and routes here via the planner) — NOT by re-routing planner->planner.
    # That edge does not exist in the graph and returning "planner" here raised
    # KeyError: 'planner' and killed the run. The planner re-decomposes the
    # stuck task; we then proceed normally to the executor/auditor.
    active = state.get("active_task")
    if not active:
        print("✅ No actionable task remains — goal satisfied or queue empty. Ending.")
        return "end"
    # First pass: the planner has planned but no audit contract exists yet —
    # send the plan to the auditor so it can derive the checklist + test list.
    try:
        from src.nodes.auditor_node import (contract_in_force, _contract_poisoned,
                                            active_goal_query)
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            has_contract = contract_in_force(db, state.get("project_id"))
            # A present-but-poisoned contract (e.g. a python `import raytracing`
            # test on a C++ goal) must ALSO route to the auditor, or the loop
            # chases an impossible target forever (the brooklyn 2-day hang). The
            # auditor retires the poison and regenerates a clean contract.
            poisoned = False
            if has_contract:
                g = active_goal_query(db, state.get("project_id"))
                poisoned = bool(g) and _contract_poisoned(db, state.get("project_id"), g)
        finally:
            db.close()
        if not has_contract or poisoned or os.getenv("PGE_FORCE_AUDIT"):
            why = "no contract yet" if not has_contract else (
                "existing contract failed quality gate" if poisoned else "forced")
            print(f"🛡️  Plan ready — routing to auditor ({why}).")
            return "auditor"
    except Exception as e:
        print(f"Contract check failed ({e}) — proceeding to executor.")
    return "executor"


def evaluator_router(state: AgentState) -> str:
    """After every evaluation the loop returns to the PLANNER so the plan can
    evolve with what was just learned (failed tests, evaluator feedback).
    Only a blocker or the turn ceiling ends the run."""
    if state.get("goal_verified"):
        print("🏁 Goal independently verified — run ENDS here.")
        return "end"
    if state.get("turn_count", 0) >= MAX_TURNS:
        print("⏱️  Turn ceiling reached — ending run (state persisted in Postgres).")
        return "end"
    decision = (state.get("decision") or "continue").lower()
    if "blocked" in decision:
        print("🚧 Blocked — ending run for human steer (no busy-loop).")
        return "end"
    return "planner"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
workflow = StateGraph(AgentState)

workflow.add_node("auditor", auditor_node)
workflow.add_node("planner", planner_node)
workflow.add_node("executor", executor_node)
workflow.add_node("evaluator", evaluator_with_control)

# Flow: planner plans first; on the first pass its plan is forwarded to the
# auditor, which derives the immutable dual contract (checklist + test list);
# then executor works and the evaluator (after independently running the
# tests) ALWAYS hands control back to the planner so the plan evolves.
workflow.set_entry_point("planner")

workflow.add_conditional_edges(
    "planner",
    planner_router,
    {"auditor": "auditor", "executor": "executor", "end": END},
)

workflow.add_edge("auditor", "executor")
workflow.add_edge("executor", "evaluator")

workflow.add_conditional_edges(
    "evaluator",
    evaluator_router,
    {"planner": "planner", "end": END},
)

app = workflow.compile()

if __name__ == "__main__":
    print("LangGraph Autonomy Engine Compiled Successfully.")
