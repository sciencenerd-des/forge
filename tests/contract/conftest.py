from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


RUN_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"
GOAL_ID = "33333333-3333-3333-3333-333333333333"
EVENT_ID = "44444444-4444-4444-4444-444444444444"
APPROVAL_ID = "55555555-5555-5555-5555-555555555555"
A2A_TASK_ID = "66666666-6666-6666-6666-666666666666"
NOW = datetime(2099, 7, 12, tzinfo=timezone.utc)
HEADERS = {"Authorization": "Bearer fixture-token"}


@pytest.fixture()
def contract_client(tmp_path, monkeypatch):
    """A real FastAPI routing stack with deterministic external seams."""
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "fixture-token")

    import app.database as pge_database
    import app.services as pge_services
    import control_plane.api as api
    import forge_config
    import pge_launcher
    from control_plane.database import Base
    from control_plane.models import ApprovalRecord, RunEventRecord, RunRecord

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        run = RunRecord(
            id=RUN_ID,
            project_id=PROJECT_ID,
            goal_id=GOAL_ID,
            provider_id="executor",
            status="running",
            current_node="executor",
            turn=2,
            max_turns=24,
            heartbeat_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        db.add(run)
        db.add(
            RunEventRecord(
                id=EVENT_ID,
                run_id=RUN_ID,
                sequence=1,
                event_type="run.running",
                actor="pge-supervisor",
                payload={"batch": 2},
                created_at=NOW,
            )
        )
        db.add(
            ApprovalRecord(
                id=APPROVAL_ID,
                run_id=RUN_ID,
                action_type="external_action",
                action_digest="a" * 64,
                action_preview={"url": "https://example.test"},
                risk="high",
                status="pending",
                requested_by="planner",
                expires_at=NOW + timedelta(minutes=15),
                created_at=NOW,
            )
        )
        db.commit()

    def override_db():
        with sessions() as db:
            yield db

    api.app.dependency_overrides[api.get_db] = override_db
    monkeypatch.setattr(api, "create_schema", lambda: None)
    monkeypatch.setattr(
        api,
        "list_runtime_snapshots",
        lambda: [{
            "id": RUN_ID,
            "project_id": PROJECT_ID,
            "project_name": "fixture-project",
            "goal_id": GOAL_ID,
            "goal_title": "Fixture goal",
            "status": "running",
            "current_node": "executor",
            "batch": 2,
            "pid": 4242,
            "updated_at": NOW.isoformat(),
            "active_task": "Implement fixture",
            "attempt_count": 1,
            "no_progress_count": 0,
            "task_total": 3,
            "task_completed": 1,
            "file_count": 2,
            "test_count": 4,
            "log_tail": ["executor running"],
            "model": "fixture-model",
        }],
    )
    monkeypatch.setattr(
        api,
        "list_project_snapshots",
        lambda: [{
            "id": PROJECT_ID,
            "name": "fixture-project",
            "repo_path": "/tmp/fixture-project",
            "goal_count": 1,
            "active_goal": "Fixture goal",
            "active_goal_status": "active",
            "task_total": 3,
            "task_completed": 1,
        }],
    )
    monkeypatch.setattr(
        pge_launcher,
        "load_run_state",
        lambda: {PROJECT_ID: {"run_id": RUN_ID, "pid": 4242}},
    )
    monkeypatch.setattr(pge_launcher, "process_is_alive", lambda _pid: False)
    monkeypatch.setattr(pge_launcher, "update_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        pge_launcher,
        "launch_pge",
        lambda project_id, **_kwargs: {
            "status": "success",
            "started": True,
            "already_running": False,
            "run_id": RUN_ID,
            "pid": 4242,
            "log": "/tmp/fixture.log",
            "source": "control-plane:web",
            "note": "Detached PGE supervisor started; lifecycle is durable in PostgreSQL.",
        },
    )

    class FakePgeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeMemoryService:
        def __init__(self, _db):
            pass

        def create_goal(self, **_kwargs):
            return object()

    monkeypatch.setattr(pge_database, "SessionLocal", lambda: FakePgeDb())
    monkeypatch.setattr(pge_services, "MemoryService", FakeMemoryService)
    monkeypatch.setattr(forge_config, "ensure_default_project", lambda _db: PROJECT_ID)

    # A2A adapter seams: task_bridge binds SessionLocal/MemoryService at import
    # time and mints uuid4 task ids, so it gets its own deterministic stubs.
    import uuid as uuid_module

    import forge_a2a.task_bridge as task_bridge
    from app.models import ForgeProject

    class FakeGoal:
        id = GOAL_ID
        status = "active"

    class FakeA2AProject:
        repo_path = "/tmp/fixture-project"

    class FakeA2ADb(FakePgeDb):
        def get(self, model, _key):
            return FakeA2AProject() if model is ForgeProject else FakeGoal()

    class FakeA2AMemoryService(FakeMemoryService):
        def create_goal(self, **_kwargs):
            return FakeGoal()

    monkeypatch.setattr(task_bridge, "SessionLocal", lambda: FakeA2ADb())
    monkeypatch.setattr(task_bridge, "MemoryService", FakeA2AMemoryService)
    monkeypatch.setattr(
        task_bridge.uuid, "uuid4", lambda: uuid_module.UUID(A2A_TASK_ID)
    )

    with TestClient(api.app) as client:
        yield client
    api.app.dependency_overrides.clear()
