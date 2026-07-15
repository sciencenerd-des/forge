# Forge Capability Index (always loaded)

_Authoritative list of tools/skills/frameworks you have access to. This is the INDEX (name · location · when to use). For full usage of any item call the `pge_get_tool_doc` tool — do not guess an interface._

_Generated from `tools_db/tool_docs.db` — 17 entries._

## Core file & shell
- **bash** — _Forge native (terminal backend; e2b sandbox)_ — Run shell commands: build, test, git, file ops, process control.
- **glob** — _Forge native_ — Find FILES by name/path pattern (e.g. **/*.py).
- **grep** — _Forge native (ripgrep)_ — Search file CONTENTS by regex across the repo.
- **read_file** — _Forge native (tools/file_operations.py)_ — Read a file's contents (text, images, PDFs, notebooks).
- **write_file / edit** — _Forge native (tools/file_operations.py)_ — Create or modify files with exact edits.
## Code execution & harnesses
- **code_execution (python)** — _Forge native (tools/code_execution_tool.py) -> e2b_ — Run Python for data work, scripts, quick computation, tests.
- **e2b sandbox** — _Cloud (e2b terminal backend)_ — Isolated environment where ALL code execution runs.
- **opencode** — _e2b sandbox (`opencode-ai`, preinstalled)_ — Delegate larger autonomous coding tasks to the OpenCode agent.
- **pi** — _e2b sandbox (`@mariozechner/pi-coding-agent`, preinstalled, global `pi`)_ — Alternate coding harness for autonomous implementation tasks.
## Web & browser
- **browser (browser_navigate/click/type/snapshot/vision)** — _Forge native toolset `browser`_ — Drive a real browser: navigate, click, type, scroll, snapshot, visual check.
- **context7** — _MCP: context7 (mcp_context7_* tools)_ — Fetch up-to-date library/framework/API documentation.
- **web (search/fetch)** — _Forge toolset `web`_ — Search the web and fetch/extract page content for research.
## Delegation
- **delegate / subagents** — _Forge native (tools/delegate_tool.py)_ — Spawn focused sub-agents for parallel inspection or narrow tasks.
## Persistent memory (Postgres/PGE)
- **lessons (continuous learning)** — _Native Forge plugin toolset `pge`: pge_record_lesson / pge_recall_lessons_ — Persist lessons from failures and proven recipes; recall them before similar work.
- **pge_memory (Postgres tools)** — _Native Forge plugin toolset `pge` (~/.forge/plugins/pge) + DB at postgresql:///forge_memory_ — Persistent project/goal/task/memory state for long-horizon autonomous work.
## Frameworks
- **PGE framework (Planner-Generator-Evaluator)** — _~/Developer/forge_memory (run_pge.py + agent-autonomy-project/)_ — Run a fully autonomous plan->execute->evaluate loop over the Postgres state.
## Meta / docs
- **pge_get_tool_doc / pge_list_tool_docs** — _Native Forge plugin toolset `pge` (~/.forge/plugins/pge), DB tools_db/tool_docs.db_ — Look up the FULL documentation for any tool by name, on demand.
