from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import SessionLocal


def _utcnow() -> datetime:
    """Naive UTC now (avoids the deprecated ``datetime.utcnow()``)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from app.models import HermesFileChange, HermesGoal, HermesMemoryItem, HermesProject, HermesTask, HermesTestRun
from app.services import MemoryService
from src.nodes.executor_node import record_tool_msg


@pytest.fixture()
def task_record():
    db = SessionLocal()
    project_id = "loop-control-test"
    try:
        for model in (HermesFileChange, HermesTestRun, HermesMemoryItem, HermesTask, HermesGoal):
            db.query(model).filter(model.project_id == project_id).delete(synchronize_session=False)
        db.query(HermesProject).filter(HermesProject.id == project_id).delete(synchronize_session=False)
        db.commit()
        service = MemoryService(db)
        service.create_project("test", "/tmp", project_id=project_id)
        goal = service.create_goal(project_id, "goal")
        task = service.create_task(project_id, goal.id, "task", status="proposed")
        service.set_active_task(project_id, task.id)
        yield project_id, task.id
    finally:
        db.rollback()
        for model in (HermesFileChange, HermesTestRun, HermesMemoryItem, HermesTask, HermesGoal):
            db.query(model).filter(model.project_id == project_id).delete(synchronize_session=False)
        db.query(HermesProject).filter(HermesProject.id == project_id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_attempts_and_no_progress_are_durable(task_record):
    project_id, task_id = task_record
    with SessionLocal() as first:
        task = MemoryService(first).record_task_attempt(project_id, task_id)
        assert (task.attempt_count, task.no_progress_count) == (1, 1)

    with SessionLocal() as second:
        MemoryService(second).record_test_run(project_id, task_id, "pytest", "success", "ok")
        task = MemoryService(second).record_task_attempt(project_id, task_id)
        assert (task.attempt_count, task.no_progress_count) == (2, 0)


def test_historical_evidence_cannot_complete_new_activation(task_record):
    project_id, task_id = task_record
    with SessionLocal() as db:
        task = db.get(HermesTask, task_id)
        task.evidence_baseline_at = _utcnow()
        db.add(HermesTestRun(
            project_id=project_id, task_id=task_id, command="old",
            status="success", output_summary="old",
            created_at=_utcnow() - timedelta(days=1),
        ))
        db.commit()
        with pytest.raises(ValueError, match="fresh evidence"):
            MemoryService(db).complete_task(project_id, task_id)


def test_context_consults_verified_learning_only(task_record):
    project_id, task_id = task_record
    with SessionLocal() as db:
        service = MemoryService(db)
        service.record_learning_failure(
            project_id, task_id,
            [{"id": "T1", "command": "pytest -q", "passed": False, "exit": 1}])
        service.promote_verified_learning(
            project_id, task_id,
            [{"id": "T1", "command": "pytest -q", "passed": True, "exit": 0}])
        pack = service.build_context_pack(project_id)
        lessons = " ".join(pack["LESSONS_AND_MISTAKES"])
        assert "Repair the smallest failing behavior" in lessons
        assert "Independent rubric verification failed" not in lessons
        assert db.query(HermesMemoryItem).filter(
            HermesMemoryItem.task_id == task_id,
            HermesMemoryItem.memory_type == "learning_fail").count() == 1


def test_distill_stage_rejects_unverified_evidence(task_record):
    project_id, task_id = task_record
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="independent passing evidence"):
            MemoryService(db).record_learning_stage(
                project_id, task_id, "distill", "unverified", {"exit": 1}, "unsafe rule")


def test_failed_verification_does_not_create_reusable_lesson(task_record):
    project_id, task_id = task_record
    with SessionLocal() as db:
        service = MemoryService(db)
        service.record_learning_failure(
            project_id, task_id, [{"id": "T1", "passed": False, "exit": 1}])

        assert db.query(HermesMemoryItem).filter(
            HermesMemoryItem.task_id == task_id,
            HermesMemoryItem.memory_type == "learning_distill").count() == 0
        assert service.build_context_pack(project_id)["LESSONS_AND_MISTAKES"] == []


def test_passing_reverification_promotes_one_deduplicated_lesson(task_record):
    project_id, task_id = task_record
    failed = [{"id": "T1", "command": "pytest -q", "passed": False, "exit": 1}]
    passed = [{"id": "T1", "command": "pytest -q", "passed": True, "exit": 0}]
    with SessionLocal() as db:
        service = MemoryService(db)
        service.record_learning_failure(project_id, task_id, failed)
        first = service.promote_verified_learning(project_id, task_id, passed)
        second = service.promote_verified_learning(project_id, task_id, passed)

        assert first is not None
        assert second.id == first.id
        active = db.query(HermesMemoryItem).filter(
            HermesMemoryItem.task_id == task_id,
            HermesMemoryItem.memory_type == "learning_distill",
            HermesMemoryItem.status == "active").all()
        assert len(active) == 1
        payload = __import__("json").loads(active[0].content)
        assert payload["evidence"]["verification_passed"] is True
        assert payload["evidence"]["recovered_test_ids"] == ["T1"]


def test_executor_has_bounded_in_turn_tool_loop():
    source = (Path(__file__).parent / "src/nodes/executor_node.py").read_text()
    assert 'PGE_MAX_EXECUTOR_ITERATIONS", "4"' in source
    assert "Executor reached maximum turn count" not in source
    assert "for iteration in range(max_iterations)" in source


def test_pge_planner_and_executor_use_role_specific_models():
    tools = (Path(__file__).parent / "hermes_tools.py").read_text()
    planner = (Path(__file__).parent / "src/nodes/planner_node.py").read_text()
    executor = (Path(__file__).parent / "src/nodes/executor_node.py").read_text()
    evaluator = (Path(__file__).parent / "src/nodes/evaluator_node.py").read_text()

    assert 'PGE_PLANNER_MODEL' in tools
    assert 'PGE_EXECUTOR_MODEL' in tools
    assert 'from hermes_tools import planner_llm' in planner
    assert 'from hermes_tools import executor_llm' in executor
    assert 'from hermes_tools import llm' in evaluator


def test_detached_runner_recovers_transient_model_failures_from_postgres():
    runner = (Path(__file__).parents[1] / "run_pge.py").read_text()
    assert 'PGE_MAX_TRANSIENT_FAILURES' in runner
    assert 'event_type="pge_transient_failure"' in runner
    assert 'durable state preserved, retrying from PostgreSQL' in runner


def test_detached_lifecycle_is_persisted_to_control_plane_postgres():
    launcher = (Path(__file__).parents[1] / "pge_launcher.py").read_text()
    assert "def _persist_lifecycle" in launcher
    assert "RunRecord" in launcher
    assert "RunEventRecord" in launcher
    assert "lifecycle is durable in PostgreSQL" in launcher


def test_executor_does_not_mirror_tools_to_latest_gateway_session(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    db_path = tmp_path / "state.db"
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, tool_name TEXT, content TEXT, timestamp REAL, active INTEGER)")
        conn.execute("INSERT INTO sessions (id) VALUES ('unrelated-chat')")

    real_connect = sqlite3.connect
    monkeypatch.setattr("src.nodes.executor_node.sqlite3.connect", lambda _path: real_connect(db_path))
    record_tool_msg("write_file", "project evidence")

    with real_connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_goal_tests_are_not_recorded_as_task_progress():
    source = (Path(__file__).parent / "src/nodes/evaluator_node.py").read_text()
    assert "project_id=project_id, task_id=None" in source
    assert "Atomic task advanced on fresh evidence" in source


def test_model_cannot_manufacture_evidence_records():
    tools = (Path(__file__).parent / "hermes_tools.py").read_text()
    executor = (Path(__file__).parent / "src/nodes/executor_node.py").read_text()
    schema_section = tools.split("EXECUTOR_SCHEMA =", 1)[1].split("class LLM", 1)[0]
    assert '"record_test_run"' not in schema_section
    assert '"record_file_change"' not in schema_section
    assert 'elif tool_name == "record_test_run"' not in executor
    assert 'elif tool_name == "record_file_change"' not in executor


def test_executor_recognizes_forge_tool_result_contract():
    executor = (Path(__file__).parent / "src/nodes/executor_node.py").read_text()
    assert "if tool_name == \"write_file\" and result.ok" in executor
    assert 'status="success" if result.ok else "failure"' in executor


def test_notebook_is_scratch_only_not_completion_evidence():
    tools = (Path(__file__).parent / "hermes_tools.py").read_text()
    executor = (Path(__file__).parent / "src/nodes/executor_node.py").read_text()
    schema_section = tools.split("EXECUTOR_SCHEMA =", 1)[1].split("class LLM", 1)[0]
    assert '"notebook_cell"' in schema_section
    evidence_section = executor.split("db_ev = SessionLocal()", 1)[1].split("finally:", 1)[0]
    assert 'tool_name == "write_file"' in evidence_section
    assert 'tool_name == "run_command"' in evidence_section
    assert 'tool_name == "notebook_cell"' not in evidence_section


def test_dynamic_auditor_context_is_failure_focused_and_bounded(task_record):
    project_id, task_id = task_record
    from src.nodes.auditor_node import build_dynamic_audit_context
    with SessionLocal() as db:
        task = db.get(HermesTask, task_id)
        task.description = "Implement parser behavior"
        db.commit()
        MemoryService(db).record_test_run(
            project_id, task_id, "python -m pytest parser", "failure", "assert 2 == 3")
    pack = build_dynamic_audit_context(
        project_id,
        [{"id": "T1", "command": "python -m pytest parser",
          "passed": False, "exit": 1, "output": "assert 2 == 3"}],
        {"action_repeats": {"read_file:abcd": 3}},
    )
    assert pack["active_task"]["id"] == task_id
    assert pack["failing_checks"][0]["id"] == "T1"
    assert pack["repeated_actions_to_avoid"] == ["read_file:abcd"]
    assert "Reproduce only T1" in pack["next_action"]
    assert len(pack["workspace_files"]) <= 40
    assert any("fetch_doc" in item for item in pack["capability_guidance"])
