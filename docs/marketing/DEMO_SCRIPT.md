# 90-second demo

## Setup

- Clean Docker environment.
- A small public fixture repository with one failing test.
- Local model endpoint already running.
- Terminal and Forge console side by side.

## Script

**0-10s - Problem**

Voice: "Coding agents can write code, but long runs fail when state disappears or the model declares victory without proof."

Visual: failing fixture test, then Forge console idle state.

**10-25s - Launch**

Run:

```bash
docker compose up -d
docker compose run --rm forge run --goal "Fix the parser bug and make the tests pass"
```

Voice: "Forge checkpoints the goal and routes it through a deterministic four-node loop."

**25-45s - Observe**

Visual: Planner, Auditor, Executor, Evaluator states; task and evidence counts.

Voice: "The auditor creates the acceptance contract. The executor takes one bounded action. Every transition is durable and inspectable."

**45-60s - Failure recovery**

Stop the worker container, restart it, and show the same run resuming.

Voice: "Kill the worker and Forge reloads the run from Postgres instead of reconstructing state from chat history."

**60-80s - Independent verification**

Visual: evaluator executes the test list; test output changes from failing to passing.

Voice: "The evaluator runs the tests itself. The model cannot mark the goal complete by saying it looks done."

**80-90s - Close**

Visual: completed state and GitHub URL.

Voice: "Forge is open source, works with local or cloud OpenAI-compatible models, and keeps untrusted execution inside your container boundary."

## Proof to keep visible

- Same run ID before and after restart.
- Real command output and exit status.
- Changed file count and test count.
- Model and hardware in the video description.
