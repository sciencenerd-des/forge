"""Independent Auditor — dual-contract generator for the PGE (Ralph) loop.

A BIG cloud model (Ollama free cloud tier, proxied through the local Ollama
daemon at :11434) reads the original user prompt / goal INDEPENDENTLY of the
planner and produces TWO artifacts, both strictly derived from the request:

1. CHECKLIST  -> the executor's contract: subgoals broken into concrete tasks
                 (what must be built/changed). Persisted to
                 ``HermesGoal.success_criteria`` and seeded into the task queue.
2. TEST LIST  -> the evaluator's contract: deterministic shell commands with
                 expected outputs. The evaluator RUNS these itself every cycle —
                 it does not trust the executor's self-report. All tests pass
                 -> the loop may terminate. Any fail -> the failing output is
                 fed back into the next executor attempt.

Together they are the steering contract: the local model cannot weaken, drift
from, or argue with criteria it did not write.

Cloud calls happen once per goal (free tier is plenty). If every cloud model
fails (429 / offline), we degrade to the local model so the loop never blocks.
"""
import os
import forge_config
import json
import urllib.request

OLLAMA_URL = os.getenv("PGE_AUDITOR_BASE_URL", "http://127.0.0.1:11434/v1")

AUDITOR_MODELS = [
    m.strip() for m in os.getenv(
        "PGE_AUDITOR_MODELS",
        # The contract is the keystone — it gates the entire loop. Use the
        # 12B (capable), not the 2B e2b (which wrote a python `import raytracing`
        # test for a C++ goal — 2026-06-13). e2b remains the steward (context/
        # steering) where 2B suffices; contracts need the bigger brain.
        "lmstudio:google/gemma-4-12b-qat,lmstudio:google/gemma-4-e2b",
    ).split(",") if m.strip()
]

# Optional last cloud resort before the weak local fallback: Codex GPT-5.5
# at low reasoning, called through the Hermes CLI (OAuth lives there).
CODEX_FALLBACK = os.getenv("PGE_AUDITOR_CODEX", "1") not in ("0", "false", "")
CODEX_MODEL = os.getenv("PGE_AUDITOR_CODEX_MODEL", "gpt-5.5")


def _codex_chat(prompt: str, timeout: int = 300) -> str:
    """Ask Codex via `hermes chat -q` (non-interactive). Returns raw text."""
    import subprocess
    r = subprocess.run(
        ["hermes", "chat", "-q", prompt, "-Q", "--provider", "openai-codex",
         "-m", CODEX_MODEL, "--ignore-user-config"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"hermes codex call failed: {(r.stderr or r.stdout)[:150]}")
    return r.stdout

CONTRACT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "audit_contract",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "checklist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "criterion": {"type": "string"},
                            "verification": {"type": "string"},
                        },
                        "required": ["id", "criterion", "verification"],
                    },
                },
                "tests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "command": {"type": "string"},
                            "expect_substring": {"type": "string"},
                            "expect_exit": {"type": "integer"},
                        },
                        "required": ["id", "command", "expect_substring", "expect_exit"],
                    },
                },
            },
            "required": ["checklist", "tests"],
        },
    },
}

