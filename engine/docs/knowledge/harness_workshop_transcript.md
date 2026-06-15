# Transcript: Anthropic Workshop - Build Agents That Run for Hours
Source: https://www.youtube.com/watch?v=mR-WAvEPRwE
Topic: Agentic Engineering, Multi-Agent Workflows, Harness Model.

## Key Takeaways

### 1. The Agentic Engineering Paradigm
- Shift from "AI as a tool" to "AI as a worker."
- The core of agentic engineering is the **Harness**: humans steer (intent, constraints), agents execute (steps, reasoning, tool use).
- Complexity is managed by breaking high-level goals into small, verifiable tasks.

### 2. Long-Horizon Autonomy
- Agents that "run for hours" do not do this in one go.
- They iterate: Plan -> Action -> Observe -> Re-plan.
- Persistence is key. The state of the "Plan" must be saved between turns so the agent doesn't lose its place.

### 3. The Planner-Generator-Evaluator Loop
- **Planner**: Deconstructs the "What" into a "To-Do List."
- **Generator (Executor)**: Performs one item on the list at a time.
- **Evaluator**: Checks the result against the original success criteria.
- This cycle repeats until the goal is met or a blocker is identified.

### 4. Handling Blockers
- Agents must be explicit about failures. 
- Instead of guessing, an agent should identify a "Blocker" (e.g., "I don't have the API key for X" or "The tool returned an unexpected error") and stop.
- This allows the human "Steerer" to intervene and provide the missing piece.

### 5. Context Compaction vs. State Persistence
- Simply adding more context doesn't help long-horizon goals.
- The solution is **State Persistence**: keeping the current "Task ID" and "Next Step" as a first-class citizen in the database.

---
*Scraped from YouTube via Playwright.*
