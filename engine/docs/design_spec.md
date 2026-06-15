# Agent Autonomy & Memory Architecture

## 1. Core Philosophy
The agent must operate on a **Persistent State Machine** model. Success is not just a correct response, but a persistent state transition that survives the death of the current inference loop.

### The Harness Model (Steer vs. Execute)
We adopt the 'Harness' philosophy from the Anthropic workshop:
- **Human Role (Steer)**: Define high-level goals, constraints, and the "What" needs to be done.
- **Agent Role (Execute)**: Manage the "How" - the sequence of sub-tasks, tool calls, and intermediate state management.

## 2. The Loop (Plan -> Execute -> Save -> Repeat)
Every turn must follow this sequence:
1. **Load**: Retrieve the current Goal, active Task, and last recorded Memory.
2. **Plan**: Select the next logical action based on the Task description.
3. **Execute**: Perform the action (Tool call, file write, or thought).
4. **Save**: Forced write-back of the results, new blockers, and the *next* task.
5. **Checkpoint**: Create a `checkpoint` in the memory DB.

## 3. Database Schema Requirements
To support the "Forced Write-back," the database must persist the following entities:

### Goals
- `id`: UUID (PK)
- `title`: TEXT
- `description`: TEXT
- `status`: 'active', 'completed', 'blocked'
- `success_criteria`: JSONB
- `priority`: INT

### Tasks
- `id`: UUID (PK)
- `goal_id`: UUID (FK)
- `title`: TEXT
- `description`: TEXT
- `status`: 'proposed', 'active', 'completed', 'blocked'
- `next_step`: TEXT (The explicit next action for the Executor)
- `priority`: INT
- `created_at`: TIMESTAMP

### Memories & Context
- `memories`: Semantic (Vector) and Fact-based data.
- `blockers`: Explicit reasons why a task is stuck.
- `artifacts`: Paths to generated files/data.
- `agent_runs`: Log of turn counts, summaries, and resume instructions.

## 4. Model Roles
- **Planner (Stronger Model)**: 
  - Runs to break down Goals into Tasks.
  - Defines the `next_step` for the Executor.
- **Executor (Gemma 4 12B QAT)**: 
  - Runs the "Execution" phase of the loop.
  - Follows the `next_step` provided by the Planner.

## 5. The "Heartbeat" Prompt
The prompt sent to the Executor must include a system instruction:
> "You are a persistent task agent. You have a current task and a next step. 
> 1. Perform the next step.
> 2. If finished, describe the next logical task.
> 3. If blocked, describe the blocker.
> 4. You MUST output a summary in the following JSON format at the end of every response:
> {
>   'progress_summary': '...',
>   'next_task_description': '...',
>   'blocker': '...',
>   'resume_instruction': '...'
> }"

## 6. Validation Strategy
- **Checkpoint Verification**: Every 5 turns, the agent must verify that the `task_id` in memory matches the actual work done.
- **Red-Teaming**: Simulate "forgotten" context by clearing the immediate history and forcing the agent to rely only on the Memory DB.