_PROMPT = """You are an INDEPENDENT auditor for an autonomous coding agent. You
did not write the plan and you will not execute it. Read the goal and derive,
from first principles, the full contract that defines "done".

GOAL TITLE: {title}
GOAL DESCRIPTION: {description}
ORIGINAL USER REQUEST: {user_prompt}

PROPOSED PLAN (written by the planner — use it to understand the intended
implementation, but your contract must validate the USER REQUEST, not the plan;
if the plan misses part of the request, your contract must still cover it):
{plan}

LESSONS FROM PAST FAILURES (learn from these — do NOT repeat the mistakes that
deadlocked previous runs):
{lessons}

TECHNOLOGY OF THIS GOAL (your tests MUST match this stack — a mismatch makes
the contract unsatisfiable and deadlocks the loop):
{stack_hint}

ANTI-HALLUCINATION RULES (a previous run looped for 2 days hunting for files it
believed already existed):
- The workspace starts EMPTY unless the request says otherwise. NEVER assume
  any file, asset, library, or directory already exists.
- For a "create/build/generate from scratch" goal, every test must verify an
  artifact that THIS run produces — e.g. `test -f <file>` then a build/run
  check. NEVER write a test that "locates", "finds", or "copies" pre-existing
  files; that premise is false and creates an unsatisfiable search loop.
- Tests run from the project workspace root with RELATIVE paths to artifacts
  the run creates. Do not reference the user's other folders.

Produce TWO artifacts, ALL strictly derived from the user request above:

1. "checklist" (3-8 items) — end-state facts that must be TRUE when the goal is
   achieved. Each: CONCRETE (a file with specific content, a passing command,
   a visible behaviour), NECESSARY (false item = goal not done), and DERIVED —
   every number, path, name and threshold must come from the request itself;
   NEVER invent constraints the request does not state. Re-check the set is
   MUTUALLY CONSISTENT (no two items can be impossible to satisfy together).

2. "tests" (2-8) — DETERMINISTIC shell commands the evaluator will run itself
   to verify the goal, without trusting anyone's claims. Each test:
   - "command": a single non-interactive bash command (no placeholders, no
     <angle-brackets>, absolute paths). It must terminate quickly.
   - "expect_substring": text that MUST appear in stdout+stderr for a pass
     (empty string if exit code alone suffices).
   - "expect_exit": required exit code (almost always 0).
   Tests must cover every checklist item that a command can verify. Commands
   must only READ state (cat, test, grep, ls, curl localhost, python -c
   checks, py_compile) — never mutate it.
   PLATFORM RULES (target is macOS; violating these makes a test permanently
   unpassable and deadlocks the whole system):
   - NEVER rely on `grep -L` exit codes (GNU/BSD differ).
   - NEVER pipe `unittest`/`pytest` output into grep — their verbose output
     goes to STDERR, the pipe sees nothing. To verify tests pass use e.g.
     `python3 -m pytest -q` with expect_substring on the summary, or
     `python3 -m unittest -v 2>&1 | grep ...` only with explicit 2>&1.
   - Prefer ONE simple command per test (test -f X; grep -c pat file;
     python3 -m py_compile X). Avoid && chains and pipelines when possible.
   - Every test MUST be able to FAIL. `find`/`ls`-style commands exit 0 even
     when they find nothing — they need a non-empty expect_substring.
   - expect_substring must be text the program will LITERALLY print — never a
     description of the output. WRONG: expect "friendly error message" or
     "word count" (those are descriptions). RIGHT: create the input yourself
     so the output is exactly knowable, e.g.
     command: printf 'a a a b\n' > /tmp/wf_fix.txt && python3 wordfreq.py /tmp/wf_fix.txt
     expect_substring: "a 3"
     Tests MAY create their own fixture files under /tmp only. If you cannot
     know a literal output string, use expect_substring "" and exit code only.
   - For error messages expect only a SHORT stable fragment ("not found",
     "is empty") — NEVER a full sentence and NEVER one embedding a file path
     (programs may echo the full path you passed).
   - NEVER invent counts ("Ran 4 tests") — you do not know how many tests the
     implementation will have. Use 'OK' or exit code for suite checks.
   - THE TESTS ARE THE SPECIFICATION. The implementer will read ONLY your test
     list to learn the exact required behavior — every exact string, path
     handling rule, and exit code must be derivable from the tests themselves,
     and the tests must be mutually consistent with each other.

Return JSON: {{"checklist": [...], "tests": [{{"id": "T1",
"command": "...", "expect_substring": "...", "expect_exit": 0}}]}}"""


