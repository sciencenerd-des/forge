from __future__ import annotations

import asyncio
import json
import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import SessionLocal, create_schema
from .models import ApprovalRecord, ExternalActionRecord, RunEventRecord, RunRecord
from .schemas import (
    ApprovalCreate,
    ApprovalDecision,
    BrowserActionCreate,
    BrowserSessionCreate,
    EventCreate,
    ExternalActionCreate,
    LeaseClaim,
    LeaseHeartbeat,
    RunCreate,
    RuntimeRunStart,
)
from .service import (
    ConflictError,
    NotFoundError,
    append_event,
    claim_run,
    create_browser_session,
    create_run,
    decide_approval,
    heartbeat_run,
    queue_browser_action,
    queue_external_action,
    request_approval,
    serialize_model,
)
from .workers import execute_external_action, run_browser_worker_once
from .runtime_snapshot import (
    get_config_snapshot,
    get_system_snapshot,
    list_project_snapshots,
    list_runtime_snapshots,
)


app = FastAPI(title="Forge Control Plane", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    create_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_control_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("FORGE_CONTROL_TOKEN")
    if not expected:
        raise HTTPException(503, "FORGE_CONTROL_TOKEN is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid control-plane credential", headers={"WWW-Authenticate": "Bearer"})


def translate_errors(error: Exception) -> HTTPException:
    if isinstance(error, NotFoundError):
        return HTTPException(404, str(error))
    if isinstance(error, ConflictError):
        return HTTPException(409, str(error))
    return HTTPException(400, str(error))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/runtime/runs", dependencies=[Depends(require_control_token)])
def runtime_runs() -> list[dict]:
    """Read-only loopback dashboard feed for live PGE runs."""
    return list_runtime_snapshots()


@app.get("/runtime/system", dependencies=[Depends(require_control_token)])
def runtime_system() -> dict:
    """Read-only health for the local gateway and PGE workers."""
    return get_system_snapshot()


@app.get("/runtime/config", dependencies=[Depends(require_control_token)])
def runtime_config() -> dict:
    """Resolved Forge configuration (provider, paths, db) — no secrets."""
    return get_config_snapshot()


@app.get("/runtime/projects", dependencies=[Depends(require_control_token)])
def runtime_projects() -> list[dict]:
    """Projects with goal + task progress for the operator console."""
    return list_project_snapshots()


@app.post("/runtime/runs/{project_id}/stop", dependencies=[Depends(require_control_token)])
def runtime_stop(project_id: str) -> dict:
    """Stop a detached run: signal its process and mark the manifest stopped.
    Loopback dashboard control (the console runs on localhost)."""
    import signal
    from pge_launcher import load_run_state, process_is_alive, update_run

    manifest = load_run_state().get(project_id)
    if not manifest:
        raise HTTPException(404, "no run for that project")
    pid = manifest.get("pid")
    run_id = manifest.get("run_id")
    if pid and process_is_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            raise HTTPException(409, f"could not signal pid {pid}: {exc}")
    if run_id:
        update_run(project_id, run_id, status="stopped", terminal_reason="stopped_by_operator")
    return {"status": "stopped", "project_id": project_id, "pid": pid}


@app.post("/runtime/runs/start", status_code=201, dependencies=[Depends(require_control_token)])
def runtime_start(body: RuntimeRunStart) -> dict:
    """Create a durable goal and launch its detached PGE supervisor."""
    import forge_config
    from app.database import SessionLocal as PgeSession
    from app.services import MemoryService
    from pge_launcher import launch_pge

    with PgeSession() as db:
        project_id = body.project_id or forge_config.ensure_default_project(db)
        MemoryService(db).create_goal(
            project_id=project_id,
            title=body.goal.strip(),
            description=body.description.strip(),
        )
    result = launch_pge(project_id, source="control-plane:web", invocation={"goal": body.goal})
    if result.get("status") != "success":
        raise HTTPException(409, result.get("message", "run failed to start"))
    return result


@app.post("/runs", status_code=201, dependencies=[Depends(require_control_token)])
def runs_create(body: RunCreate, db: Session = Depends(get_db)) -> dict:
    return serialize_model(create_run(db, body.project_id, body.goal_id, body.provider_id, body.max_turns))


@app.get("/runs", dependencies=[Depends(require_control_token)])
def runs_list(db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_model(run) for run in db.scalars(select(RunRecord).order_by(RunRecord.created_at.desc())).all()]


@app.get("/runs/{run_id}", dependencies=[Depends(require_control_token)])
def runs_get(run_id: str, db: Session = Depends(get_db)) -> dict:
    run = db.get(RunRecord, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return serialize_model(run)


@app.post("/runs/claim", dependencies=[Depends(require_control_token)])
def runs_claim(body: LeaseClaim, db: Session = Depends(get_db)) -> dict | None:
    run = claim_run(db, body.worker_id, body.lease_seconds)
    return serialize_model(run) if run else None


@app.post("/runs/{run_id}/heartbeat", dependencies=[Depends(require_control_token)])
def runs_heartbeat(run_id: str, body: LeaseHeartbeat, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(heartbeat_run(db, run_id, body.worker_id, body.lease_token, body.lease_seconds))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/runs/{run_id}/events", status_code=201, dependencies=[Depends(require_control_token)])
def events_create(run_id: str, body: EventCreate, db: Session = Depends(get_db)) -> dict:
    try:
        event = append_event(db, run_id, body.event_type, body.actor, body.payload)
        db.commit()
        return serialize_model(event)
    except Exception as error:
        raise translate_errors(error) from error


@app.get("/runs/{run_id}/events", dependencies=[Depends(require_control_token)])
def events_list(run_id: str, after: int = Query(0, ge=0), db: Session = Depends(get_db)) -> list[dict]:
    events = db.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.sequence > after).order_by(RunEventRecord.sequence)).all()
    return [serialize_model(event) for event in events]


@app.get("/runs/{run_id}/events/stream", dependencies=[Depends(require_control_token)])
async def events_stream(run_id: str, after: int = Query(0, ge=0)) -> StreamingResponse:
    async def generate():
        cursor = after
        while True:
            with SessionLocal() as db:
                events = db.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run_id, RunEventRecord.sequence > cursor).order_by(RunEventRecord.sequence)).all()
                for event in events:
                    cursor = event.sequence
                    yield f"id: {cursor}\nevent: {event.event_type}\ndata: {json.dumps(serialize_model(event), default=str)}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/approvals", status_code=201, dependencies=[Depends(require_control_token)])
