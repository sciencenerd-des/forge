# Slack bridge for Hermes/Forge (`#hermesbot`)

Drive your **local** Hermes/Forge agent from a Slack channel. You create a
`#hermesbot` channel, type a goal, and the bridge starts a Hermes run and posts
its progress back as threaded replies. Each Slack thread is one Hermes
conversation for deterministic controls like `status` and `stop`. Free-text
follow-ups are intentionally not routed yet; Forge needs a real follow-up API
before the Slack bridge can safely claim that behavior.

It uses **Slack Bolt in Socket Mode**, which keeps an *outbound* WebSocket from
your laptop to Slack. That means **no public URL and no ngrok** — which is
exactly why it can drive the Hermes control plane running on your MacBook.

```
Slack #hermesbot  ──(Socket Mode WebSocket)──>  forge slack (your Mac)
                                                     │ httpx
                                                     ▼
                                          Forge control plane (forge serve, :8787)
                                                     │
                                                     ▼
                                          Hermes PGE autonomy run
```

---

## 0. Prerequisites

* Forge installed and runnable on your Mac (`forge --help` works).
* The Forge **control plane** running locally: `forge serve` (defaults to
  `http://127.0.0.1:8787`). The bridge talks to it over HTTP.
* Install the Slack extra:

  ```bash
  pip install -e '.[slack]'
  ```

---

## 1. Create the Slack app (Socket Mode)

1. Go to <https://api.slack.com/apps> and click **Create New App → From scratch**.
2. Name it `hermesbot` (or anything) and pick the **promptgenerator** workspace.
3. In the left sidebar open **Socket Mode** and toggle **Enable Socket Mode**.
   When prompted, generate an **App-Level Token**:
   * Token Name: `socket`
   * Scope: **`connections:write`**
   * Click **Generate** and copy the token — it starts with **`xapp-`**. This is
     your `SLACK_APP_TOKEN`.

---

## 2. Bot OAuth scopes

Open **OAuth & Permissions → Scopes → Bot Token Scopes** and add exactly these
(the bridge reads channel messages, posts threaded replies, and resolves the
channel id):

| Scope               | Why                                                        |
| ------------------- | ---------------------------------------------------------- |
| `app_mentions:read` | receive `@hermesbot` mentions                              |
| `channels:history`  | read messages in the public `#hermesbot` channel          |
| `channels:read`     | resolve the `#hermesbot` channel name → id                |
| `groups:history`    | same, if you make `#hermesbot` a **private** channel      |
| `chat:write`        | post acknowledgements and progress as threaded replies    |

> The bridge does **not** use `im:history` or `reactions:write` in this version,
> so you can leave those out. Add them later only if you extend the bridge to
> DMs or emoji reactions.

---

## 3. Event Subscriptions

Open **Event Subscriptions** and toggle **Enable Events**. (With Socket Mode on
there is **no Request URL to verify** — that is the whole point.) Under
**Subscribe to bot events** add:

| Event              | Why                                            |
| ------------------ | ---------------------------------------------- |
| `message.channels` | new messages / thread replies in the channel   |
| `app_mention`      | `@hermesbot` mentions                          |

> If you use a **private** channel, also add `message.groups`.

Click **Save Changes**.

---

## 4. Install the app & create the channel

1. Open **OAuth & Permissions** and click **Install to promptgenerator**.
   Authorize it. Copy the **Bot User OAuth Token** — it starts with **`xoxb-`**.
   This is your `SLACK_BOT_TOKEN`.
2. In Slack, create the channel **`#hermesbot`** (`+ → Create a channel`).
3. Invite the bot to the channel:

   ```
   /invite @hermesbot
   ```

---

## 5. Set environment variables

Copy `.env.example` to `.env` (if you haven't) and fill in:

```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...          # from step 4
SLACK_APP_TOKEN=xapp-...          # from step 1 (connections:write)
SLACK_HERMES_CHANNEL=hermesbot    # channel name without '#'
SLACK_HERMES_CHANNEL_ID=C...      # optional explicit channel id
SLACK_ALLOWED_USER_IDS=U...,U...  # optional command allowlist

# Control plane the bridge calls (already in .env.example)
FORGE_CONTROL_URL=http://127.0.0.1:8787
FORGE_CONTROL_TOKEN=forge-local-control   # must match the running `forge serve`
```

The bridge needs `FORGE_CONTROL_TOKEN` and `SLACK_APP_TOKEN`/`SLACK_BOT_TOKEN`
or it will refuse to start with a clear error. It also refuses to run unless it
can resolve the configured Slack channel name to an id. If Slack channel lookup
is unavailable in your workspace, set `SLACK_HERMES_CHANNEL_ID` explicitly.

`SLACK_ALLOWED_USER_IDS` is optional. When set, only those Slack user ids can
start or stop local Forge runs from the channel.

---

## 6. Run it on your Mac

In one terminal, start the control plane:

```bash
forge serve
```

In another, start the bridge:

```bash
forge slack
```

You should see `starting Slack Socket Mode bridge for #hermesbot`. **No public
URL or ngrok is required** — Socket Mode dials out to Slack, which is what lets
it drive your *local* Hermes.

---

## 7. Using it

* **New goal** — post a top-level message in `#hermesbot`:

  > Add a `/healthz` endpoint and tests

  The bot replies in a thread acknowledging the run, then streams progress
  (launching → started → finished/failed) as threaded replies.

* **Control words** (type them as a thread reply):
  * `status` — report the current run state (status, node, batch, task progress).
  * `stop` / `cancel` — request the run to stop.

* **Free-text thread replies** — not supported yet. The bridge will post a
  clear message instead of silently creating another durable goal.

---

## Notes & limitations

* The `thread_ts → run` mapping is persisted to a small JSON file under
  `FORGE_HOME` (`slack_threads.json`), so thread continuity survives a restart.
  It is intentionally a flat file; promote it to a DB table for multi-process or
  high-volume use.
* The bridge is fail-closed by Slack channel id. If it cannot resolve the
  configured channel, it will not start.
* Progress streaming reflects **control-plane lifecycle events**
  (`run.launching/starting/finished/stopped/failed`). Finer-grained
  plan/audit/exec/eval narration appears if/when the engine emits those events
  to the control plane.
* The bridge never crashes on a Slack or control-plane error — it posts the
  error into the thread and keeps listening.
