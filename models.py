from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4


@dataclass
class ExecutionLog:
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = None
    action: str = ""
    output: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class Task:
    id: UUID = field(default_factory=uuid4)
    plan_id: UUID = None
    description: str = ""
    status: str = "pending"  # pending, in_progress, completed, failed
    dependencies: List[UUID] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    logs: List[ExecutionLog] = field(default_factory=list)

@dataclass
class Plan:
    id: UUID = field(default_factory=uuid4)
    state_id: UUID = None
    goals: List[str] = field(default_factory=list)
    status: str = "active"  # active, completed, failed
    tasks: List[Task] = field(default_factory=list)

@dataclass
class State:
    id: UUID = field(default_factory=uuid4)
    current_goal: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    memory: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "idle"  # idle, running, paused, completed, failed
    updated_at: datetime = field(default_factory=datetime.now)
    plan_id: UUID = None

# Schema Summary:
# 1. State: Represents the high-level status of the agent, including its current goal, 
#    working context, and long-term/short-term memory.
# 2. Plan: A sequence of goals derived from the State. It contains the list of 
#    Tasks required to achieve the current goal.
# 3. Task: Atomic units of work. Each task has a status, a list of dependencies
#    (other Task IDs), and a result field.
# 4. ExecutionLog: A chronological record of every action taken during a Task's execution.