def cloud_chat(messages: list, model: str = None, max_tokens: int = 4000, timeout: int = 240) -> str:
    """Multi-message chat with the primary cloud model (executor escalation).
    Raises on failure — callers fall back to the local model."""
    # Default to the first CLOUD entry — the chain is local-first for audits,
    # but escalation explicitly wants the big cloud brain.
    model = model or next((m for m in AUDITOR_MODELS if not m.startswith("lmstudio:")),
                          "gemma4:31b-cloud")
    body = {"model": model, "messages": messages, "temperature": 0.2,
            "max_tokens": max_tokens, "reasoning_effort": "low"}
    req = urllib.request.Request(
        f"{OLLAMA_URL}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = (data.get("choices") or [{}])[0].get("message")
    return (msg.get("content") if isinstance(msg, dict) else str(msg or "")) or ""


def _post_chat(model: str, prompt: str, schema=None, max_tokens=6000, timeout=None) -> str:
    if timeout is None:
        # Thinking cloud models need 60-180s for a full contract; 45s caused
        # every run to silently degrade to the weak local auditor.
        timeout = int(os.getenv("PGE_AUDITOR_TIMEOUT_SECONDS", "240"))
    # "lmstudio:<id>" entries route to the local LM Studio server (:1234).
    base_url = OLLAMA_URL
    if model.startswith("lmstudio:"):
        model = model.split(":", 1)[1]
        base_url = os.getenv("PGE_LMSTUDIO_URL", "http://127.0.0.1:1234/v1")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        # Thinking cloud models (minimax) burn the budget on CoT otherwise.
        "reasoning_effort": "low",
    }
    # NOTE: response_format json_schema is NOT sent — Ollama cloud ignores or
    # garbles it (verified 2026-06-12: schema-violating ids, fenced output).
    # The prompt demands JSON and _parse_contract strips fences.
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if not isinstance(data, dict) or "choices" not in data:
        raise RuntimeError(f"unexpected response shape: {str(data)[:120]}")
    msg = (data["choices"] or [{}])[0].get("message")
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return str(msg or "")


def _extract_json(raw: str) -> dict:
    s = (raw or "").strip()
    if "</think>" in s:
        s = s.split("</think>")[-1].strip()
    if "```" in s:
        for chunk in s.split("```"):
            chunk = chunk.removeprefix("json").strip()
            if chunk.startswith("{"):
                s = chunk
                break
    first, last = s.find("{"), s.rfind("}")
    if first != -1 and last > first:
        s = s[first:last + 1]
    return json.loads(s)


def _parse_contract(raw: str) -> dict:
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError(f"contract is not a JSON object: {type(data).__name__}")
    checklist = []
    for i, it in enumerate(data.get("checklist") or []):
        if isinstance(it, str):                       # bare-string item
            it = {"criterion": it}
        if not isinstance(it, dict):
            continue
        crit = str(it.get("criterion") or "").strip()
        if crit:
            checklist.append({"id": str(it.get("id") or f"C{i+1}"), "criterion": crit,
                              "verification": str(it.get("verification") or "").strip()})
    tests = []
    for i, t in enumerate(data.get("tests") or []):
        if not isinstance(t, dict):                   # can't trust a bare-string test
            continue
        cmd = str(t.get("command") or "").strip()
        if not cmd or "<" in cmd:  # "<...>" placeholders = non-deterministic, refuse
            continue
        try:
            expect_exit = int(t.get("expect_exit") or 0)
        except (TypeError, ValueError):
            expect_exit = 0
        tests.append({"id": str(t.get("id") or f"T{i+1}"), "command": cmd,
                      "expect_substring": str(t.get("expect_substring") or "").strip(),
                      "expect_exit": expect_exit})
    return {"checklist": checklist, "tests": tests}


import re as _re
import shutil as _shutil


