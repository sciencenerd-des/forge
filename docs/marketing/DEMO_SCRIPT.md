# 90-second reproducible verification demo

This version needs Python 3.11+, Bash, and `tail`, with no credentials or services.
It demonstrates one real production helper, not a complete autonomous run.

1. **0–15s:** Explain the problem: formatting a failed check's output can hide
   its failure status from the next component.
2. **15–40s:** Run `python3 examples/verification_demo.py` from the checkout.
   Show the broken producer returning exit 0 before hardening and exit 1 after.
3. **40–65s:** Open `harden_pipefail` in `engine/src/auditor.py`. Explain the
   Bash prefix and point to the positive control that still passes.
4. **65–90s:** Show `docs/CODEX_WALKTHROUGH.md`. Invite viewers to use Codex to
   inspect the helper and reason about a fallback that pipefail cannot fix.

Keep the commit SHA, terminal output, and scope statement in the recording.
Do not substitute a console mockup for evidence of a live run.

## Optional full-system recording (requires separate validation)

The outline below is a recording plan, not evidence that the flow has passed.
Follow the current README quickstart, run Forge on the host with the default
container sandbox, and verify each step before recording it. The Compose API
service deliberately lacks Docker-daemon access, so launching container-mode
execution inside that service fails closed.

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
make db
make sandbox-image
.venv/bin/forge run --goal "Fix the parser bug and make the tests pass"
```

Voice: "Forge checkpoints the goal and routes it through a deterministic four-node loop."

**25-45s - Observe**

Visual: Planner, Auditor, Executor, Evaluator states; task and evidence counts.

Voice: "The auditor creates the acceptance contract. The executor takes one bounded action. Every transition is durable and inspectable."

**45-60s - Failure recovery**

If you demonstrate restart recovery, record the exact supported launcher and
resume commands for this revision and show the same run ID after recovery.
Do not claim that restarting a fresh command resumes a run without verifying it.

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
