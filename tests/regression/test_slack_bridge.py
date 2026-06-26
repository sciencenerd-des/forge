from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from integrations.slack.bridge import (
    SlackAccessPolicy,
    ThreadMap,
    build_app,
    handle_slack_text,
)
from integrations.slack.client import HermesClientError, StartedRun


class FakeHermesClient:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.stopped: list[str] = []

    def start_goal(
        self,
        goal: str,
        *,
        description: str = "",
        project_id: str | None = None,
    ) -> StartedRun:
        self.started.append(
            {"goal": goal, "description": description, "project_id": project_id}
        )
        return StartedRun(run_id="run-12345678", project_id="project-1", raw={})

    def runtime_run(self, run_id: str) -> dict:
        return {
            "id": run_id,
            "status": "running",
            "current_node": "executor",
            "batch": 2,
            "task_completed": 1,
            "task_total": 3,
        }

    def stop(self, project_id: str) -> dict:
        self.stopped.append(project_id)
        return {"status": "stopped"}


def capture_say(messages: list[dict]):
    def say(**kwargs):
        messages.append(kwargs)

    return say


def make_thread_map(tmp_path: Path) -> ThreadMap:
    return ThreadMap(tmp_path / "slack_threads.json")


def test_wrong_channel_is_ignored_without_starting_run(tmp_path):
    client = FakeHermesClient()
    messages: list[dict] = []

    handle_slack_text(
        client,
        make_thread_map(tmp_path),
        SlackAccessPolicy(channel_id="C-HERMES"),
        "Ship it",
        "C-RANDOM",
        "U-ALLOWED",
        "111.222",
        "111.222",
        capture_say(messages),
    )

    assert client.started == []
    assert messages == []


def test_user_allowlist_blocks_unapproved_user(tmp_path):
    client = FakeHermesClient()
    messages: list[dict] = []

    handle_slack_text(
        client,
        make_thread_map(tmp_path),
        SlackAccessPolicy(channel_id="C-HERMES", allowed_user_ids=frozenset({"U-ALLOWED"})),
        "Ship it",
        "C-HERMES",
        "U-DENIED",
        "111.222",
        "111.222",
        capture_say(messages),
    )

    assert client.started == []
    assert messages
    assert "not allowed" in messages[0]["text"]


def test_start_persists_project_id_and_stop_uses_it(tmp_path):
    client = FakeHermesClient()
    thread_map = make_thread_map(tmp_path)
    messages: list[dict] = []

    handle_slack_text(
        client,
        thread_map,
        SlackAccessPolicy(channel_id="C-HERMES"),
        "Add health endpoint",
        "C-HERMES",
        "U-ALLOWED",
        "111.222",
        "111.222",
        capture_say(messages),
    )
    handle_slack_text(
        client,
        thread_map,
        SlackAccessPolicy(channel_id="C-HERMES"),
        "stop",
        "C-HERMES",
        "U-ALLOWED",
        "111.222",
        "111.333",
        capture_say(messages),
    )

    assert thread_map.get("111.222") == {"run_id": "run-12345678", "project_id": "project-1"}
    assert client.stopped == ["project-1"]


def test_free_text_thread_reply_does_not_create_second_goal(tmp_path):
    client = FakeHermesClient()
    thread_map = make_thread_map(tmp_path)
    messages: list[dict] = []
    say = capture_say(messages)
    policy = SlackAccessPolicy(channel_id="C-HERMES")

    handle_slack_text(
        client, thread_map, policy, "Initial goal", "C-HERMES", "U-1", "111.222", "111.222", say
    )
    handle_slack_text(
        client, thread_map, policy, "Actually also do x", "C-HERMES", "U-1", "111.222", "111.333", say
    )

    assert [call["goal"] for call in client.started] == ["Initial goal"]
    assert "does not yet support free-text follow-ups" in messages[-1]["text"]


def test_build_app_fails_closed_when_channel_cannot_be_resolved(monkeypatch, tmp_path):
    class FakeClient:
        def auth_test(self):
            return {"user_id": "U-BOT"}

        def conversations_list(self, **_kwargs):
            return {"channels": [], "response_metadata": {}}

    class FakeApp:
        def __init__(self, token: str) -> None:
            self.client = FakeClient()

        def event(self, _name: str):
            def decorator(func):
                return func

            return decorator

    fake_module = types.ModuleType("slack_bolt")
    fake_module.App = FakeApp
    monkeypatch.setitem(sys.modules, "slack_bolt", fake_module)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    with pytest.raises(RuntimeError, match="could not be resolved"):
        build_app(FakeHermesClient(), "hermesbot", make_thread_map(tmp_path))


def test_client_rejects_start_response_without_project_id():
    from integrations.slack.client import HermesClient

    client = HermesClient("http://forge.local", "token")
    client._request = lambda *_args, **_kwargs: {"run_id": "run-1"}  # type: ignore[method-assign]

    with pytest.raises(HermesClientError, match="project_id"):
        client.start_goal("Build the thing")

    client.close()