def validate_tests(tests: list, stack: dict | None = None) -> tuple:
    """Mechanically reject tests that can NEVER pass (these deadlock the loop):
    unittest/pytest piped to grep without 2>&1, grep -L exit reliance, or a
    first binary that does not exist on this machine. Returns (kept, dropped)."""
    # Prefer the stack passed by the caller; fall back to the module global set
    # during generate_contract. (The python-for-cpp rule used to read only the
    # global, so a standalone health-check missed it — that let an
    # `import raytracing` test survive on a C++ goal.)
    eff_stack = stack or _STACK or {}
    kept, dropped = [], []
    for t in tests:
        cmd = t.get("command", "")
        reason = None
        if _re.search(r"(unittest|pytest)[^|]*\|", cmd) and "2>&1" not in cmd:
            reason = "test-runner output piped without 2>&1 (verbose goes to stderr)"
        elif "grep -L" in cmd or "grep --files-without-match" in cmd:
            reason = "grep -L exit semantics differ between GNU and BSD"
        elif cmd.strip().split()[0] == "find" and not (t.get("expect_substring") or "").strip():
            reason = "find always exits 0 — test cannot fail without expect_substring"
        elif any(w in cmd.lower() for w in ("locate", "find source", "copy ", " cp ", "/users/")):
            reason = "test assumes/searches/copies pre-existing files (hallucination loop) — forbidden"
        elif cmd.count("/tmp/") >= 1 and not any(tok in cmd for tok in ("cmake", "build", "make ", "./", ".venv")):
            reason = "trivial /tmp scratch test unrelated to the project's real artifacts"
        elif (eff_stack.get("language") == "cpp" and "import " in cmd and "python" in cmd
              and not any(tok in cmd for tok in ("open(", ".ppm", ".png", "build",
                                                 "subprocess", "os.path", "sys.argv"))):
            # Forbid the POISON pattern — `python3 -c "import raytracing"`, a bare
            # module-existence probe for a C++ goal — but NOT a legitimate python
            # script that READS a build artifact (e.g. parses render.ppm). The old
            # rule matched any "import", silently dropping valid verification and
            # neutering the contract → false completion.
            reason = "python module-existence test for a C++ goal (technology mismatch) — forbidden"
        elif cmd.strip() in ("true", ":") or _re.fullmatch(r"echo [^|;&]*", cmd.strip() or ""):
            reason = "no-op/echo test verifies nothing"
        else:
            first = cmd.strip().split()[0] if cmd.strip() else ""
            # Shell keywords / builtins are valid first tokens — they are NOT
            # binaries on PATH, so shutil.which() returns None for them. Without
            # this allowlist a compound test like `if ...; then ...; fi` was
            # silently DROPPED as "binary 'if' not found", which neutered the
            # contract (e.g. the render-variety check) and caused false goal
            # completion — the loop's recurring "exits after one batch" failure.
            _SHELL_KEYWORDS = {"cd", "test", "[", "command", "if", "then", "else",
                               "elif", "fi", "for", "while", "until", "case",
                               "do", "done", "{", "}", "(", "!", "[[", "echo",
                               "true", "false", "export", "local", "read", "set"}
            if (first and "/" not in first and first not in _SHELL_KEYWORDS
                    and _shutil.which(first) is None):
                reason = f"binary '{first}' not found on this machine"
        if reason:
            dropped.append({**t, "rejected": reason})
        else:
            kept.append(t)
    return kept, dropped


