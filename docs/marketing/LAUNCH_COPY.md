# Launch copy

## GitHub description

Reliable, resumable autonomous coding with independent test verification for local or OpenAI-compatible models.

## Hacker News

**Title:** Show HN: Forge - a reliability harness for long-running coding agents

**Post:**

I built Forge after watching small/local coding models fail in repetitive ways: claiming completion without running tests, losing progress after a timeout, and restarting from conversational history instead of durable state.

Forge wraps an OpenAI-compatible model in a deterministic Planner -> Auditor -> Executor -> Evaluator loop. The evaluator independently runs a machine-checkable test contract. State is checkpointed to Postgres, runs survive node failures, and the React control plane exposes progress, evidence, and approvals.

It is local-first and works with LM Studio, Ollama, vLLM, or cloud endpoints. Model-proposed commands are disabled on the host by default; the intended execution path is Docker or another isolated environment.

Repository: https://github.com/sciencenerd-des/forge

I would especially value feedback on the test-contract design, failure recovery model, and what traces would make benchmark comparisons credible.

## X / Twitter

I open-sourced Forge: a reliability harness for long-running coding agents.

- durable Postgres checkpoints
- Planner -> Auditor -> Executor -> Evaluator
- completion verified by tests, not model claims
- local or cloud OpenAI-compatible models
- operator console + approval gates

Run untrusted commands in Docker. Inspect every step.

https://github.com/sciencenerd-des/forge

## LinkedIn

Coding agents are good at producing actions. They are much less reliable at owning state, recovering from failure, and proving that a task is actually complete.

I built and open-sourced Forge to separate those responsibilities. Models propose work inside a deterministic loop; Postgres owns durable state; an independent evaluator executes the acceptance tests; and an operator console exposes evidence, lifecycle, and approvals.

Forge is designed for local and OpenAI-compatible models, including LM Studio, Ollama, and vLLM. It defaults to denying arbitrary host commands, with Docker as the recommended execution boundary.

Repository: https://github.com/sciencenerd-des/forge

## Reddit / community post

**Title:** Open-sourced a durable coding-agent harness built for local models

Forge is the orchestration layer I wanted around local coding models: durable task state, bounded retries, independent test execution, and a console that shows what the agent actually changed and verified.

The core loop is Planner -> Auditor -> Executor -> Evaluator. The auditor creates a checklist and machine-readable test contract. The executor takes bounded actions. The evaluator runs the tests itself and controls completion from exit codes, not model confidence.

It supports OpenAI-compatible endpoints and per-role model routing. Postgres is required because runs are designed to resume instead of starting over. Docker is the recommended boundary for model-proposed commands.

I am looking for testers with local-model setups and real repositories. Please include model, hardware, task, intervention count, and trace evidence in reports.

## Product Hunt

**Tagline:** Keep coding agents running, recoverable, and honest.

**Description:** Forge is an open-source reliability harness for autonomous coding workflows. It checkpoints every batch, routes specialized models by role, independently verifies completion with tests, and gives operators a live control plane for runs and approvals.

## Newsletter subject lines

- I open-sourced the reliability layer behind my coding agents
- Coding agents should not grade their own homework
- Durable, test-verified coding agents for local models
