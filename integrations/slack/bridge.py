"""Slack Bolt (Socket Mode) bridge for the ``#hermesbot`` channel.

Run it with ``forge slack``. Because it uses Socket Mode, the laptop keeps an
outbound WebSocket to Slack and needs no public URL/ngrok — which is exactly why
it can drive the *local* Hermes/Forge control plane.

Behaviour
---------
* A new **top-level** message in the configured channel is treated as a Hermes
  goal: the bridge starts a run (via the control plane) and acknowledges in a
  thread anchored on that message's ``ts``.
* Each Slack thread (``thread_ts``) maps to one Hermes run. Thread replies are
  currently limited to deterministic control words until Forge exposes a real
  follow-up API.
* Meaningful run progress (launch / start / finish / failure) is streamed back
  as threaded replies by a background poller.
* In-thread control words: ``status`` and ``stop`` / ``cancel``.

The Slack <-> Hermes seam is :class:`integrations.slack.client.HermesClient`;
this module contains only Slack glue.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from .client import HermesClient

logger = logging.getLogger("forge.slack")

# Control words a user can type as a thread reply.
_STATUS_WORDS = {"status", "?"}
_STOP_WORDS = {"stop", "cancel", "abort"}


@dataclass(frozen=True, slots=True)
class SlackAccessPolicy:
    """Deterministic authorization boundary for Slack-originated commands."""

    channel_id: str
    allowed_user_ids: frozenset[str] = frozenset()

    def allows(self, channel: str, user_id: str | None) -> bool:
        if channel != self.channel_id:
            return False
        if not self.allowed_user_ids:
            return True
        return bool(user_id and user_id in self.allowed_user_ids)


class ThreadMap:
    """Persistent ``thread_ts -> {run_id, project_id}`` mapping.

    Backed by a small JSON file under ``FORGE_HOME`` so thread continuity
    survives a bridge restart. Reads/writes are guarded by a lock since the
    Slack listener and the progress poller touch it from different threads.

    NOTE: this is intentionally a simple flat file. For multi-process or
    high-volume use, promote it to a real table (a ``HermesSlackThread`` model).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:  # persistence is best-effort; never crash on it
            logger.warning("could not persist Slack thread map: %s", exc)

    def get(self, thread_ts: str) -> dict[str, str] | None:
        with self._lock:
            return self._data.get(thread_ts)

    def put(self, thread_ts: str, run_id: str, project_id: str) -> None:
        with self._lock:
            self._data[thread_ts] = {"run_id": run_id, "project_id": project_id}
            self._flush()

    def items(self) -> list[tuple[str, dict[str, str]]]:
        with self._lock:
            return list(self._data.items())


def _format_event(event: dict[str, Any]) -> str | None:
    """Render a control-plane run event as a short Slack line, or ``None`` to skip.

    Control-plane lifecycle events are ``run.<status>`` (see
    ``pge_launcher._persist_lifecycle``); we surface the interesting ones and
    enrich them with the payload's batch / terminal reason when present.
    """
    event_type = str(event.get("event_type", ""))
    payload = event.get("payload") or {}
    batch = payload.get("batch")
    reason = payload.get("terminal_reason") or payload.get("failure")

    labels = {
        "run.launching": "Launching the autonomy supervisor...",
        "run.starting": "Run started.",
        "run.running": "Working...",
        "run.finished": "Run finished.",
        "run.stopped": "Run stopped.",
        "run.failed": "Run failed.",
    }
    text = labels.get(event_type)
    if text is None:
        # Surface engine-side node/task events generically if they ever appear.
        if event_type and event_type != "run.launching":
            text = event_type.replace("_", " ").replace(".", " ").strip().capitalize()
        else:
            return None
    extra = []
    if isinstance(batch, int) and batch:
        extra.append(f"batch {batch}")
    if reason:
        extra.append(str(reason))
    if extra:
        text = f"{text} ({', '.join(extra)})"
    return f":robot_face: {text}"