def generate_contract(title: str, description: str = "", user_prompt: str = "",
                      plan: str = "", lessons: str = "") -> dict:
    """Returns {"checklist": [...], "tests": [...], "auditor_model": "<who>"}.
    Tries each cloud model; degrades to local. `lessons` are injected so the
    auditor improves its contracts run-over-run."""
    global _STACK
    stack = detect_stack(f"{title} {description} {user_prompt}")
    _STACK = stack
    # KEYSTONE: deterministic technology-templated contract for known stacks —
    # fast, correct, incremental, and immune to the 2B/12B writing a
    # tech-mismatched or trivial contract. LLM only for unknown stacks.
    _tmpl = template_contract(stack, f"{title} {description}")
    if _tmpl.get("tests") and os.getenv("PGE_AUDITOR_FORCE_LLM") != "1":
        print(f"🛡️  Contract from template:{stack.get('language')} "
              f"({len(_tmpl['tests'])} incremental build/run tests).")
        return _tmpl
    prompt = _PROMPT.format(
        title=title, description=description or "(none)",
        user_prompt=user_prompt or description or title,
        plan=plan or "(no plan provided)",
        lessons=lessons or "(none recorded yet)",
        stack_hint=stack.get("test_hint") or "(stack not detected — infer from the request)")

    for model in AUDITOR_MODELS:
        try:
            raw = _post_chat(model, prompt, schema=CONTRACT_SCHEMA)
            c = _parse_contract(raw)
            if c["checklist"] and c["tests"]:
                print(f"🛡️  Dual contract by {model}: {len(c['checklist'])} criteria, "
                      f"{len(c['tests'])} tests")
                c["auditor_model"] = model
                return c
        except Exception as e:
            print(f"🛡️  Auditor model {model} unavailable ({str(e)[:90]}) — trying next")

    if CODEX_FALLBACK:
        try:
            raw = _codex_chat(prompt + "\n\nReturn ONLY the JSON object, no prose.")
            c = _parse_contract(raw)
            if c["checklist"] and c["tests"]:
                print(f"🛡️  Dual contract by codex/{CODEX_MODEL}: "
                      f"{len(c['checklist'])} criteria, {len(c['tests'])} tests")
                c["auditor_model"] = f"codex/{CODEX_MODEL}"
                return c
        except Exception as e:
            print(f"🛡️  Codex auditor fallback unavailable ({str(e)[:90]}) — degrading to local")

    try:
        from hermes_tools import llm
        raw = llm.generate(prompt, schema=CONTRACT_SCHEMA)
        c = _parse_contract(raw)
        if c["checklist"]:
            print("🛡️  Auditor degraded to LOCAL model")
            c["auditor_model"] = "local-fallback"
            return c
    except Exception as e:
        print(f"🛡️  Local auditor fallback failed too: {e}")
    return {"checklist": [], "subgoals": [], "tests": [], "auditor_model": "none"}


# Backwards-compatible alias used by pge_audit_goal
def generate_checklist(title: str, description: str = "", user_prompt: str = "") -> dict:
    c = generate_contract(title, description, user_prompt)
    return {"checklist": c["checklist"], "auditor_model": c["auditor_model"],
            "tests": c.get("tests", [])}


def checklist_to_criteria(checklist: list) -> list:
    return [f"[{it['id']}] {it['criterion']} || VERIFY: {it['verification']}"
            for it in checklist]


# --------------------------------------------------------------------------- #
# Proactive documentation research: when a goal names technologies/libraries,
# the auditor researches them ONCE, caches the docs in tools_db/web_docs, and
# every future run reuses them (no repeated fetching). This is what makes a
# small local model able to build against unfamiliar libraries safely.
# --------------------------------------------------------------------------- #
_STACK = {}
import re as _re2
import sqlite3 as _sql2
import subprocess as _sp2
from datetime import datetime as _dt2, timezone as _tz2

_WEB_DOCS_DB = forge_config.tooldocs_db_path()

# Stopwords + generic verbs we never research.
_RESEARCH_STOP = {
    "create", "build", "using", "with", "that", "your", "yourself", "have", "should",
    "along", "other", "required", "libraries", "library", "based", "also", "the", "and",
    "for", "from", "into", "make", "high", "fidelity", "features", "include", "dynamic",
    "project", "generation", "generate", "assets", "selecting", "time", "day", "map",
}


