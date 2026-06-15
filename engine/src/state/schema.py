from typing import List, Dict, Optional, TypedDict
from pydantic import BaseModel, Field
from datetime import datetime

# --- Schema Models ---

class Heartbeat(BaseModel):
    """The forced write-back from the Executor model."""
    progress_summary: str
    next_task_description: str
    blocker: Optional[str] = None
    resume_instruction: str

class Task(BaseModel):
    """A discrete unit of work."""
    id: str
    title: str
    description: str
    status: str  # 'proposed', 'active', 'completed', 'blocked'
    next_step: Optional[str] = None
    priority: int
    parent_id: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    attempts: int = 0

class Goal(BaseModel):
    """The high-level project objective."""
    id: str
    title: str
    description: str
    status: str
    success_criteria: List[str]
    priority: int

# --- LangGraph State ---

class AgentState(TypedDict):
    """The global state that flows through the LangGraph."""
    project_id: Optional[str]
    goal: Goal
    task_queue: List[Task]
    active_task: Optional[Task]
    heartbeat: Optional[Heartbeat]
    history: List[Dict[str, str]]
    # Control / routing
    decision: Optional[str]            # 'complete' | 'blocked' | 'continue'
    task_attempts: Dict[str, int]      # active_task.id -> executor attempts (anti-ping-pong)
    last_eval: Optional[Dict]          # evaluator feedback fed to the next executor attempt
    goal_verified: Optional[bool]
    test_fail_streaks: Optional[Dict]
    last_pass_ids: Optional[List[str]]
    last_actions: Optional[List[str]]   # executor tool-call signatures this turn
    action_repeats: Optional[Dict]      # signature -> consecutive-turn repeat count
    prev_actions: Optional[List[str]]   # previous turn's signatures (watchdog comparison)
    progress_stall: Optional[Dict]      # {"n": consecutive no-progress cycles} -> hard-stop  # audit tests passing at last eval (ratchet baseline)  # test_id -> {"count": n, "output": last} (suspect-test breaker)      # set by the evaluator when ALL audit tests pass -> loop ENDS
    recent_action_signatures: List[str]  # bounded signatures used for deterministic stagnation checks
    selected_lesson_ids: List[str]       # durable lessons injected into the current model context
    steering_reason: Optional[str]       # deterministic router explanation
    dynamic_audit_context: Optional[Dict]  # fresh failure-focused auditor pack
    # Metadata for persistence
    turn_count: int
    current_run_id: str
    timestamp: datetime