def _status_text(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return ":mag: No live snapshot yet — the run may still be launching."
    parts = [
        f"*status*: {snapshot.get('status', 'unknown')}",
        f"*node*: {snapshot.get('current_node', 'waiting')}",
        f"*batch*: {snapshot.get('batch', 0)}",
        f"*tasks*: {snapshot.get('task_completed', 0)}/{snapshot.get('task_total', 0)}",
    ]
    active = snapshot.get("active_task")
    if active:
        parts.append(f"*active*: {active}")
    return ":mag: " + "  ".join(parts)


def handle_slack_text(
    client: HermesClient,
    thread_map: ThreadMap,
    policy: SlackAccessPolicy,
    text: str,
    channel: str,
    user_id: str | None,
    thread_ts: str,
    message_ts: str,
    say: Callable[..., Any],
    start_poller: Callable[[str, str, Callable[..., Any]], None] | None = None,
) -> None:
    """Route one Slack text event through the bridge's deterministic policy."""
    if channel != policy.channel_id:
        return
    text = (text or "").strip()
    if not text:
        return
    if not policy.allows(channel, user_id):
        _reply(
            say,
            thread_ts or message_ts,
            ":no_entry: This Slack user is not allowed to run Forge.",
        )
        return

    mapping = thread_map.get(thread_ts)
    lowered = text.lower()
    if mapping and lowered in _STATUS_WORDS:
        snapshot = client.runtime_run(mapping["run_id"])
        _reply(say, thread_ts, _status_text(snapshot))
        return
    if mapping and lowered in _STOP_WORDS:
        try:
            client.stop(mapping["project_id"])
            _reply(say, thread_ts, ":octagonal_sign: Stop requested.")
        except Exception as exc:  # noqa: BLE001
            _reply(say, thread_ts, f":warning: Could not stop: {exc}")
        return

    if mapping is None:
        try:
            run = client.start_goal(text, description=f"Slack thread {thread_ts}")
        except Exception as exc:  # noqa: BLE001
            _reply(say, message_ts, f":warning: Could not start Hermes run: {exc}")
            return
        thread_map.put(thread_ts, run.run_id, run.project_id)
        _reply(
            say, thread_ts,
            f":white_check_mark: Got it — started Hermes run `{run.run_id[:8]}`.\n"
            f"> {text}\nThread controls available: `status` / `stop`.",
        )
        if start_poller is not None:
            start_poller(run.run_id, thread_ts, say)
        return

    _reply(
        say, thread_ts,
        ":information_source: This bridge does not yet support free-text follow-ups. "
        "Use `status` or `stop`, or start a new top-level message for a new goal.",
    )


def _reply(say: Callable[..., Any], thread_ts: str, text: str) -> None:
    try:
        say(text=text, thread_ts=thread_ts)
    except Exception as exc:  # noqa: BLE001 - never crash the listener
        logger.error("failed to post to thread %s: %s", thread_ts, exc)


def build_app(
    client: HermesClient,
    channel_name: str,
    thread_map: ThreadMap,
    *,
    channel_id: str | None = None,
    allowed_user_ids: set[str] | frozenset[str] | None = None,
):
    """Construct and return the configured Slack Bolt ``App``.

    The heavy ``slack_bolt`` import lives here so importing this module (e.g. for
    an AST/syntax check, or to wire the CLI) never requires Slack to be
    installed.
    """
    from slack_bolt import App

    app = App(token=_require_env("SLACK_BOT_TOKEN"))

    # Resolve our own bot user id once so the message handler can ignore
    # @-mentions (those are delivered separately as app_mention events; handling
    # them in both places would start/route every mentioned goal twice).
    bot_user_id = _resolve_bot_user_id(app)

    # Resolve the channel name to its id once; messages carry only the id. This
    # is fail-closed because Slack can deliver events from every subscribed
    # channel the bot can see.
    resolved_channel_id = channel_id or _resolve_channel_id(app, channel_name)
    if not resolved_channel_id:
        raise RuntimeError(
            f"Slack channel #{channel_name.lstrip('#')} could not be resolved. "
            "Create/invite the bot first, or set SLACK_HERMES_CHANNEL_ID explicitly."
        )
    policy = SlackAccessPolicy(
        channel_id=resolved_channel_id,
        allowed_user_ids=frozenset(allowed_user_ids or ()),
    )
    logger.info("watching channel #%s (%s)", channel_name, policy.channel_id)

    pollers: dict[str, threading.Thread] = {}
    pollers_lock = threading.Lock()

    def _start_poller(run_id: str, thread_ts: str, say: Callable[..., Any]) -> None:
        # Guard the registry: the Slack listener (multiple Bolt worker threads)
        # and the poller-cleanup callback all touch it concurrently.
        with pollers_lock:
            if run_id in pollers:
                return
            thread = threading.Thread(
                target=_poll_run,
                # Drop ourselves from the registry on exit so the dict can't grow
                # without bound and a future run with the same id can re-poll.
                args=(client, run_id, thread_ts, say, lambda: _drop_poller(run_id)),
                name=f"hermes-poll-{run_id[:8]}",
                daemon=True,
            )
            pollers[run_id] = thread
            thread.start()

    def _drop_poller(run_id: str) -> None:
        with pollers_lock:
            pollers.pop(run_id, None)

    @app.event("message")
    def on_message(event: dict[str, Any], say: Callable[..., Any]) -> None:
        # Ignore edits, deletes, bot echoes, and join notices.
        if event.get("subtype") or event.get("bot_id"):
            return
        # Ignore our own user's messages (guards against an echo loop even if a
        # post somehow arrives without bot_id, e.g. via a user token).
        if bot_user_id and event.get("user") == bot_user_id:
            return
        text = event.get("text", "") or ""
        # Messages that @-mention us are also delivered as app_mention events;
        # let that handler own them to avoid processing the goal twice.
        if bot_user_id and f"<@{bot_user_id}>" in text:
            return
        message_ts = event.get("ts", "")
        # Thread replies carry thread_ts; top-level messages anchor on their ts.
        thread_ts = event.get("thread_ts") or message_ts
        try:
            handle_slack_text(
                client, thread_map, policy, text, event.get("channel", ""),
                event.get("user"), thread_ts, message_ts, say, _start_poller,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            logger.exception("message handler error")
            _reply(say, thread_ts, f":warning: Internal error: {exc}")

    @app.event("app_mention")
    def on_app_mention(event: dict[str, Any], say: Callable[..., Any]) -> None:
        message_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or message_ts
        # Strip the leading <@BOTID> mention so the goal text is clean.
        text = _strip_mention(event.get("text", ""))
        try:
            handle_slack_text(
                client, thread_map, policy, text, event.get("channel", ""),
                event.get("user"), thread_ts, message_ts, say, _start_poller,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("app_mention handler error")
            _reply(say, thread_ts, f":warning: Internal error: {exc}")

    return app


def _poll_run(client: HermesClient, run_id: str, thread_ts: str,
              say: Callable[..., Any],
              on_exit: Callable[[], None] | None = None) -> None:
    """Background loop: stream new run events into the Slack thread.

    Runs as a daemon thread; exits when a terminal event is seen. Errors are
    logged but never propagated, so a flaky control plane can't kill the bridge.
    ``on_exit`` is invoked exactly once when the loop ends (terminal event,
    give-up, or unexpected error) so the caller can drop us from its registry.
    """
    import time

    cursor = 0
    terminal = {"run.finished", "run.stopped", "run.failed"}
    idle = 0
    try:
        while True:
            try:
                events = client.events(run_id, after=cursor)
            except Exception as exc:  # noqa: BLE001
                logger.debug("poll error for %s: %s", run_id, exc)
                time.sleep(3)
                idle += 1
                if idle > 200:  # ~10 min of failures -> give up quietly
                    return
                continue
            idle = 0
            for event in events:
                cursor = max(cursor, int(event.get("sequence", cursor)))
                line = _format_event(event)
                if line:
                    try:
                        say(text=line, thread_ts=thread_ts)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("failed to stream event to %s: %s", thread_ts, exc)
                if str(event.get("event_type")) in terminal:
                    return
            time.sleep(2)
    finally:
        if on_exit is not None:
            try:
                on_exit()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                logger.debug("poller cleanup failed for %s", run_id)


def _strip_mention(text: str) -> str:
    """Remove a leading ``<@U…>`` bot mention from app_mention text."""
    import re

    return re.sub(r"^\s*<@[^>]+>\s*", "", text or "").strip()


def _resolve_bot_user_id(app: Any) -> str | None:
    """Return this app's own bot user id via ``auth.test`` (best effort)."""
    try:
        return app.client.auth_test().get("user_id")
    except Exception as exc:  # noqa: BLE001 - non-fatal; bot_id filter still applies
        logger.warning("could not resolve bot user id: %s", exc)
        return None


def _resolve_channel_id(app: Any, channel_name: str) -> str | None:
    """Look up a channel id by name via ``conversations.list`` (best effort)."""
    name = channel_name.lstrip("#")
    try:
        cursor = None
        while True:
            resp = app.client.conversations_list(
                types="public_channel,private_channel", limit=200, cursor=cursor
            )
            for channel in resp.get("channels", []):
                if channel.get("name") == name:
                    return channel.get("id")
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None
    except Exception as exc:  # noqa: BLE001 - non-fatal; we match on name later
        logger.warning("could not resolve channel #%s: %s", name, exc)
        return None


def _require_env(name: str) -> str:
    import os

    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill in the Slack "
            f"tokens (see integrations/slack/README.md)."
        )
    return value


def run() -> int:
    """Entry point used by ``forge slack``.

    Boots the Hermes client + Slack app and blocks in Socket Mode. All heavy
    imports are local so the module imports cleanly without ``slack_bolt``.
    """
    import os

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    import forge_config

    from .client import HermesClient

    logging.basicConfig(
        level=os.getenv("FORGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    channel_name = forge_config.slack_channel()
    control_url = forge_config.control_url()
    control_token = forge_config.control_token() or _require_env("FORGE_CONTROL_TOKEN")
    app_token = _require_env("SLACK_APP_TOKEN")

    client = HermesClient(control_url, control_token)
    if not client.health():
        logger.warning(
            "control plane at %s is not answering /health — start it with "
            "`forge serve` (the bridge will keep retrying per message)", control_url
        )

    thread_map = ThreadMap(forge_config.home() / "slack_threads.json")
    app = build_app(
        client,
        channel_name,
        thread_map,
        channel_id=forge_config.slack_channel_id(),
        allowed_user_ids=forge_config.slack_allowed_user_ids(),
    )

    logger.info("starting Slack Socket Mode bridge for #%s", channel_name)
    handler = SocketModeHandler(app, app_token)
    handler.start()  # blocks
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