def detect_stack(text: str) -> dict:
    """Detect the goal's primary language/build system so the contract tests
    the RIGHT thing (a C++ goal must not get python import tests)."""
    t = (text or "").lower()
    stack = {"language": None, "build": None, "test_hint": "", "feature": None}
    # Feature detection: a goal that must produce a rendered image needs a
    # contract that VERIFIES the image, not just that the project compiles —
    # otherwise a 7-line `cout` stub satisfies a build-only contract (the
    # brooklyn trivial-completion). Detected here, enforced in template_contract.
    if any(w in t for w in ("raytrac", "ray trac", "render", "image", "ppm", "pixel",
                            "scene", "rasteriz", "framebuffer", "3d map", "3d ")):
        stack["feature"] = "render"
    if any(w in t for w in ("c++", "cpp", " c plus", "raytrac", "opengl", "vulkan", "cmake")):
        stack["language"] = "cpp"; stack["build"] = "cmake"
        stack["test_hint"] = ("C++/CMake project. Tests MUST verify real C++ artifacts: "
            "`test -f CMakeLists.txt`; the project CONFIGURES and BUILDS with "
            "`cmake -S . -B build && cmake --build build` (expect_exit 0); the built "
            "binary runs. NEVER use python imports, and NEVER test files under /tmp — "
            "test the project's own source/build outputs in the workspace.")
    elif any(w in t for w in ("python", "fastapi", "flask", "django", "pytest", ".py", "tkinter", "pandas", "numpy")):
        stack["language"] = "python"; stack["build"] = "pip"
        stack["test_hint"] = ("Python project. Tests run `python3 -m py_compile <file>` and "
            "`python3 -m pytest -q` or `python3 -m unittest -v 2>&1`. Dependencies install "
            "into ./.venv via install_deps; the venv is on PATH automatically.")
    elif any(w in t for w in ("node", "npm", "javascript", "typescript", "react", "express", ".js", ".ts")):
        stack["language"] = "node"; stack["build"] = "npm"
        stack["test_hint"] = "Node project. Tests run `npm test` or `node <file>`; deps via install_deps manager=npm."
    elif any(w in t for w in ("rust", "cargo", ".rs")):
        stack["language"] = "rust"; stack["build"] = "cargo"
        stack["test_hint"] = "Rust project. Tests run `cargo build` and `cargo test`."
    return stack


