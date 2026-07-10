"""Thin client the Slack bridge uses to talk to Hermes.

This is the *single integration seam*: the bridge never imports engine/db
internals directly, it only goes through :class:`HermesClient`. Today that seam
is the Forge **control-plane HTTP API** (the FastAPI app served by
``forge serve`` on port 8787), which already exposes everything the bridge
needs:

* ``POST /runtime/runs/start``  — create a durable goal and launch a detached
  PGE run; returns the ``run_id`` and ``project_id``.
* ``GET  /runs/{run_id}/events`` — poll lifecycle/progress events with an
  ``after`` cursor.
* ``GET  /runtime/runs``        — runtime snapshots (status, node, progress).
* ``POST /runtime/runs/{project_id}/stop`` — request a stop.

Choosing the HTTP seam (over importing ``pge_launcher``/``MemoryService`` in
process) keeps the bridge decoupled from the engine internals and lets it drive
a control plane running in a separate process — exactly the "drive my local
Hermes from Slack" use case. To swap to an in-process implementation, replace
this class with one exposing the same methods.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class HermesClientError(RuntimeError):
    """Raised when the control plane returns an error or is unreachable."""


@dataclass(slots=True)
class StartedRun:
    """A run created from a Slack message."""

    run_id: str
    project_id: str
    raw: dict[str, Any]


class HermesClient:
    """Synchronous httpx client for the Forge control-plane API.

    All calls send the bearer ``FORGE_CONTROL_TOKEN`` the control plane requires.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HermesClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # network / connection / timeout
            raise HermesClientError(
                f"could not reach Forge control plane at {self._base_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise HermesClientError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return None
        return response.json()

    # -- operations the bridge uses ---------------------------------------

    def health(self) -> bool:
        """Return ``True`` if the control plane answers ``/health``."""
        try:
            body = self._request("GET", "/health")
        except HermesClientError:
            return False
        return bool(body and body.get("status") == "ok")

    def start_goal(
        self,
        goal: str,
        *,
        description: str = "",
        project_id: str | None = None,
    ) -> StartedRun:
        """Create a durable goal and launch its detached PGE supervisor.

        Mirrors ``POST /runtime/runs/start`` (schema ``RuntimeRunStart``): the
        goal text must be 3-500 chars; the description is optional.
        """
        payload: dict[str, Any] = {"goal": goal.strip(), "description": description.strip()}
        if project_id:
            payload["project_id"] = project_id
        body = self._request("POST", "/runtime/runs/start", json=payload) or {}
        run_id = body.get("run_id")
        if not run_id:
            raise HermesClientError(f"control plane did not return a run_id: {body}")
        resolved_project_id = body.get("project_id") or project_id
        if not resolved_project_id:
            raise HermesClientError(f"control plane did not return a project_id: {body}")
        return StartedRun(run_id=run_id, project_id=resolved_project_id, raw=body)

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return run events with ``sequence > after`` (ascending)."""
        return self._request("GET", f"/runs/{run_id}/events", params={"after": after}) or []

    def runtime_runs(self) -> list[dict[str, Any]]:
        """Return all runtime snapshots (status, current node, progress)."""
        return self._request("GET", "/runtime/runs") or []

    def runtime_run(self, run_id: str) -> dict[str, Any] | None:
        """Return the runtime snapshot for a single run, if present."""
        for snapshot in self.runtime_runs():
            if snapshot.get("id") == run_id:
                return snapshot
        return None

    def stop(self, project_id: str) -> dict[str, Any]:
        """Request a stop for a project's detached run."""
        return self._request("POST", f"/runtime/runs/{project_id}/stop") or {}
