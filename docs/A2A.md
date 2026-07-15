# Forge A2A adapter

Forge exposes a scoped Agent2Agent endpoint at `/a2a` and a public Agent Card
at `/.well-known/agent-card.json`.

Every task request must include `metadata.project_id`. The project must already
exist in Forge and its workspace must not be a forbidden Forge directory. Forge
does not create workspaces from arbitrary remote context identifiers.

Supported JSON-RPC operations:

- `message/send` creates a Forge goal and returns a task in `submitted` state.
- `message/stream` creates a task and emits state updates as SSE.
- `tasks/get` returns the durable task state.
- `tasks/cancel` signals the associated detached run and marks the task canceled.

All RPC operations require the Forge bearer token. Agent Card discovery is
public so clients can discover the endpoint and security scheme.

Example:

```sh
curl http://127.0.0.1:8787/.well-known/agent-card.json
curl -H "Authorization: Bearer $FORGE_CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"metadata":{"project_id":"PROJECT_ID"},"message":{"parts":[{"text":"Run the test suite and fix the failing parser."}]}}}' \
  http://127.0.0.1:8787/a2a
```

Task records are stored in `$FORGE_HOME/a2a_tasks.json` with atomic writes and
mode `0600`. They map one A2A task to one Forge goal and project.