def template_contract(stack: dict, goal_text: str) -> dict:
    """Deterministic, technology-grounded contract for a known stack — fast,
    correct, satisfiable, and INCREMENTAL (file -> configures -> compiles ->
    runs). This replaces the weak/slow LLM for the keystone on known stacks;
    it can never emit a python-import test for a C++ goal. Returns {} for
    unknown stacks (LLM path then handles it)."""
    lang = (stack or {}).get("language")
    if lang == "cpp":
        checklist = [
            {"id": "C1", "criterion": "A CMakeLists.txt build file exists", "verification": "test -f CMakeLists.txt"},
            {"id": "C2", "criterion": "At least one C++ source file exists", "verification": "ls *.cpp src/*.cpp"},
            {"id": "C3", "criterion": "The project configures and compiles with CMake", "verification": "cmake -S . -B build && cmake --build build"},
            {"id": "C4", "criterion": "The built program runs without crashing", "verification": "run the built binary, exit 0"},
        ]
        tests = [
            {"id": "T1", "command": "test -f CMakeLists.txt && echo CMAKE_OK", "expect_substring": "CMAKE_OK", "expect_exit": 0},
            {"id": "T2", "command": "ls *.cpp src/*.cpp 2>/dev/null | head -1", "expect_substring": ".cpp", "expect_exit": 0},
            {"id": "T3", "command": "cmake -S . -B build >/dev/null 2>&1 && echo CONFIG_OK", "expect_substring": "CONFIG_OK", "expect_exit": 0},
            {"id": "T4", "command": "cmake --build build >/dev/null 2>&1 && echo BUILD_OK", "expect_substring": "BUILD_OK", "expect_exit": 0},
        ]
        if (stack or {}).get("feature") == "render":
            # Feature-level contract: the program must actually RENDER an image,
            # not just compile. Convention (also stated in the checklist so the
            # executor knows the target): running the built binary writes a PPM
            # to render.ppm in the project root; the image must be a real scene
            # (many distinct byte values), not a solid color a stub could print.
            checklist += [
                {"id": "C5", "criterion": "Running the built binary writes a PPM image to render.ppm in the project root",
                 "verification": "run binary, then test -f render.ppm"},
                {"id": "C6", "criterion": "render.ppm is a valid PPM (P3 or P6 header) at least 200x200",
                 "verification": "PPM magic + dimensions"},
                {"id": "C7", "criterion": "The rendered image is a real scene with varied pixels, not a solid fill",
                 "verification": ">16 distinct byte values in the pixel data"},
            ]
            tests += [
                {"id": "T5",
                 "command": ("cmake --build build >/dev/null 2>&1; "
                             "bin=$(find build -maxdepth 3 -type f -perm -111 "
                             "-not -name 'CMake*' -not -name '*.cmake' -not -name '*.sh' 2>/dev/null | head -1); "
                             "rm -f render.ppm; [ -n \"$bin\" ] && \"$bin\" >/dev/null 2>&1; "
                             "test -f render.ppm && echo RENDER_OK"),
                 "expect_substring": "RENDER_OK", "expect_exit": 0},
                {"id": "T6", "command": "head -c2 render.ppm 2>/dev/null | grep -qE 'P[36]' && echo PPM_OK",
                 "expect_substring": "PPM_OK", "expect_exit": 0},
                {"id": "T7",
                 # Measure PIXEL-value variety, format-agnostic, PURE SHELL (no
                 # python — a python `import` test gets stripped by the quality
                 # gate for a C++ goal, which silently neutered this check and
                 # caused false completion). od -tu1 raw-byte variety is
                 # unsatisfiable for a P3 (ASCII) PPM (~13 distinct bytes however
                 # varied) yet T6 permits P3. So: P6 => distinct raw body bytes;
                 # P3 => distinct whitespace-separated integer pixel tokens.
                 "command": ("if head -c2 render.ppm | grep -q P6; then "
                             "N=$(od -An -tu1 render.ppm | tr -s ' ' '\\n' | grep -v '^$' | sort -u | wc -l); "
                             "else "
                             "N=$(sed '1,3d' render.ppm | tr -cs '0-9' '\\n' | grep -v '^$' | sort -u | wc -l); "
                             "fi; test \"$N\" -gt 16 && echo VARIED"),
                 "expect_substring": "VARIED", "expect_exit": 0},
            ]
            return {"checklist": checklist, "tests": tests, "auditor_model": "template:cpp-render"}
        return {"checklist": checklist, "tests": tests, "auditor_model": "template:cpp-cmake"}
    if lang == "python":
        return {"checklist": [
            {"id": "C1", "criterion": "A Python entry file exists and compiles", "verification": "py_compile"},
            {"id": "C2", "criterion": "The test suite passes", "verification": "pytest/unittest"}],
            "tests": [
            {"id": "T1", "command": "ls *.py | head -1", "expect_substring": ".py", "expect_exit": 0},
            {"id": "T2", "command": "for f in *.py; do python3 -m py_compile \"$f\" || exit 1; done; echo COMPILE_OK", "expect_substring": "COMPILE_OK", "expect_exit": 0},
            {"id": "T3", "command": "python3 -m pytest -q 2>&1 | tail -3 || python3 -m unittest discover -v 2>&1 | tail -3", "expect_substring": "", "expect_exit": 0}],
            "auditor_model": "template:python"}
    if lang == "node":
        return {"checklist": [
            {"id": "C1", "criterion": "package.json exists", "verification": "test -f package.json"},
            {"id": "C2", "criterion": "npm test passes", "verification": "npm test"}],
            "tests": [
            {"id": "T1", "command": "test -f package.json && echo PKG_OK", "expect_substring": "PKG_OK", "expect_exit": 0},
            {"id": "T2", "command": "npm test 2>&1 | tail -5", "expect_substring": "", "expect_exit": 0}],
            "auditor_model": "template:node"}
    if lang == "rust":
        return {"checklist": [
            {"id": "C1", "criterion": "Cargo project builds", "verification": "cargo build"},
            {"id": "C2", "criterion": "cargo test passes", "verification": "cargo test"}],
            "tests": [
            {"id": "T1", "command": "cargo build 2>&1 | tail -3 && echo BUILD_OK", "expect_substring": "BUILD_OK", "expect_exit": 0},
            {"id": "T2", "command": "cargo test 2>&1 | tail -3", "expect_substring": "", "expect_exit": 0}],
            "auditor_model": "template:rust"}
    return {}


