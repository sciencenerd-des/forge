import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, false, func, literal_column, or_, select
from sqlalchemy.orm import Session

import forge_config

from ..database import Base, engine
from ..models import (
    HermesCheckpoint,
    HermesEvent,
    HermesFileChange,
    HermesGoal,
    HermesMemoryItem,
    HermesMessage,
    HermesProject,
    HermesRuntimeMetadata,
    HermesSession,
    HermesTask,
    HermesTestRun,
)


def _utcnow() -> datetime:
    """Naive UTC now. DB columns are TIMESTAMP WITHOUT TIME ZONE, so we keep
    timestamps naive while avoiding the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Schema bootstrap is BEST-EFFORT at import: the engine should import
# without a live database (unit tests, `forge config`, CLI help). A real
# run requires a reachable DB; bootstrap is re-attempted there.
def _bootstrap_schema():
    from sqlalchemy import text
    # 1) The pgvector extension must exist BEFORE create_all — some models
    #    declare VECTOR(768) columns, so the type has to be registered first.
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            conn.commit()
    # 2) Create the base tables.
    Base.metadata.create_all(bind=engine)
    # 3) Idempotent column/constraint migrations on the now-existing tables.
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE hermes_goals ADD COLUMN IF NOT EXISTS embedding vector(768);"))
            conn.execute(text("ALTER TABLE hermes_tasks ADD COLUMN IF NOT EXISTS embedding vector(768);"))
            conn.execute(text("ALTER TABLE hermes_memory_items ADD COLUMN IF NOT EXISTS embedding vector(768);"))
            conn.execute(text("ALTER TABLE hermes_tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;"))
            conn.execute(text("ALTER TABLE hermes_tasks ADD COLUMN IF NOT EXISTS no_progress_count integer NOT NULL DEFAULT 0;"))
            conn.execute(text("ALTER TABLE hermes_tasks ADD COLUMN IF NOT EXISTS evidence_baseline_at timestamp;"))
            conn.execute(text("ALTER TABLE hermes_tasks ADD COLUMN IF NOT EXISTS last_progress_at timestamp;"))
            conn.commit()
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'hermes_context_compression_snapshots_project_id_fkey'
                                 AND confdeltype <> 'c') THEN
                        ALTER TABLE hermes_context_compression_snapshots
                            DROP CONSTRAINT hermes_context_compression_snapshots_project_id_fkey;
                        ALTER TABLE hermes_context_compression_snapshots
                            ADD CONSTRAINT hermes_context_compression_snapshots_project_id_fkey
                            FOREIGN KEY (project_id) REFERENCES hermes_projects(id) ON DELETE CASCADE;
                    END IF;
                    IF EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'hermes_context_compression_snapshots_goal_id_fkey'
                                 AND confdeltype <> 'n') THEN
                        ALTER TABLE hermes_context_compression_snapshots
                            DROP CONSTRAINT hermes_context_compression_snapshots_goal_id_fkey;
                        ALTER TABLE hermes_context_compression_snapshots
                            ADD CONSTRAINT hermes_context_compression_snapshots_goal_id_fkey
                            FOREIGN KEY (goal_id) REFERENCES hermes_goals(id) ON DELETE SET NULL;
                    END IF;
                    IF EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'hermes_context_compression_snapshots_task_id_fkey'
                                 AND confdeltype <> 'n') THEN
                        ALTER TABLE hermes_context_compression_snapshots
                            DROP CONSTRAINT hermes_context_compression_snapshots_task_id_fkey;
                        ALTER TABLE hermes_context_compression_snapshots
                            ADD CONSTRAINT hermes_context_compression_snapshots_task_id_fkey
                            FOREIGN KEY (task_id) REFERENCES hermes_tasks(id) ON DELETE SET NULL;
                    END IF;
                END $$;
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_messages_content_tsvector "
                              "ON messages USING gin (to_tsvector('english', content));"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp "
                              "ON messages (session_id, timestamp);"))
            conn.commit()

try:
    _bootstrap_schema()
except Exception as _schema_exc:  # pragma: no cover
    import logging as _lg
    _lg.getLogger(__name__).warning(
        'DB schema bootstrap skipped (%s): %s', type(_schema_exc).__name__, _schema_exc)

class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        if not text or not text.strip():
            return None
        import requests
        url = "http://127.0.0.1:1234/v1/embeddings"
        payload = {
            "model": "text-embedding-nomic-embed-text-v1.5",
            "input": text
        }
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                return res.json()["data"][0]["embedding"]
        except Exception:
            pass
        return None

    def create_project(self, name: str, repo_path: str, description: str = "", project_id: Optional[str] = None) -> HermesProject:
        project = HermesProject(id=project_id, name=name, repo_path=repo_path, description=description)
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        return project

    def create_goal(self, project_id: str, title: str, description: str = "", 
                    success_criteria: List[str] = None, priority: int = 3) -> HermesGoal:
        emb = self._generate_embedding(f"{title} {description or ''}")
        goal = HermesGoal(
            project_id=project_id,
            title=title,
            description=description,
            success_criteria=success_criteria or [],
            priority=priority,
            embedding=emb
        )
        self.db.add(goal)
        self.db.commit()
        self.db.refresh(goal)
        return goal

    def create_task(self, project_id: str, goal_id: str, title: str, 
                     description: str = "", status: str = "proposed",
                     priority: int = 3, acceptance_criteria: List[str] = None,
                     verification_required: bool = True) -> HermesTask:
        emb = self._generate_embedding(f"{title} {description or ''}")
        task = HermesTask(
            project_id=project_id,
            goal_id=goal_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            acceptance_criteria=acceptance_criteria or [],
            verification_required=verification_required,
            embedding=emb
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def set_active_task(self, project_id: str, task_id: str):
        task = self.db.query(HermesTask).filter(
            HermesTask.id == task_id, HermesTask.project_id == project_id).first()
        if task:
            task.status = "active"
            if task.evidence_baseline_at is None:
                task.evidence_baseline_at = _utcnow()
            self.db.commit()
            self.db.refresh(task)
            return task
        return None

    def record_task_attempt(self, project_id: str, task_id: str, made_progress: Optional[bool] = None) -> HermesTask:
        task = self.db.query(HermesTask).filter(
            HermesTask.id == task_id, HermesTask.project_id == project_id).first()
        if not task:
            raise ValueError("Task not found.")
        if made_progress is None:
            progress_floor = task.last_progress_at or task.evidence_baseline_at or task.created_at
            made_progress = (
                self.db.query(HermesFileChange).filter(
                    HermesFileChange.task_id == task_id,
                    HermesFileChange.created_at >= progress_floor,
                ).first() is not None
                or self.db.query(HermesTestRun).filter(
                    HermesTestRun.task_id == task_id,
                    HermesTestRun.status == "success",
                    HermesTestRun.created_at >= progress_floor,
                ).first() is not None
            )
        task.attempt_count = (task.attempt_count or 0) + 1
        if made_progress:
            task.no_progress_count = 0
            task.last_progress_at = _utcnow()
        else:
            task.no_progress_count = (task.no_progress_count or 0) + 1
        self.db.commit()
        self.db.refresh(task)
        return task

    def complete_task(self, project_id: str, task_id: str) -> HermesTask:
        task = self.db.query(HermesTask).filter(HermesTask.id == task_id, HermesTask.project_id == project_id).first()
        if not task:
            raise ValueError("Task not found.")
            
        if task.verification_required:
            # Check for associated successful test runs or file changes
            baseline = task.evidence_baseline_at or task.created_at
            has_success_test = self.db.query(HermesTestRun).filter(
                HermesTestRun.task_id == task_id, 
                HermesTestRun.status == "success",
                HermesTestRun.created_at >= baseline,
            ).first() is not None
            
            has_file_change = self.db.query(HermesFileChange).filter(
                HermesFileChange.task_id == task_id,
                HermesFileChange.created_at >= baseline,
            ).first() is not None
            
            if not (has_success_test or has_file_change):
                raise ValueError("Verification required: Task needs fresh evidence created after activation.")
        
        task.status = "completed"
        task.completed_at = _utcnow()
        self.db.commit()
        self.db.refresh(task)
        
        # Record event
        self.record_event(
            project_id=project_id,
            task_id=task_id,
            event_type="task_completed",
            actor="system",
            content=f"Completed Task: {task.title}"
        )
        
        return task

    def record_event(self, project_id: str, task_id: str = None, 
                      event_type: str = "", actor: str = "hermes", 
                      content: str = "", metadata: Dict[str, Any] = None) -> HermesEvent:
        event = HermesEvent(
            project_id=project_id,
            task_id=task_id,
            event_type=event_type,
            actor=actor,
            content=content,
            event_metadata=metadata or {}
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    def record_memory_item(self, project_id: str, task_id: str = None, 
                            source_event_id: str = None, memory_type: str = "", 
                            content: str = "", confidence: float = 0.8, 
                            importance: int = 3, tags: List[str] = None,
                            file_path: str = None, supersedes_id: str = None) -> HermesMemoryItem:
        emb = self._generate_embedding(content)
        item = HermesMemoryItem(
            project_id=project_id,
            task_id=task_id,
            source_event_id=source_event_id,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            importance=importance,
            tags=tags or [],
            file_path=file_path,
            supersedes_id=supersedes_id,
            embedding=emb
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def record_decision(self, project_id: str, task_id: str = None, 
                         content: str = "", context: str = "") -> HermesMemoryItem:
        event = self.record_event(project_id, task_id, "decision", content=f"Decision: {content}")
        return self.record_memory_item(
            project_id=project_id,
            task_id=task_id,
            source_event_id=event.id,
            memory_type="decision",
            content=content,
            tags=["decision"]
        )

    def record_constraint(self, project_id: str, content: str) -> HermesMemoryItem:
        return self.record_memory_item(
            project_id=project_id,
            memory_type="constraint",
            content=content,
            importance=5,
            tags=["constraint"]
        )

    def record_learning_stage(self, project_id: str, task_id: str, stage: str,
                              summary: str, evidence: Dict[str, Any],
                              rule: str = "") -> HermesMemoryItem:
        """Persist one auditable Fail->Investigate->Verify->Distill learning step."""
        allowed = {"fail", "investigate", "verify", "distill"}
        if stage not in allowed:
            raise ValueError(f"Unsupported learning stage: {stage}")
        if stage in {"verify", "distill"} and evidence.get("verification_passed") is not True:
            raise ValueError(f"learning_{stage} requires independent passing evidence")
        payload = {
            "stage": stage,
            "summary": summary,
            "evidence": evidence,
            "rule": rule,
        }
        return self.record_memory_item(
            project_id=project_id,
            task_id=task_id,
            memory_type=f"learning_{stage}",
            content=json.dumps(payload, sort_keys=True),
            importance=5 if stage in {"verify", "distill"} else 4,
            tags=["learning-cycle", stage, "verified" if stage in {"verify", "distill"} else "unverified"],
        )

    def record_learning_failure(self, project_id: str, task_id: str,
                                failed_tests: List[Dict[str, Any]]) -> HermesMemoryItem:
        """Record observed failure and diagnosis without making either reusable."""
        evidence = {"failed_tests": failed_tests[:4], "source": "independent_evaluator"}
        failure = self.record_learning_stage(
            project_id, task_id, "fail",
            "Independent rubric verification failed.", evidence)
        self.record_learning_stage(
            project_id, task_id, "investigate",
            "Failure is localized to the listed deterministic commands and outputs.", evidence)
        return failure

    def promote_verified_learning(self, project_id: str, task_id: str,
                                  passing_tests: List[Dict[str, Any]]) -> Optional[HermesMemoryItem]:
        """Promote a prior failure only after the same independent checks pass."""
        prior = self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id,
            HermesMemoryItem.task_id == task_id,
            HermesMemoryItem.memory_type == "learning_fail",
            HermesMemoryItem.status == "active",
        ).order_by(HermesMemoryItem.created_at.desc()).first()
        if prior is None:
            return None
        try:
            failed_ids = {
                str(item["id"]) for item in json.loads(prior.content)["evidence"]["failed_tests"]
                if item.get("id")
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        passing_ids = {
            str(item["id"]) for item in passing_tests
            if item.get("passed") and item.get("id")
        }
        recovered_ids = sorted(failed_ids & passing_ids)
        if not failed_ids or recovered_ids != sorted(failed_ids):
            return None

        rule = "Use independent command output as ground truth; never infer success from executor prose."
        fingerprint = hashlib.sha256(
            json.dumps({"failed_ids": sorted(failed_ids), "rule": rule}, sort_keys=True).encode()
        ).hexdigest()[:16]
        tag = f"fingerprint:{fingerprint}"
        candidates = self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id,
            HermesMemoryItem.task_id == task_id,
            HermesMemoryItem.memory_type == "learning_distill",
            HermesMemoryItem.status == "active",
        ).order_by(HermesMemoryItem.created_at.desc()).all()
        existing = next((item for item in candidates if tag in (item.tags or [])), None)
        if existing:
            return existing

        evidence = {
            "source": "independent_evaluator",
            "verification_passed": True,
            "recovered_test_ids": recovered_ids,
            "passing_tests": [
                item for item in passing_tests if str(item.get("id")) in failed_ids
            ],
            "failure_memory_id": prior.id,
        }
        self.record_learning_stage(
            project_id, task_id, "verify",
            "Previously failing independent checks now pass.", evidence)
        distilled = self.record_learning_stage(
            project_id, task_id, "distill",
            "Repair the smallest failing behavior, then rerun the affected check before full verification.",
            evidence, rule=rule)
        distilled.tags = [*distilled.tags, tag]
        self.db.commit()
        self.db.refresh(distilled)
        return distilled

    def record_file_change(self, project_id: str, task_id: str = None, 
                            file_path: str = "", change_summary: str = "", 
                            reason: str = "") -> HermesFileChange:
        change = HermesFileChange(
            project_id=project_id,
            task_id=task_id,
            file_path=file_path,
            change_summary=change_summary,
            reason=reason
        )
        self.db.add(change)
        self.db.commit()
        self.db.refresh(change)
        return change

    def record_test_run(self, project_id: str, task_id: str = None, 
                         command: str = "", status: str = "", 
                         output_summary: str = "", failure_summary: str = "") -> HermesTestRun:
        run = HermesTestRun(
            project_id=project_id,
            task_id=task_id,
            command=command,
            status=status,
            output_summary=output_summary,
            failure_summary=failure_summary
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def create_checkpoint(self, project_id: str, goal_id: str = None, 
                           task_id: str = None, summary: str = "", 
                           current_state: Dict[str, Any] = None, 
                           next_actions: List[str] = None, 
                           open_risks: List[str] = None) -> HermesCheckpoint:
        checkpoint = HermesCheckpoint(
            project_id=project_id,
            goal_id=goal_id,
            task_id=task_id,
            summary=summary,
            current_state=current_state or {},
            next_actions=next_actions or [],
            open_risks=open_risks or []
        )
        self.db.add(checkpoint)
        self.db.commit()
        self.db.refresh(checkpoint)
        return checkpoint

    def sync_sqlite_to_postgres(self, state_db_path: Optional[str] = None):
        import sqlite3
        
        if not state_db_path:
            state_db_path = os.getenv("HERMES_STATE_DB_PATH", forge_config.state_db_path())
            
        if not os.path.exists(state_db_path):
            return {"status": "error", "message": f"state.db not found at {state_db_path}"}
            
        try:
            sqlite_conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
            sqlite_conn.row_factory = sqlite3.Row
            sqlite_cursor = sqlite_conn.cursor()
            
            sqlite_cursor.execute("SELECT * FROM sessions")
            source_sessions = sqlite_cursor.fetchall()
            session_columns = {column.name for column in HermesSession.__table__.columns}
            synced_sessions = 0
            updated_sessions = 0
            for source_row in source_sessions:
                values = {k: v for k, v in dict(source_row).items() if k in session_columns}
                existing = self.db.get(HermesSession, values["id"])
                if existing is None:
                    self.db.add(HermesSession(**values))
                    synced_sessions += 1
                else:
                    for key, value in values.items():
                        if key != "id":
                            setattr(existing, key, value)
                    updated_sessions += 1

            existing_message_ids = {row[0] for row in self.db.query(HermesMessage.id).all()}
            sqlite_cursor.execute("SELECT * FROM messages ORDER BY id")
            source_messages = sqlite_cursor.fetchall()
            message_columns = {column.name for column in HermesMessage.__table__.columns}
            synced_messages = 0
            for source_row in source_messages:
                values = {k: v for k, v in dict(source_row).items() if k in message_columns}
                if values["id"] in existing_message_ids:
                    continue
                self.db.add(HermesMessage(**values))
                synced_messages += 1

            sqlite_cursor.execute("SELECT version FROM schema_version LIMIT 1")
            version_row = sqlite_cursor.fetchone()
            metadata = {"sqlite_schema_version": str(version_row[0]) if version_row else "unknown"}
            sqlite_cursor.execute("SELECT key, value FROM state_meta")
            metadata.update({row[0]: row[1] for row in sqlite_cursor.fetchall()})
            for key, value in metadata.items():
                existing = self.db.get(HermesRuntimeMetadata, key)
                if existing:
                    existing.value = value
                else:
                    self.db.add(HermesRuntimeMetadata(key=key, value=value))

            self.db.commit()
            sqlite_conn.close()
            return {
                "status": "success",
                "inserted_sessions": synced_sessions,
                "updated_sessions": updated_sessions,
                "inserted_messages": synced_messages,
                "source_sessions": len(source_sessions),
                "source_messages": len(source_messages),
                "runtime_metadata": metadata,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def build_context_pack(self, project_id: str, state_db_path: Optional[str] = None) -> Dict[str, Any]:
        """Build context only from durable records scoped to the active goal."""
        project = self.db.query(HermesProject).filter(HermesProject.id == project_id).first()
        if not project:
            return {"error": "Project not found"}

        goals = self.db.query(HermesGoal).filter(HermesGoal.project_id == project_id)
        goal = (goals.filter(HermesGoal.status != "completed")
                .order_by(HermesGoal.created_at.desc()).first()
                or goals.order_by(HermesGoal.created_at.desc()).first())
        active_task = None
        goal_task_ids = []
        if goal:
            goal_task_ids = [row[0] for row in self.db.query(HermesTask.id).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
            ).all()]
            active_task = self.db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
                HermesTask.status == "active",
            ).order_by(HermesTask.updated_at.desc()).first()

        now = _utcnow()
        superseded_ids = select(HermesMemoryItem.supersedes_id).where(
            HermesMemoryItem.supersedes_id.isnot(None))
        memory_scope = false()
        if goal_task_ids:
            memory_scope = HermesMemoryItem.task_id.in_(goal_task_ids)
        # Only explicit project constraints are global. Decisions, lessons,
        # mistakes and blockers must be attached to a task in the active goal.
        memory_scope = or_(
            memory_scope,
            and_(HermesMemoryItem.task_id.is_(None),
                 HermesMemoryItem.memory_type == "constraint"),
        )
        memories = self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id,
            memory_scope,
            HermesMemoryItem.status == "active",
            or_(HermesMemoryItem.expires_at.is_(None), HermesMemoryItem.expires_at > now),
            HermesMemoryItem.id.notin_(superseded_ids),
        ).order_by(
            HermesMemoryItem.importance.desc(),
            HermesMemoryItem.created_at.desc()
        ).limit(20).all()

        constraints = [m.content for m in memories if m.memory_type == "constraint"]
        decisions = [m.content for m in memories if m.memory_type == "decision"]
        # Consult only verified or distilled knowledge. Raw failures and guesses
        # remain auditable in Postgres but are not injected as instructions.
        lessons = [m.content for m in memories if m.memory_type in (
            "lesson", "learning_distill")]
        next_actions = [m.content for m in memories if m.memory_type == "next_action"]
        blockers = [m.content for m in memories if m.memory_type in ["bug", "blocker"]]
        
        recent_files = []
        if goal_task_ids:
            recent_files = self.db.query(HermesFileChange).filter(
                HermesFileChange.project_id == project_id,
                HermesFileChange.task_id.in_(goal_task_ids),
            ).order_by(HermesFileChange.created_at.desc()).limit(8).all()

        pack = {
            "PROJECT": {
                "name": project.name,
                "goal": goal.title if goal else "No goal defined",
                "status": project.status,
            },
            "ACTIVE_TASK": {
                "title": active_task.title if active_task else "None",
                "status": active_task.status if active_task else "None",
                "acceptance_criteria": active_task.acceptance_criteria if active_task else [],
                "current_progress": active_task.description if active_task else "",
            },
            "NON_NEGOTIABLE_CONSTRAINTS": constraints,
            "DECISIONS_ALREADY_MADE": decisions,
            "LESSONS_AND_MISTAKES": lessons,
            "RELEVANT_FILES": [{"file_path": f.file_path, "summary": f.change_summary} for f in recent_files],
            "OPEN_BUGS_BLOCKERS_RISKS": blockers,
            "NEXT_BEST_ACTION": next_actions[0] if next_actions else "No specific next action recorded",
            "MEMORY_EVIDENCE": [{"id": m.id, "type": m.memory_type} for m in memories[:5]],
            "RUNTIME_TOOL_STATE": {
                "recent_tool_invocations": [],
                "active_sandbox_info": {"workspace": project.repo_path}
            }
        }
        try:
            from forge_runtime.context_pack import build_repo_context_pack

            pack["REPO_CONTEXT"] = build_repo_context_pack(
                project.repo_path,
                task_text=" ".join(
                    part for part in [
                        goal.title if goal else "",
                        goal.description if goal else "",
                        active_task.title if active_task else "",
                        active_task.description if active_task else "",
                    ]
                    if part
                ),
            )
        except Exception as exc:
            pack["REPO_CONTEXT"] = {
                "schema_version": 1,
                "cache_status": "error",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "selected_files": [],
                "invalidation_rules": [],
            }
        from ..context_compression import compress_context_pack
        return compress_context_pack(
            self.db,
            project_id=project_id,
            goal_id=goal.id if goal else None,
            task_id=active_task.id if active_task else None,
            pack=pack,
        )


    def classify_task_alignment(self, project_id: str, user_request: str) -> str:
        goals = self.db.query(HermesGoal).filter(HermesGoal.project_id == project_id)
        goal = (goals.filter(HermesGoal.status != "completed")
                .order_by(HermesGoal.created_at.desc()).first()
                or goals.order_by(HermesGoal.created_at.desc()).first())
        active_task = None
        if goal:
            active_task = self.db.query(HermesTask).filter(
                HermesTask.project_id == project_id,
                HermesTask.goal_id == goal.id,
                HermesTask.status == "active",
            ).order_by(HermesTask.updated_at.desc()).first()
        if not active_task:
            return "clarification"
        
        # Helper to tokenize and clean text
        def get_keywords(text: str) -> set:
            if not text:
                return set()
            stop_words = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "is", "this", "project", "task", "run", "do", "how", "why", "what", "with", "from"}
            words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
            return {w for w in words if w not in stop_words and len(w) > 1}
        
        req_words = get_keywords(user_request)
        if not req_words:
            return "aligned"
            
        task_words = get_keywords(active_task.title)
        task_words.update(get_keywords(active_task.description))
        for criterion in active_task.acceptance_criteria:
            task_words.update(get_keywords(criterion))
            
        # If there's any overlap with task keywords, it's aligned
        overlap = req_words.intersection(task_words)
        if overlap:
            return "aligned"

        # Goal terms provide a weaker but valid alignment signal. Generic tool
        # verbs such as read/write/run never bypass this content check.
        goal_words = get_keywords(goal.title)
        goal_words.update(get_keywords(goal.description or ""))
        if req_words.intersection(goal_words):
            return "aligned"
            
        return "distraction"

    def _rerank_results(self, query: str, items: List[Any], limit: int) -> List[Any]:
        if not items:
            return items
        try:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder('BAAI/bge-reranker-base')
            pairs = [[query, item.content] for item in items]
            scores = model.predict(pairs)
            ranked = sorted(zip(items, scores), key=lambda x: x[1], reverse=True)
            return [x[0] for x in ranked[:limit]]
        except Exception:
            return items[:limit]

    def search_memory(self, project_id: str, query: str, memory_type: Optional[str] = None, limit: int = 10) -> List[HermesMemoryItem]:
        db_query = self.db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id
        )
        if memory_type:
            db_query = db_query.filter(HermesMemoryItem.memory_type == memory_type)
            
        if self.db.bind.dialect.name == "postgresql":
            query_emb = self._generate_embedding(query)
            if query_emb:
                db_query = db_query.order_by(HermesMemoryItem.embedding.op('<=>')(query_emb))
                results = db_query.limit(limit * 2).all()  # fetch more for reranking
                return self._rerank_results(query, results, limit)
            
        if self.db.bind.dialect.name == "sqlite":
            db_query = db_query.filter(HermesMemoryItem.content.ilike(f"%{query}%"))
        else:
            db_query = db_query.filter(
                func.to_tsvector(literal_column("'english'"), HermesMemoryItem.content).op('@@')(
                    func.plainto_tsquery(literal_column("'english'"), query)
                )
            )
            
        results = db_query.limit(limit * 2).all()
        return self._rerank_results(query, results, limit)

    def search_events(self, project_id: str, query: str, limit: int = 10) -> List[HermesEvent]:
        db_query = self.db.query(HermesEvent).filter(
            HermesEvent.project_id == project_id
        )
        
        # Detect dialect and perform full-text search
        if self.db.bind.dialect.name == "sqlite":
            db_query = db_query.filter(HermesEvent.content.ilike(f"%{query}%"))
        else:
            db_query = db_query.filter(
                func.to_tsvector(literal_column("'english'"), HermesEvent.content).op('@@')(
                    func.plainto_tsquery(literal_column("'english'"), query)
                )
            )
            
        return db_query.limit(limit).all()

    def consolidate_old_logs(self, project_id: str, days_threshold: int = 7) -> Dict[str, Any]:
        """Consolidate old fine-grained events and checkpoints into high-level digest items."""
        from datetime import timedelta
        cutoff_date = _utcnow() - timedelta(days=days_threshold)
        
        old_completed_tasks = self.db.query(HermesTask).filter(
            HermesTask.project_id == project_id,
            HermesTask.status == "completed",
            HermesTask.completed_at < cutoff_date
        ).all()
        
        consolidated_count = 0
        for task in old_completed_tasks:
            existing_digest = self.db.query(HermesMemoryItem).filter(
                HermesMemoryItem.project_id == project_id,
                HermesMemoryItem.task_id == task.id,
                HermesMemoryItem.memory_type == "task_consolidation"
            ).first()
            if existing_digest:
                continue
                
            events = self.db.query(HermesEvent).filter(
                HermesEvent.project_id == project_id,
                HermesEvent.task_id == task.id
            ).order_by(HermesEvent.created_at.asc()).all()
            
            file_changes = self.db.query(HermesFileChange).filter(
                HermesFileChange.project_id == project_id,
                HermesFileChange.task_id == task.id
            ).all()
            
            test_runs = self.db.query(HermesTestRun).filter(
                HermesTestRun.project_id == project_id,
                HermesTestRun.task_id == task.id
            ).all()
            
            if not events and not file_changes and not test_runs:
                continue
                
            lines = [f"# Consolidation Summary: {task.title}", f"Description: {task.description or 'None'}", ""]
            if file_changes:
                lines.append("## Files Modified:")
                for fc in file_changes:
                    lines.append(f"- `{fc.file_path}`: {fc.change_summary} (Reason: {fc.reason or 'None'})")
                lines.append("")
                
            if test_runs:
                lines.append("## Test Invocations:")
                for tr in test_runs:
                    lines.append(f"- Command `{tr.command}` -> Status: {tr.status}")
                lines.append("")
                
            if events:
                lines.append("## Activity Timeline:")
                for ev in events:
                    lines.append(f"- [{ev.actor}] {ev.content}")
                lines.append("")
                
            summary_content = "\n".join(lines)
            
            self.record_memory_item(
                project_id=project_id,
                task_id=task.id,
                memory_type="task_consolidation",
                content=summary_content,
                importance=2,
                tags=["consolidation", "digest", task.title.lower()]
            )
            
            for fc in file_changes:
                self.db.delete(fc)
            for tr in test_runs:
                self.db.delete(tr)
            for ev in events:
                self.db.delete(ev)
                
            checkpoints = self.db.query(HermesCheckpoint).filter(
                HermesCheckpoint.project_id == project_id,
                HermesCheckpoint.task_id == task.id
            ).all()
            for cp in checkpoints:
                self.db.delete(cp)
                
            consolidated_count += 1
            
        if consolidated_count > 0:
            self.db.commit()
            
        return {
            "status": "success",
            "consolidated_tasks_count": consolidated_count
        }
