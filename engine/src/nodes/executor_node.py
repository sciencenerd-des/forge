import forge_config
import os
import json
import subprocess
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional
from src.state.schema import AgentState, Heartbeat
from hermes_tools import executor_llm, EXECUTOR_SCHEMA
from app.database import SessionLocal
from app.services import MemoryService
from src.runtime import project_workspace
from forge_runtime.tools import ToolContext, ToolRequest, default_registry

def record_tool_msg(tool_name: str, content: str, session_id: str | None = None):
    """Mirror a tool result only when the caller owns an explicit session.

    PGE runs are project-scoped, while Hermes gateway sessions are user
    conversations. Selecting the newest global session conflates unrelated
    projects and injects autonomous tool output into an active chat.
    """
    session_id = session_id or os.getenv("HERMES_SESSION_ID")
    if not session_id:
        return
    db_path = forge_config.state_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
        if cursor.fetchone() is None:
            conn.close()
            return
        
        cursor.execute("""
            INSERT INTO messages (session_id, role, tool_name, content, timestamp, active)
            VALUES (?, 'tool', ?, ?, ?, 1)
        """, (session_id, tool_name, content, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to insert tool msg to state.db: {e}")


def _is_placeholder_content(content: str) -> bool:
    lowered = (content or "").lower()
    markers = ("placeholder", "to be populated", "todo", "tbd")
    non_heading_lines = [line.strip() for line in (content or "").splitlines()
                         if line.strip() and not line.lstrip().startswith("#")]
    return any(marker in lowered for marker in markers) or not non_heading_lines

_WEB_DOCS_DB = forge_config.tooldocs_db_path()


def _venv_env(workspace: str) -> dict:
    """Environment with the project's .venv/bin and common toolchains
    prepended to PATH, so `pip`/`python`/`python3` always resolve to the
    project venv (fixes: host has no `pip`, only pip3; and installed packages
    being invisible to the test runner)."""
    env = dict(os.environ)
    extra = [os.path.join(workspace, ".venv", "bin"),
             os.path.join(workspace, "node_modules", ".bin"),
             "/opt/homebrew/bin", os.path.expanduser("~/.local/bin"),
             os.path.expanduser("~/.cargo/bin")]
    env["PATH"] = ":".join([p for p in extra if p]) + ":" + env.get("PATH", "")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


# Shell metacharacters that prove a run_command payload is a SHELL line, not a
# clean argv. The registry run_command is argv-only: a naive split feeds tokens
# like `>/dev/null`, `2>&1;`, `&&`, `$(...)` to the program as literal arguments
# (observed: `cmake: Unknown argument >/dev/null`), so the loop burns turns
# fighting the tool instead of the task. When shell-shaped, we route through a
# real shell (like the bash tool) so the model's correct commands just work.
_SHELL_META = (">", "<", "|", ";", "&", "$", "`", "&&", "||", "2>", "*", "(")

# Paths whose write_file was blocked once as "looks like an edit". A second
# attempt on the same path is honored as an intentional overwrite — small models
# (omnicoder) only ever emit write_file and will deadlock forever otherwise.
_WRITE_BLOCK_SEEN: set = set()


def _shellish_run_command(args: dict) -> bool:
    """True when run_command was handed a shell line: a `command` string, OR a
    `command`/`argv` LIST whose tokens contain shell metacharacters (the model
    sometimes packs a whole shell line into the argv list)."""
    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return True
    for key in ("command", "argv"):
        seq = args.get(key)
        if isinstance(seq, list) and any(any(m in str(tok) for m in _SHELL_META) for tok in seq):
            return True
    return False


def _run_command_string(args: dict) -> str:
    """Best-effort shell string from a run_command payload (command or argv).
    Handles command as a string OR a list, and argv as a list. Drops a stray
    trailing 'timeout_seconds' token the model sometimes appends to the list."""
    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    for seq in (cmd, args.get("argv")):
        if isinstance(seq, list) and seq:
            toks = [str(t) for t in seq if str(t) != "timeout_seconds"]
            return " ".join(toks)
    return ""


def _ensure_venv(workspace: str) -> str:
    """Create <workspace>/.venv once; return its python path."""
    import subprocess as _vsp
    vpy = os.path.join(workspace, ".venv", "bin", "python")
    if not os.path.exists(vpy):
        _vsp.run(["python3", "-m", "venv", os.path.join(workspace, ".venv")],
                 capture_output=True, timeout=120)
    return vpy if os.path.exists(vpy) else "python3"


def _fetch_doc(url: str) -> str:
    """curl a documentation URL ONCE; cache the text in tools_db forever.
    Subsequent fetches of the same URL are served from the cache."""
    import sqlite3, subprocess as _sp
    if not url.startswith(("http://", "https://")):
        return json.dumps({"status": "error", "message": "url must be http(s)"})
    con = sqlite3.connect(_WEB_DOCS_DB, timeout=3)
    con.execute("""CREATE TABLE IF NOT EXISTS web_docs (
        url TEXT PRIMARY KEY, content TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
    row = con.execute("SELECT content FROM web_docs WHERE url=?", (url,)).fetchone()
    if row:
        con.close()
        return json.dumps({"status": "success", "cached": True, "content": row[0][:8000]})
    r = _sp.run(["curl", "-sL", "--max-time", "30", "--max-filesize", "2000000", url],
                capture_output=True, text=True, timeout=40)
    if r.returncode != 0 or not r.stdout:
        con.close()
        return json.dumps({"status": "error", "message": f"fetch failed (curl exit {r.returncode})"})
    text = r.stdout
    if "<html" in text[:1000].lower():
        import re as _re
        text = _re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=_re.S | _re.I)
        text = _re.sub(r"<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text)
    from datetime import datetime, timezone
    con.execute("INSERT OR REPLACE INTO web_docs(url,content,fetched_at) VALUES (?,?,?)",
                (url, text[:60000], datetime.now(timezone.utc).isoformat()))
    con.commit(); con.close()
    return json.dumps({"status": "success", "cached": False, "content": text[:8000]})


def executor_node(state: AgentState) -> Dict:
    """
    Generator (Executor) Node: Performs the actual work for the current task.
    This node acts as the 'Worker'.
    """
    active_task = state.get('active_task')
    if not active_task:
        return {"heartbeat": None}

    project_id = state.get("project_id")
    if not project_id:
        raise ValueError("executor_node requires 'project_id' in state")
    db_workspace = SessionLocal()
    try:
        workspace = project_workspace(db_workspace, project_id)
    finally:
        db_workspace.close()
    
    # Fetch only project-scoped durable state. Interactive Hermes history is
    # deliberately excluded from the autonomy prompt.
    db = SessionLocal()
    try:
        service = MemoryService(db)
        context_pack = service.build_context_pack(project_id)
    except Exception as e:
        print(f"Error fetching context pack: {e}")
        context_pack = {
            "PROJECT": {"goal": state["goal"].description if state.get("goal") else ""},
            "NON_NEGOTIABLE_CONSTRAINTS": [],
            "DECISIONS_ALREADY_MADE": [],
            "RELEVANT_FILES": [],
            "RUNTIME_TOOL_STATE": {"recent_tool_invocations": [], "active_sandbox_info": {}}
        }
    finally:
        db.close()

    runtime_tool_state = context_pack.get("RUNTIME_TOOL_STATE", {})
    compression = context_pack.get("CONTEXT_COMPRESSION", {})
    if compression.get("snapshot_id"):
        print("Headroom context: "
              f"{compression.get('tokens_before', 0)} -> {compression.get('tokens_after', 0)} "
              f"tokens (saved {compression.get('tokens_saved', 0)}), "
              f"snapshot={compression['snapshot_id']}")
    elif compression.get("status") == "fallback_uncompressed":
        print(f"Headroom fallback: {compression.get('error')}")
    active_sandbox = runtime_tool_state.get("active_sandbox_info", {})
    tool_state_str = ""
    if active_sandbox:
        tool_state_str += f"Project workspace: {active_sandbox.get('workspace', workspace)}\n"

    # THE SINGLE SOURCE OF TRUTH: the verbatim audit tests. The executor must
    # satisfy these EXACT commands/strings — without them it satisfies its own
    # interpretation while the evaluator enforces the auditor's literals, and
    # the two never meet (observed: unit suite green for hours while audit
    # test T3 demanded an exact error sentence the executor never saw).
    acceptance = ""
    try:
        from src.nodes.auditor_node import load_audit_tests
        db_t = SessionLocal()
        try:
            for t in load_audit_tests(db_t, project_id):
                acceptance += (f"- {t.get('id')}: run `{t.get('command')}` -> must exit "
                               f"{t.get('expect_exit', 0)}")
                if t.get("expect_substring"):
                    acceptance += f" and print text containing EXACTLY: {t['expect_substring']!r}"
                acceptance += "\n"
        finally:
            db_t.close()
    except Exception:
        pass
    acceptance = acceptance or "(no acceptance tests issued yet)"

    # Compact context from the steward (small local model filters DB state
    # down to what THIS task needs — raw dumps bloated prompts with stale data).
    try:
        from src.steward import compact_context
        steward_brief = compact_context(project_id, active_task.title) or "(no briefing)"
    except Exception:
        steward_brief = "(steward unavailable)"

    # Feedback from the previous evaluation — this is what makes each attempt
    # DIFFERENT from the last one instead of replaying it verbatim.
    last_eval = state.get("last_eval") or {}
    prev_hb = state.get("heartbeat")
    feedback_parts = []
    if last_eval.get("reason"):
        feedback_parts.append(f"EVALUATOR VERDICT ON YOUR LAST ATTEMPT: {last_eval['reason']}")
    if last_eval.get("missing_items"):
        feedback_parts.append(f"CONTRACT ITEMS STILL UNMET: {last_eval['missing_items']}")
    if prev_hb is not None and getattr(prev_hb, "progress_summary", ""):
        feedback_parts.append(f"YOUR PREVIOUS SELF-REPORT: {prev_hb.progress_summary[:400]}")
    feedback_str = ""
    if feedback_parts:
        feedback_str = ("PREVIOUS ATTEMPT FEEDBACK (do something DIFFERENT this time; "
                        "do not repeat the same actions or the same heartbeat):\n"
                        + "\n".join(feedback_parts) + "\n\n")

    # Construct the System Instruction / Prompt
    system_prompt = f"""You are a Persistent Task Executor.

PROJECT GOAL: {context_pack.get('PROJECT', {}).get('goal', '')}
CURRENT TASK: {active_task.title}
TASK DESCRIPTION: {active_task.description}

ACCEPTANCE TESTS (the evaluator runs these EXACT commands to decide completion —
satisfy them LITERALLY, including exact output strings and exit codes):
{acceptance}
TEST HIERARCHY RULE: acceptance tests OUTRANK your own unit tests. If the
program already satisfies an acceptance test but your unit tests disagree,
fix YOUR UNIT TESTS to match the acceptance behavior — never change working
program behavior to satisfy a unit test.

STEWARD BRIEFING (fresh from the DB, stale context filtered out):
{steward_brief}

DYNAMIC AUDITOR REPAIR PACK (fresh durable facts; follow next_action and avoid repeats):
{json.dumps(state.get('dynamic_audit_context') or {}, default=str)[:12000]}

LESSONS / PREVIOUS MISTAKES (do not repeat):
{json.dumps(context_pack.get('LESSONS_AND_MISTAKES', []))}

OPEN BUGS / BLOCKERS / RISKS:
{json.dumps(context_pack.get('OPEN_BUGS_BLOCKERS_RISKS', []))}

RELEVANT FILES:
{json.dumps(context_pack.get('RELEVANT_FILES', []))}

RUNTIME TOOL STATE:
{tool_state_str}

{feedback_str}Your goal:
Perform the specific 'Next Step' required to achieve this task. Use your tools to perform the work.
AGENTIC CODING TOOLKIT (arguments shown; call exactly one per JSON block):
CORE (use these for almost everything):
- edit_file(path, old_string, new_string, replace_all=false): PREFERRED for code changes — replace ONE exact unique snippet (or every occurrence with replace_all=true for renames). NEVER rewrite a whole file to change part of it. old_string must be copied verbatim (whitespace included).
- multi_edit(path, edits=[{{old_string, new_string}}]): apply SEVERAL edits to one file in ONE call (atomic — all or nothing). Use when changing multiple spots; saves round-trips.
- write_file(path, content): create a NEW file only (modify existing files with edit_file).
- read_file(path): read a file before editing it.
- install_deps(packages=[...], manager="pip"): install dependencies the RIGHT way — pip installs into a project .venv (every later command/test sees them automatically), or manager="npm"/"brew"/"cargo". USE THIS for dependencies; never run raw `pip install` (the host has no `pip`, only a venv).
- bash(command, timeout_seconds=120): real shell — pipes, &&, redirects, globs. `python`/`pip`/`python3` resolve to the project .venv. Use for builds, running tests, git, file ops.
- grep(pattern, path="."): regex content search across files.
- glob(pattern): find files by pattern, e.g. "**/*.py".
ON-DEMAND (only when needed):
- list_files(path="."), search_text(query, path="."), run_command(argv-array, timeout_seconds=120)
- git_diff(ref="HEAD", path=""): see what changed (your last edit + the ratchet's reverts) — use when a change unexpectedly failed or was reverted.
- fetch_doc(url): fetch + permanently cache library/API docs — use instead of guessing an API.
- browser_fetch(url), notebook_cell(...) for scratch experiments (never counts as evidence).
Verification evidence is recorded automatically from actual tool results. You
cannot create evidence records directly.

You MUST respond with a single, valid JSON block ONLY.

To call a tool, output a JSON block like:
{{
  "type": "tool_call",
  "name": "write_file",
  "arguments": {{
    "path": "hello.py",
    "content": "print('Hello, Autonomy!')"
  }}
}}

Once you have completed the task or if you are blocked, output a Heartbeat JSON block:
{{
  "type": "heartbeat",
  "progress_summary": "Details of the files written/commands run and verification results",
  "next_task_description": "What to do in the next task",
  "blocker": null or "detailed explanation if blocked",
  "resume_instruction": "How a human/agent can resume work"
}}

Do NOT wrap the JSON block in any other text. Output ONLY the JSON block.
"""

    messages = [
        {"role": "user", "content": system_prompt}
    ]

    # One graph step permits one model decision and at most one tool call.
    # LangGraph owns iteration and durable state; a nested agent loop hid
    # repeated writes inside one node and exhausted its private budget before
    # the planner/evaluator could advance the task queue.
    # ONE iteration per turn deadlocks the executor: with a fresh transcript
    # each graph turn it reads a file, the budget ends, and next turn it has
    # no memory of what it read — so it reads again forever and can never
    # write (observed live: 17 consecutive read_file-only turns). A
    # read->write->verify->heartbeat cycle needs 4.
    max_iterations = int(os.getenv("PGE_MAX_EXECUTOR_ITERATIONS", "4"))
    tools_executed = []
    turn_action_sigs = []
    for iteration in range(max_iterations):
        print(f"--- Executor Turn {iteration + 1} ---")
        try:
            # Fully local: the 12B executes; the e2b steward coaches it via
            # the steering directives (loop/regression/error detection).
            response_raw = None
            # PHASE 1 — REASON (conditional): the extra ~700-token reasoning
            # pass earns its cost only when the task is HARD — a repair task,
            # a task already attempted (carrying evaluator feedback), or a
            # later in-turn iteration recovering from a tool error. On a fresh
            # task's first action it is wasted tokens; skip it then.
            _attempts = (state.get("task_attempts") or {}).get(active_task.id, 0)
            _hard = (active_task.title.startswith("Make audit test")
                     or _attempts >= 1 or iteration >= 1
                     or bool((state.get("last_eval") or {}).get("reason")))
            analysis = ""
            if _hard:
                try:
                    analysis = executor_llm.reason(messages + [{"role": "user", "content":
                        "Before acting: in at most 6 short lines, state (1) what the "
                        "failing acceptance/unit test actually requires, (2) what the "
                        "last tool outputs proved, (3) the MINIMAL next action and "
                        "which tool with what arguments. Plain text only."}])
                    if analysis:
                        print(f"🧠 Executor analysis: {analysis[:140]}")
                except Exception:
                    analysis = ""
            act_messages = list(messages)
            if analysis:
                act_messages.append({"role": "user", "content":
                    f"YOUR ANALYSIS (follow it):\n{analysis}\n\nNow emit ONLY the "
                    "single JSON block (tool_call or heartbeat) that executes this plan."})
            try:
                if response_raw is None:
                    response_raw = executor_llm.generate_chat(act_messages, schema=EXECUTOR_SCHEMA)
            except Exception as llm_err:
                # Backend timeout/unreachable: report it as a failed turn
                # instead of crashing the whole LangGraph run.
                print(f"💥 Executor LLM call failed: {llm_err}")
                return {"heartbeat": Heartbeat(
                    progress_summary=f"FAILED: LLM backend error this turn ({str(llm_err)[:150]}). No work done.",
                    next_task_description="Retry the same task next cycle",
                    blocker=None,
                    resume_instruction="Backend was slow/unreachable; the loop continues next cycle.")}
        except Exception as model_error:
            error_name = type(model_error).__name__
            db_error = SessionLocal()
            try:
                MemoryService(db_error).record_learning_stage(
                    project_id, active_task.id, "fail",
                    f"Local model call failed: {error_name}",
                    {"error_type": error_name, "iteration": iteration + 1},
                )
            finally:
                db_error.close()
            return {"heartbeat": Heartbeat(
                progress_summary=f"NO-ACTION TURN: local model call failed ({error_name}).",
                next_task_description="Retry the same bounded task after the local model is healthy.",
                blocker=None,
                resume_instruction="Resume from durable task evidence; do not repeat completed tool calls.",
            )}
        print(f"LLM Raw Output:\n{response_raw}\n")
        
        # Clean JSON block
        try:
            clean_raw = response_raw.strip()
            if "<think>" in clean_raw:
                clean_raw = clean_raw.split("</think>")[-1].strip()
            if "</think>" in clean_raw:
                clean_raw = clean_raw.split("</think>")[-1].strip()
                
            if "```json" in clean_raw:
                json_str = clean_raw.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_raw:
                json_str = clean_raw.split("```")[1].split("```")[0].strip()
            else:
                json_str = clean_raw
                
            data = json.loads(json_str)
        except Exception as parse_err:
            print(f"Failed to parse LLM JSON output: {parse_err}")
            # Append error message to LLM and retry
            messages.append({"role": "assistant", "content": response_raw})
            messages.append({"role": "user", "content": "Error: Output was not valid JSON. Please return ONLY a valid JSON block as specified."})
            continue

        msg_type = data.get("type")
        if msg_type == "heartbeat":
            if not tools_executed:
                data["progress_summary"] = ("NO-ACTION TURN (no tool executed; treat as zero "
                                            "progress): ") + data.get("progress_summary", "")
            # Successfully finished execution
            try:
                # Hard caps: a stuck model writes longer and longer self-
                # summaries that get fed forward and balloon every prompt
                # ("summarisation getting longer, nothing else happening").
                heartbeat = Heartbeat(
                    progress_summary=(data.get("progress_summary", "") or "")[:500],
                    next_task_description=(data.get("next_task_description", "") or "")[:300],
                    blocker=(data.get("blocker") or None) and str(data.get("blocker"))[:300],
                    resume_instruction=(data.get("resume_instruction", "") or "")[:300]
                )
                return {"heartbeat": heartbeat,
                        "last_actions": list(turn_action_sigs)}
            except Exception as e:
                print(f"Error constructing heartbeat schema: {e}")
                return {"heartbeat": Heartbeat(
                    progress_summary=f"FAILED: heartbeat malformed ({e})",
                    next_task_description="Retry the task",
                    blocker=None,
                    resume_instruction="Emit a valid heartbeat JSON")}

        elif msg_type == "tool_call":
            tool_name = data.get("name")
            args = data.get("arguments", {})
            tools_executed.append(tool_name)
            try:
                import hashlib as _hl
                _sig = f"{tool_name}:{_hl.md5(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:8]}"
                turn_action_sigs.append(_sig)
            except Exception:
                pass
            print(f"🔧 Checking alignment and executing tool: {tool_name} with args: {args}")
            
            # Format action description to check against active task keywords
            action_desc = f"{tool_name}: " + ", ".join(f"{k}={v}" for k, v in args.items())
            db_alignment = SessionLocal()
            try:
                service_align = MemoryService(db_alignment)
                alignment = service_align.classify_task_alignment(project_id, action_desc)
            except Exception as e:
                print(f"Alignment check failed: {e}")
                alignment = "distraction"
            finally:
                db_alignment.close()
                
            tool_result = ""
            # PRECONDITION CHECK (SDOF Algorithm 1 §5): structurally block a
            # write_file that is really an EDIT of an existing file — this is
            # the dispatch-layer enforcement of minimal-diff editing that the
            # prompt alone could not achieve (whole-file rewrites perturbed
            # working code 18x in one run). A genuine new file is allowed.
            _precond_block = None
            if tool_name == "write_file":
                _wp = str(args.get("path", ""))
                _abs = os.path.join(workspace, _wp) if not os.path.isabs(_wp) else _wp
                if os.path.isfile(_abs) and os.path.getsize(_abs) > 0:
                    _new_lines = (args.get("content") or "").splitlines()
                    try:
                        _old_lines = open(_abs, encoding="utf-8", errors="ignore").read().splitlines()
                    except Exception:
                        _old_lines = []
                    _shared = len(set(_old_lines) & set(_new_lines))
                    _overlap = _shared / max(len(_old_lines), 1)
                    # >40% shared lines => this is an edit dressed as a rewrite.
                    # BUT: a small source file (a stub the executor is meant to
                    # replace) or a path already blocked once is honored as an
                    # intentional overwrite — small models only ever emit
                    # write_file and deadlock forever against a hard block
                    # (observed: omnicoder retried main.cpp 4x, T7 never moved).
                    _small_file = len(_old_lines) <= 120
                    _seen_before = _abs in _WRITE_BLOCK_SEEN
                    if _overlap > 0.4 and not (_small_file or _seen_before):
                        _WRITE_BLOCK_SEEN.add(_abs)
                        _precond_block = (f"PRECONDITION_FAIL: write_file on existing '{_wp}' shares "
                            f"{int(_overlap*100)}% of its lines with the current file — this is an "
                            "EDIT, not a new file. Use edit_file(path, old_string, new_string) to "
                            "change only the wrong lines. (To intentionally replace a file, bash `rm` it first.)")
                    elif _overlap > 0.4:
                        print(f"📝 write_file overwrite ALLOWED for '{_wp}' "
                              f"({'small file' if _small_file else 'repeated intent'}) — honoring rewrite.")
            if _precond_block is not None:
                tool_result = json.dumps({"status": "error", "message": _precond_block})
                print(f"🚧 {_precond_block[:90]}")
            elif alignment == "distraction":
                tool_result = json.dumps({
                    "status": "error",
                    "message": f"BLOCKED: The action '{action_desc}' was flagged as a distraction/drift from the active task. Please focus strictly on completing the active task: {active_task.title}."
                })
                print(f"⚠️ Guardrail Blocked Distraction Tool Call: {action_desc}")
            else:
                try:
                    if tool_name == "edit_file":
                        # SURGICAL EDIT: exact unique-string replacement so the
                        # 12B changes only the lines that are wrong, instead of
                        # regenerating the whole file from memory (which
                        # perturbed working code 18x in one run -> regressions
                        # -> reverts). Routed through the sandboxed registry.
                        _ctx = ToolContext(workspace=Path(workspace), allow_write=True,
                                           allow_shell=False, allow_network=False,
                                           allowed_hosts=frozenset())
                        _path = str(args.get("path", "")); _old = args.get("old_string", "")
                        _new = args.get("new_string", "")
                        _rd = default_registry().execute(_ctx,
                            ToolRequest("read_file", {"path": _path}, call_id="edit-read"))
                        if not _rd.ok:
                            tool_result = json.dumps({"status": "error",
                                "message": f"edit_file: cannot read {_path}: {_rd.error}"})
                        else:
                            _content = _rd.data.get("content", "")
                            _cnt = _content.count(_old) if _old else 0
                            _all = bool(args.get("replace_all"))
                            if _cnt == 0:
                                tool_result = json.dumps({"status": "error",
                                    "message": f"edit_file: old_string NOT FOUND in {_path}. "
                                    "read_file first and copy an EXACT snippet (with whitespace)."})
                            elif _cnt > 1 and not _all:
                                tool_result = json.dumps({"status": "error",
                                    "message": f"edit_file: old_string appears {_cnt}x in {_path} — "
                                    "include surrounding lines to make it UNIQUE, or pass "
                                    "replace_all=true to change every occurrence (e.g. a rename)."})
                            else:
                                _patched = _content.replace(_old, _new) if _all else _content.replace(_old, _new, 1)
                                _wr = default_registry().execute(_ctx, ToolRequest("write_file",
                                    {"path": _path, "content": _patched},
                                    call_id="edit-write"))
                                tool_result = json.dumps(_wr.to_dict())
                                record_tool_msg("edit_file", tool_result[:200])
                                if _wr.ok:
                                    _dbx = SessionLocal()
                                    try:
                                        MemoryService(_dbx).record_file_change(
                                            project_id=project_id, task_id=active_task.id,
                                            file_path=_wr.data["path"],
                                            change_summary=f"Edited {Path(_wr.data['path']).name}",
                                            reason="surgical edit")
                                    finally:
                                        _dbx.close()
                    elif tool_name == "multi_edit":
                        # Atomic batch of exact edits to ONE file in a single
                        # tool call — cuts LLM round-trips (the 12B's dominant
                        # cost) when several lines need changing. All-or-nothing:
                        # if any edit's old_string is missing/ambiguous, NONE apply.
                        _ctx = ToolContext(workspace=Path(workspace), allow_write=True,
                                           allow_shell=False, allow_network=False, allowed_hosts=frozenset())
                        _path = str(args.get("path", "")); _edits = args.get("edits") or []
                        _rd = default_registry().execute(_ctx,
                            ToolRequest("read_file", {"path": _path}, call_id="me-read"))
                        if not _rd.ok:
                            tool_result = json.dumps({"status": "error",
                                "message": f"multi_edit: cannot read {_path}: {_rd.error}"})
                        else:
                            _c = _rd.data.get("content", ""); _err = None
                            for _i, _e in enumerate(_edits):
                                _o = _e.get("old_string", ""); _n = _e.get("new_string", "")
                                _k = _c.count(_o) if _o else 0
                                if _k == 0:
                                    _err = f"edit #{_i+1}: old_string not found"; break
                                if _k > 1 and not _e.get("replace_all"):
                                    _err = f"edit #{_i+1}: old_string appears {_k}x — make it unique or replace_all"; break
                                _c = _c.replace(_o, _n) if _e.get("replace_all") else _c.replace(_o, _n, 1)
                            if _err:
                                tool_result = json.dumps({"status": "error",
                                    "message": f"multi_edit aborted ({_err}). NO changes applied — fix and resend all edits."})
                            else:
                                _wr = default_registry().execute(_ctx, ToolRequest("write_file",
                                    {"path": _path, "content": _c}, call_id="me-write"))
                                tool_result = json.dumps(_wr.to_dict())
                                record_tool_msg("multi_edit", tool_result[:200])
                                if _wr.ok:
                                    _dbx = SessionLocal()
                                    try:
                                        MemoryService(_dbx).record_file_change(
                                            project_id=project_id, task_id=active_task.id,
                                            file_path=_wr.data["path"],
                                            change_summary=f"Multi-edited {Path(_wr.data['path']).name} ({len(_edits)} edits)",
                                            reason="batch surgical edit")
                                    finally:
                                        _dbx.close()

                    elif tool_name == "git_diff":
                        # Let the model SEE what changed — its own last edit and
                        # the ratchet's reverts. Critical for self-correction:
                        # without it the 12B re-derives state blindly each turn.
                        import subprocess as _dsp
                        _ref = str(args.get("ref", "HEAD"))
                        _gp = str(args.get("path", ""))
                        _cmd = f"git diff {_ref} -- {_gp}".strip() if _gp else f"git diff {_ref}"
                        try:
                            _d = _dsp.run(["/bin/bash", "-lc",
                                f"{_cmd} 2>&1 | head -200; echo '--- recent checkpoints ---'; "
                                "git log --oneline -8 2>/dev/null"],
                                capture_output=True, text=True, timeout=20, cwd=workspace)
                            tool_result = json.dumps({"status": "success", "diff": (_d.stdout or "(no diff)")[:6000]})
                        except Exception as _de:
                            tool_result = json.dumps({"status": "error", "message": f"git_diff: {_de}"})
                        record_tool_msg("git_diff", tool_result[:200])

                    elif tool_name == "install_deps":
                        # DETERMINISTIC dependency install — the model only
                        # names packages + manager; the harness gets the
                        # incantation right (venv for pip, etc.) so a small
                        # model can't fail on `pip` vs `pip3`, missing venv, or
                        # PATH. Full network.
                        from forge_runtime.tools import host_execution_allowed as _host_exec
                        import subprocess as _isp
                        _mgr = (args.get("manager") or "pip").lower()
                        _pkgs = args.get("packages") or []
                        if isinstance(_pkgs, str): _pkgs = _pkgs.split()
                        _envp = _venv_env(workspace)
                        try:
                            if not _host_exec():
                                raise ValueError("dependency installation is disabled outside an isolated container")
                            if _mgr in ("pip", "python", "pip3"):
                                _vpy = _ensure_venv(workspace)
                                _cmd = [_vpy, "-m", "pip", "install", *_pkgs]
                            elif _mgr in ("npm", "node"):
                                _cmd = ["npm", "install", *_pkgs]
                            elif _mgr == "brew":
                                _cmd = ["brew", "install", *_pkgs]
                            elif _mgr == "cargo":
                                _cmd = ["cargo", "add", *_pkgs]
                            else:
                                _cmd = [_mgr, "install", *_pkgs]
                            _ir = _isp.run(_cmd, capture_output=True, text=True,
                                           timeout=600, cwd=workspace, env=_envp)
                            tool_result = json.dumps({
                                "status": "success" if _ir.returncode == 0 else "error",
                                "manager": _mgr, "packages": _pkgs,
                                "exit_code": _ir.returncode,
                                "stdout": (_ir.stdout or "")[-3000:],
                                "stderr": (_ir.stderr or "")[-3000:],
                                "note": "pip packages installed into ./.venv — tests run with .venv on PATH automatically."})
                        except _isp.TimeoutExpired:
                            tool_result = json.dumps({"status": "error", "message": "install timed out (600s)"})
                        except Exception as _ie:
                            tool_result = json.dumps({"status": "error", "message": f"install_deps: {_ie}"})
                        record_tool_msg("install_deps", tool_result[:200])

                    elif tool_name == "bash":
                        # Real shell (pipes, redirects, &&, globs) — the
                        # registry run_command is argv-only. Sandboxed to the
                        # workspace; output recorded as test-run evidence.
                        from forge_runtime.tools import host_execution_allowed as _host_exec
                        import subprocess as _bsp
                        _cmd = args.get("command", "")
                        _to = min(int(args.get("timeout_seconds", 120) or 120), 600)
                        try:
                            if not _host_exec():
                                raise ValueError("shell execution is disabled outside an isolated container")
                            _r = _bsp.run(["/bin/bash", "-lc", _cmd], capture_output=True,
                                          text=True, timeout=_to, cwd=workspace,
                                          env=_venv_env(workspace))
                            tool_result = json.dumps({"status": "success" if _r.returncode == 0 else "error",
                                "exit_code": _r.returncode, "stdout": (_r.stdout or "")[-6000:],
                                "stderr": (_r.stderr or "")[-3000:]})
                        except _bsp.TimeoutExpired:
                            tool_result = json.dumps({"status": "error", "message": f"bash timed out after {_to}s"})
                        except Exception as _be:
                            tool_result = json.dumps({"status": "error", "message": f"bash: {_be}"})
                        record_tool_msg("bash", tool_result[:200])
                        _dbb = SessionLocal()
                        try:
                            MemoryService(_dbb).record_test_run(project_id=project_id,
                                task_id=active_task.id, command=_cmd,
                                status="success" if '"status": "success"' in tool_result else "failure",
                                output_summary=tool_result[:300])
                        except Exception:
                            pass
                        finally:
                            _dbb.close()

                    elif tool_name == "grep":
                        # Content search by regex (ripgrep if present, else python).
                        import subprocess as _gsp
                        _pat = args.get("pattern", ""); _gp = str(args.get("path", "."))
                        try:
                            _rg = _gsp.run(["rg", "-n", "--no-heading", "-S", _pat, _gp],
                                           capture_output=True, text=True, timeout=30, cwd=workspace)
                            _out = _rg.stdout
                        except FileNotFoundError:
                            _gg = _gsp.run(["/bin/bash", "-lc",
                                f"grep -rn -E {json.dumps(_pat)} {json.dumps(_gp)} 2>/dev/null"],
                                capture_output=True, text=True, timeout=30, cwd=workspace)
                            _out = _gg.stdout
                        except Exception as _ge:
                            _out = f"grep error: {_ge}"
                        tool_result = json.dumps({"status": "success",
                            "matches": (_out or "(no matches)")[:6000]})
                        record_tool_msg("grep", tool_result[:200])

                    elif tool_name == "glob":
                        # Find files by glob pattern, relative to the workspace.
                        _pat = args.get("pattern", "**/*")
                        try:
                            _hits = sorted(str(q.relative_to(workspace))
                                           for q in Path(workspace).glob(_pat) if q.is_file())[:300]
                            tool_result = json.dumps({"status": "success", "files": _hits})
                        except Exception as _gle:
                            tool_result = json.dumps({"status": "error", "message": f"glob: {_gle}"})
                        record_tool_msg("glob", tool_result[:200])

                    elif tool_name == "run_command" and _shellish_run_command(args):
                        # The model handed run_command a SHELL line (pipes, >,
                        # ;, $(), &&) — the argv-only registry tool would mangle
                        # it. Run it through a real shell, like the bash tool.
                        from forge_runtime.tools import host_execution_allowed as _host_exec
                        import subprocess as _rsp
                        _cmd = _run_command_string(args)
                        _to = min(int(args.get("timeout_seconds", 120) or 120), 600)
                        try:
                            if not _host_exec():
                                raise ValueError("shell execution is disabled outside an isolated container")
                            _r = _rsp.run(["/bin/bash", "-lc", _cmd], capture_output=True,
                                          text=True, timeout=_to, cwd=workspace,
                                          env=_venv_env(workspace))
                            tool_result = json.dumps({"ok": _r.returncode == 0, "tool": "run_command",
                                "status": "success" if _r.returncode == 0 else "error",
                                "exit_code": _r.returncode, "stdout": (_r.stdout or "")[-6000:],
                                "stderr": (_r.stderr or "")[-3000:]})
                        except _rsp.TimeoutExpired:
                            tool_result = json.dumps({"ok": False, "status": "error",
                                "message": f"run_command timed out after {_to}s"})
                        except Exception as _re:
                            tool_result = json.dumps({"ok": False, "status": "error",
                                "message": f"run_command: {_re}"})
                        record_tool_msg("run_command", tool_result[:200])
                        _dbr = SessionLocal()
                        try:
                            MemoryService(_dbr).record_test_run(project_id=project_id,
                                task_id=active_task.id, command=_cmd,
                                status="success" if '"status": "success"' in tool_result else "failure",
                                output_summary=tool_result[:300])
                        except Exception:
                            pass
                        finally:
                            _dbr.close()

                    elif tool_name in default_registry().names:
                        allowed_hosts = frozenset(
                            host.strip().lower()
                            for host in os.getenv("FORGE_BROWSER_ALLOWED_HOSTS", "").split(",")
                            if host.strip()
                        )
                        context = ToolContext(
                            workspace=Path(workspace),
                            allow_write=True,
                            allow_shell=True,
                            allow_network=bool(allowed_hosts),
                            allowed_hosts=allowed_hosts,
                        )
                        result = default_registry().execute(
                            context,
                            ToolRequest(tool_name, args, call_id=f"{state.get('current_run_id', '')}:{iteration}"),
                        )
                        tool_result = json.dumps(result.to_dict())
                        record_tool_msg(tool_name, tool_result[:200])

                        db_ev = SessionLocal()
                        try:
                            service = MemoryService(db_ev)
                            if tool_name == "write_file" and result.ok:
                                service.record_file_change(
                                    project_id=project_id,
                                    task_id=active_task.id,
                                    file_path=result.data["path"],
                                    change_summary=f"Wrote {Path(result.data['path']).name}",
                                    reason="Task execution",
                                )
                            elif tool_name == "run_command":
                                service.record_test_run(
                                    project_id=project_id,
                                    task_id=active_task.id,
                                    command=json.dumps(result.data.get("argv", [])),
                                    status="success" if result.ok else "failure",
                                    output_summary=(result.data.get("stdout") or result.data.get("stderr") or "")[:300],
                                    failure_summary=result.error or "",
                                )
                        finally:
                            db_ev.close()

                    elif tool_name == "fetch_doc":
                        tool_result = _fetch_doc(str(args.get("url", "")))
                        record_tool_msg("fetch_doc", tool_result[:200])

                    else:
                        tool_result = json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"})

                except Exception as tool_err:
                    print(f"Tool execution failed: {tool_err}")
                    tool_result = json.dumps({"status": "error", "message": str(tool_err)})

            print(f"Tool Result: {tool_result}\n")
            # Feed the result back into THIS turn's transcript and continue the
            # cycle. Returning after every single tool call deadlocked the
            # executor: the transcript resets between graph turns, so a model
            # that reads a file loses what it read before it can write — it
            # re-read the same file 17 turns straight doing zero work. A
            # read->modify->write->verify cycle needs in-turn memory.
            messages.append({"role": "assistant", "content": response_raw})
            messages.append({"role": "user", "content": f"Tool execution result:\n{tool_result[:4000]}\n\nContinue: take the next action toward the task, or emit a heartbeat if done."})
        else:
            # Not a tool call or heartbeat, or unknown type
            messages.append({"role": "assistant", "content": response_raw})
            messages.append({"role": "user", "content": "Error: Invalid response format. Please output a valid JSON block of type 'tool_call' or 'heartbeat'."})

    print("Executor produced no valid action in its single bounded decision.")
    return {"heartbeat": Heartbeat(
        progress_summary="NO-ACTION TURN: model produced no valid tool action.",
        next_task_description="Retry the same atomic task with one valid action.",
        blocker=None,
        resume_instruction="Inspect the structured-output failure; do not increase a tool budget.",
    )}
