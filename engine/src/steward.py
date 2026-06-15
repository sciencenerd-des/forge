"""Context Steward — the local-first small brain (gemma-4-e2b on LM Studio MLX).

Responsibilities:
1. compact_context(project_id): query Postgres, filter stale context, and
   return a COMPACT context pack string for the executor — instead of dumping
   raw decisions/runtime state that bloats prompts and confuses the 12B.
2. steering(...): at the END of each loop cycle, look at the DB + fresh test
   results and emit a 2-3 sentence directive that steers the next cycle
   toward the contract and the project goal.

Every call is best-effort: on any failure the loop continues with a
deterministic fallback (empty string / raw truncation), never blocks.
"""
import json
import urllib.request

STEWARD_URL = "http://127.0.0.1:1234/v1"
# Model is env-driven so the whole loop can run single-model (all nodes on the
# 12B) or split-brain (steward on the small e2b). Defaults to the same model the
# other nodes use via LLM_MODEL, falling back to the 12B.
import os
STEWARD_MODEL = (os.getenv("PGE_STEWARD_MODEL")
                 or os.getenv("LLM_MODEL")
                 or "google/gemma-4-12b-qat")


def _chat(prompt: str, max_tokens: int = 400, timeout: int = 90) -> str:
    body = {"model": STEWARD_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1, "max_tokens": max_tokens,
            "reasoning_effort": "none"}
    req = urllib.request.Request(f"{STEWARD_URL}/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip()


def _db_snapshot(project_id: str) -> dict:
    """Fresh, minimal DB state — queried live, never cached."""
    from app.database import SessionLocal
    from app.models import HermesGoal, HermesTask, HermesFileChange, HermesTestRun
    db = SessionLocal()
    try:
        g = db.query(HermesGoal).filter(HermesGoal.project_id == project_id).first()
        tasks = db.query(HermesTask).filter(HermesTask.project_id == project_id).all()
        fcs = (db.query(HermesFileChange).filter(HermesFileChange.project_id == project_id)
               .order_by(HermesFileChange.created_at.desc()).limit(5).all())
        trs = (db.query(HermesTestRun).filter(HermesTestRun.project_id == project_id)
               .order_by(HermesTestRun.created_at.desc()).limit(5).all())
        return {
            "goal": {"title": g.title, "status": g.status,
                     "criteria": g.success_criteria or []} if g else None,
            "tasks": [{"title": t.title, "status": t.status} for t in tasks][:20],
            "recent_files": [f"{f.file_path}: {f.change_summary}" for f in fcs],
            "recent_tests": [f"{t.command} -> {t.status}" for t in trs],
        }
    finally:
        db.close()


def _cached_docs_for(task_title: str, limit: int = 2) -> str:
    """Knowledge reuse: snippets from web docs previously fetched via fetch_doc
    (tools_db/web_docs) that match the current task — fetched once from the
    internet, used for every future task."""
    try:
        import sqlite3
        import forge_config
        con = sqlite3.connect(forge_config.tooldocs_db_path(), timeout=2)
        words = [w for w in task_title.lower().split() if len(w) > 4][:3]
        snippets = []
        for w in words:
            row = con.execute("SELECT url, substr(content,1,400) FROM web_docs "
                              "WHERE content LIKE ? LIMIT 1", (f"%{w}%",)).fetchone()
            if row:
                snippets.append(f"[cached doc {row[0]}] {row[1]}")
            if len(snippets) >= limit:
                break
        con.close()
        return "\n".join(snippets)
    except Exception:
        return ""


_BRIEF_CACHE = {}  # (project_id, task_title) -> (fingerprint, briefing)


def _state_fingerprint(snap: dict) -> str:
    """Cheap signature of the project state — changes only when tasks, files,
    or test runs actually change."""
    import hashlib
    sig = json.dumps({"t": snap.get("tasks"), "f": snap.get("recent_files"),
                      "r": snap.get("recent_tests")}, sort_keys=True, default=str)
    return hashlib.md5(sig.encode()).hexdigest()


def compact_context(project_id: str, task_title: str = "", budget_words: int = 180) -> str:
    """Compact, stale-filtered context for the executor prompt.
    Cached: the e2b briefing is regenerated ONLY when the project state
    fingerprint changes — identical successive turns reuse it for free."""
    try:
        snap = _db_snapshot(project_id)
        fp = _state_fingerprint(snap)
        ck = (project_id, task_title)
        cached = _BRIEF_CACHE.get(ck)
        if cached and cached[0] == fp:
            return cached[1]
        docs = _cached_docs_for(task_title)
        if docs:
            snap["cached_documentation"] = docs[:900]
        raw = json.dumps(snap, default=str)[:6000]
        out = _chat(
            f"You are a context steward for a coding agent. Current task: {task_title or '(unknown)'}.\n"
            f"Raw project state (JSON):\n{raw}\n\n"
            f"Write a compact briefing (max {budget_words} words) containing ONLY what the agent "
            "needs for the CURRENT task: the goal in one line, which contract criteria are unmet, "
            "what was recently done (files/tests), and what to avoid repeating. Drop everything "
            "stale or irrelevant. Plain text, no preamble.")
        if "BRIEFING:" in out:
            out = out.split("BRIEFING:", 1)[1].strip()
        out = out[:2000]
        _BRIEF_CACHE[ck] = (fp, out)
        return out
    except Exception as e:
        print(f"🧑‍✈️ Steward compact_context unavailable ({str(e)[:60]}) — using raw fallback.")
        try:
            snap = _db_snapshot(project_id)
            return json.dumps(snap, default=str)[:1500]
        except Exception:
            return ""


def steering(project_id: str, test_results: list, last_reason: str = "") -> str:
    """End-of-cycle directive: where should the loop go next."""
    try:
        snap = _db_snapshot(project_id)
        tests = "; ".join(f"{t['id']}:{'PASS' if t['passed'] else 'FAIL ' + t['output'][-120:]}"
                          for t in (test_results or []))
        out = _chat(
            "You steer an autonomous coding loop. Fresh independent test results this cycle: "
            f"{tests or '(none)'}\nEvaluator reason: {last_reason[:300]}\n"
            f"Project state: {json.dumps(snap, default=str)[:3000]}\n\n"
            "In 2-3 short sentences, direct the next cycle: the single most important thing to do, "
            "what NOT to touch (passing tests / working files), and whether any task is a dead end. "
            "Output EXACTLY one paragraph starting with 'DIRECTIVE:' and nothing else — "
            "no analysis, no headings, no restating the input.")
        if "DIRECTIVE:" in out:
            out = out.split("DIRECTIVE:", 1)[1].strip()
        return out[:600]
    except Exception as e:
        print(f"🧑‍✈️ Steward steering unavailable ({str(e)[:60]}).")
        return ""
