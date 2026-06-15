# Positioning

## Category

Open-source reliability and orchestration harness for autonomous coding agents.

## Primary audience

1. Developers running coding agents on local models, LM Studio, Ollama, or vLLM.
2. AI engineers building long-running agent workflows that need durable state and replayable evidence.
3. Small teams evaluating autonomous coding without handing correctness to model self-report.

## One-line pitch

Forge keeps coding agents running, recoverable, and honest about completion.

## Message hierarchy

1. **Verified completion:** the evaluator runs the acceptance tests itself.
2. **Durable recovery:** every batch checkpoints to Postgres and resumes after failure.
3. **Model portability:** route any OpenAI-compatible model to each loop role.
4. **Operator control:** inspect runs, logs, approvals, and state from one console.
5. **Explicit safety boundary:** execute untrusted model commands inside a container or VM.

## Differentiation

| Raw agent loop | Forge |
|---|---|
| Model says it is done | Independent evaluator runs the contract |
| Conversation is the state | Postgres is the durable state |
| One failure ends the run | Bounded retries and resumable batches |
| One model handles everything | Per-role provider and model routing |
| Logs are incidental | Events, evidence, tests, and lifecycle are first-class |

## Claims policy

Use: reliable, durable, test-verified, local-model friendly, observable, resumable.

Avoid: fully autonomous, production-ready, secure sandbox, guaranteed correctness, zero supervision, builds any software.

## Common objections

**Why not use a coding agent directly?** Forge is the control and verification layer around one. It focuses on long-run durability and evidence rather than replacing the underlying model.

**Does it work with local models?** Yes. OpenAI-compatible backends are the primary integration boundary, with role-specific routing.

**Is model-generated code trusted?** No. Completion requires independent tests. Command execution is disabled on the host by default and should run in isolation.

**Why Postgres?** The engine uses durable relational state and Postgres array/vector features to resume and audit multi-step runs.
