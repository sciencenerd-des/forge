from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class TaskStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class TaskNode:
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    parent_id: str | None = None
    status: TaskStatus = TaskStatus.PROPOSED
    priority: int = 3
    attempts: int = 0
    dependencies: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def atomic(self) -> bool:
        return bool(self.acceptance_criteria) and len(self.description) <= 500


@dataclass
class GoalPlan:
    goal_id: str
    objective: str
    success_criteria: list[str]
    tasks: list[TaskNode]


@dataclass(frozen=True)
class Lesson:
    fingerprint: str
    task_id: str
    failure_type: str
    observation: str
    prevention: str
    evidence_ids: tuple[str, ...]
    confidence: float = 0.9


@dataclass(frozen=True)
class SteeringDecision:
    action: str
    task_id: str | None
    reason: str


class SteeringEngine:
    def __init__(self, *, max_attempts: int = 18, stagnation_tolerance: int = 6) -> None:
        self.max_attempts = max_attempts
        self.stagnation_tolerance = stagnation_tolerance

    def choose_next(self, plan: GoalPlan) -> SteeringDecision:
        by_id = {task.id: task for task in plan.tasks}
        active = [task for task in plan.tasks if task.status == TaskStatus.ACTIVE]
        if len(active) > 1:
            return SteeringDecision("repair_plan", None, "multiple tasks are active")
        if active:
            task = active[0]
            if task.attempts >= self.max_attempts:
                return SteeringDecision("decompose", task.id, "task attempt ceiling reached")
            return SteeringDecision("execute", task.id, "continue the active atomic task")
        candidates = [
            task for task in plan.tasks
            if task.status == TaskStatus.PROPOSED
            and all(by_id.get(dependency) and by_id[dependency].status == TaskStatus.COMPLETED for dependency in task.dependencies)
        ]
        if candidates:
            task = sorted(candidates, key=lambda item: (item.priority, item.id))[0]
            return SteeringDecision("activate" if task.atomic else "decompose", task.id, "highest-priority unblocked task")
        if all(task.status == TaskStatus.COMPLETED for task in plan.tasks):
            return SteeringDecision("verify_goal", None, "all tasks completed; success criteria still require independent verification")
        blockers = [task.id for task in plan.tasks if task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}]
        return SteeringDecision("blocked", None, f"no runnable task; blockers={blockers}")

    def detect_stagnation(self, recent_signatures: Iterable[str]) -> bool:
        signatures = list(recent_signatures)[-self.stagnation_tolerance :]
        return len(signatures) == self.stagnation_tolerance and len(set(signatures)) <= 1

    def learn_from_failure(self, *, task_id: str, failure_type: str, observation: str, prevention: str, evidence_ids: Iterable[str]) -> Lesson:
        if not observation.strip() or not prevention.strip():
            raise ValueError("lessons require a concrete observation and prevention rule")
        evidence = tuple(sorted(set(evidence_ids)))
        if not evidence:
            raise ValueError("lessons require durable evidence IDs")
        digest = hashlib.sha256(f"{task_id}\0{failure_type}\0{observation.strip()}\0{prevention.strip()}".encode()).hexdigest()
        return Lesson(digest, task_id, failure_type, observation.strip(), prevention.strip(), evidence)

    def relevant_lessons(self, lessons: Iterable[Lesson], *, task_id: str, failure_types: set[str], limit: int = 8) -> list[Lesson]:
        selected = [lesson for lesson in lessons if lesson.task_id == task_id or lesson.failure_type in failure_types]
        unique = {lesson.fingerprint: lesson for lesson in selected}
        return sorted(unique.values(), key=lambda lesson: (-lesson.confidence, lesson.fingerprint))[:limit]
