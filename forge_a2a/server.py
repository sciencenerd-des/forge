from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from control_plane.api import require_control_token

from .agent_card import agent_card
from .task_bridge import cancel, create_task, task

router = APIRouter()


@router.get("/.well-known/agent-card.json")
def card() -> dict[str, Any]:
    return agent_card()


@router.post("/a2a", dependencies=[Depends(require_control_token)])
async def rpc(body: dict[str, Any]):
    if body.get("jsonrpc") != "2.0" or not isinstance(body.get("id"), (str, int)):
        return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
    method = body.get("method")
    try:
        if method == "message/send": result = create_task(body.get("params") or {})
        elif method == "tasks/get": result = task((body.get("params") or {}).get("id", ""))
        elif method == "tasks/cancel": result = cancel((body.get("params") or {}).get("id", ""))
        elif method == "message/stream":
            created = create_task(body.get("params") or {})
            return StreamingResponse(_stream(created["id"]), media_type="text/event-stream")
        else: return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}}
    except KeyError:
        return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32001, "message": "task not found"}}
    except (ValueError, RuntimeError) as exc:
        return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


async def _stream(task_id: str | None):
    if not task_id:
        yield f"data: {json.dumps({'error': 'task id is required'})}\n\n"
        return
    last = None
    for _ in range(120):
        try: current = task(task_id)
        except KeyError:
            yield f"data: {json.dumps({'error': 'task not found'})}\n\n"
            return
        state = current["status"]["state"]
        if state != last:
            yield f"data: {json.dumps(current)}\n\n"
            last = state
        if state in {"completed", "failed", "canceled"}: return
        await asyncio.sleep(0.5)