def _research_terms(text: str, limit: int = 4) -> list:
    """Extract candidate technology/library terms from a goal description."""
    words = _re2.findall(r"[A-Za-z][A-Za-z0-9_+.\-]{2,}", text or "")
    # Prefer capitalized / library-looking tokens (C++, OptiX, Embree, tkinter...)
    cand, seen = [], set()
    for w in words:
        lw = w.lower()
        if lw in _RESEARCH_STOP or len(w) < 3:
            continue
        looks_tech = (w[0].isupper() or "+" in w or "." in w or any(c.isdigit() for c in w)
                      or lw in {"raytracing", "raytracer", "opengl", "vulkan", "cmake", "tkinter"})
        if looks_tech and lw not in seen:
            seen.add(lw); cand.append(w)
        if len(cand) >= limit:
            break
    return cand


def _already_cached(con, term: str) -> bool:
    try:
        return con.execute("SELECT 1 FROM web_docs WHERE url LIKE ? LIMIT 1",
                           (f"%research:{term.lower()}%",)).fetchone() is not None
    except _sql2.Error:
        return False


def research_and_cache(goal_text: str, max_terms: int = 4) -> str:
    """Search the web for docs on the goal's technologies, cache them in
    web_docs (keyed research:<term>), return a compact snippet bundle.
    Best-effort: never raises; returns '' on any failure."""
    try:
        con = _sql2.connect(_WEB_DOCS_DB, timeout=4)
        con.execute("""CREATE TABLE IF NOT EXISTS web_docs (
            url TEXT PRIMARY KEY, content TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
        terms = _research_terms(goal_text, max_terms)
        snippets = []
        for term in terms:
            key = f"research:{term.lower()}"
            row = con.execute("SELECT content FROM web_docs WHERE url=?", (key,)).fetchone()
            if row:
                snippets.append(f"[{term}] {row[0][:400]}")
                continue
            text = ""
            try:
                from ddgs import DDGS
                with DDGS() as d:
                    hits = list(d.text(f"{term} official documentation", max_results=2))
                # fetch the first result body via curl, cache it
                if hits:
                    url = hits[0].get("href") or hits[0].get("url") or ""
                    body = hits[0].get("body", "")
                    if url:
                        try:
                            r = _sp2.run(["curl", "-sL", "--max-time", "20", "--max-filesize",
                                          "1500000", url], capture_output=True, text=True, timeout=25)
                            page = r.stdout or ""
                            page = _re2.sub(r"<script.*?</script>|<style.*?</style>", " ", page,
                                            flags=_re2.S | _re2.I)
                            page = _re2.sub(r"<[^>]+>", " ", page)
                            page = _re2.sub(r"\s+", " ", page).strip()
                            text = (f"DOC URL: {url}\n" + (page[:4000] or body))
                        except Exception:
                            text = f"DOC URL: {url}\n{body}"
            except Exception as se:
                text = f"(search unavailable for {term}: {str(se)[:60]})"
            if text:
                con.execute("INSERT OR REPLACE INTO web_docs(url,content,fetched_at) VALUES (?,?,?)",
                            (key, text[:8000], _dt2.now(_tz2.utc).isoformat()))
                con.commit()
                snippets.append(f"[{term}] {text[:400]}")
                print(f"📚 Auditor researched & cached docs for: {term}")
        con.close()
        return "\n".join(snippets)[:2500]
    except Exception as e:
        print(f"📚 research_and_cache failed (non-fatal): {e}")
        return ""