def approvals_create(body: ApprovalCreate, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(request_approval(db, **body.model_dump()))
    except Exception as error:
        raise translate_errors(error) from error


@app.get("/approvals", dependencies=[Depends(require_control_token)])
def approvals_list(status: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    statement = select(ApprovalRecord).order_by(ApprovalRecord.created_at.desc())
    if status:
        statement = statement.where(ApprovalRecord.status == status)
    return [serialize_model(item) for item in db.scalars(statement).all()]


@app.post("/approvals/{approval_id}/decision", dependencies=[Depends(require_control_token)])
def approvals_decide(approval_id: str, body: ApprovalDecision, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(decide_approval(db, approval_id, body.actor, body.approved, body.reason))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/external-actions", status_code=201, dependencies=[Depends(require_control_token)])
def external_actions_create(body: ExternalActionCreate, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(queue_external_action(db, **body.model_dump()))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/external-actions/{action_id}/execute", dependencies=[Depends(require_control_token)])
def external_actions_execute(action_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(execute_external_action(db, action_id))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/browser/sessions", status_code=201, dependencies=[Depends(require_control_token)])
def browser_sessions_create(body: BrowserSessionCreate, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(create_browser_session(db, body.run_id, body.allowed_hosts))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/browser/sessions/{session_id}/actions", status_code=201, dependencies=[Depends(require_control_token)])
def browser_actions_create(session_id: str, body: BrowserActionCreate, db: Session = Depends(get_db)) -> dict:
    try:
        return serialize_model(queue_browser_action(db, session_id, body.action, body.arguments, body.approval_id))
    except Exception as error:
        raise translate_errors(error) from error


@app.post("/browser/sessions/{session_id}/execute-next", dependencies=[Depends(require_control_token)])
def browser_actions_execute(session_id: str, db: Session = Depends(get_db)) -> dict | None:
    try:
        action = run_browser_worker_once(db, session_id)
        return serialize_model(action) if action else None
    except Exception as error:
        raise translate_errors(error) from error
