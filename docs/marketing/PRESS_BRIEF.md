# Press brief

## What launched

Forge is an Apache-2.0 open-source reliability harness for long-running autonomous coding workflows.

## Why it matters

Most coding agents combine generation, state, routing, and completion judgment inside one probabilistic loop. Forge separates these concerns: deterministic orchestration owns state transitions, Postgres owns durable state, and an independent evaluator owns test-backed completion.

## Technical highlights

- Planner -> Auditor -> Executor -> Evaluator graph
- Dual acceptance contract: human checklist plus executable tests
- Durable batch checkpoints and bounded failure recovery
- Per-role OpenAI-compatible model routing
- FastAPI control plane and React operator console
- Human approvals for external and browser mutations
- Host command execution denied by default; Docker/VM recommended

## Founder quote

"The model should propose work, not silently own state or grade its own completion. Forge makes those boundaries explicit and inspectable."

## Links

- Repository: https://github.com/sciencenerd-des/forge
- Architecture: https://github.com/sciencenerd-des/forge/blob/main/ARCHITECTURE.md
- Security: https://github.com/sciencenerd-des/forge/blob/main/SECURITY.md
