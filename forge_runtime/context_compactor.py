"""Threshold-triggered context autocompaction + Ollama KV-cache pruning.

Two distinct memory-pressure problems, one module:

1. PROMPT BLOAT — the executor transcript (system pack + tool results) grows
   within a batch. Past a token threshold every extra token slows TTFT and
   dilutes attention without adding signal. ``compact_messages`` folds the
   OLDER middle of the transcript into one salvage summary that keeps only
   the load-bearing lines (errors, commands, file paths, verdicts), always
   preserving the first (system) message and the most recent exchanges.
   Deterministic — no LLM call — so compaction can never fail or stall a turn.

2. KV-CACHE GROWTH — at num_ctx=200k the Ollama server's KV cache grows with
   the longest prompt seen and never shrinks (observed 7.7GB -> 17GB over a
   run, ending in host swap exhaustion). ``prune_ollama_kv`` asks the daemon
   to unload the model (keep_alive=0), releasing the cache; the next request
   reloads the weights (~seconds) with a fresh, small KV allocation. num_ctx
   itself is untouched — the model can still use the full window afterwards.

Env knobs:
    PGE_CONTEXT_COMPACT_THRESHOLD  tokens before compaction kicks in (default 12000)
    PGE_CONTEXT_COMPACT_KEEP       recent messages always kept verbatim (default 6)
    PGE_KV_PRUNE_INTERVAL          prune every N batches; 0 disables (default 4)
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

# ~4 chars/token holds well enough for mixed English/code to gate a threshold.
_CHARS_PER_TOKEN = 4

# Lines worth keeping when a transcript chunk is folded away: failure
# evidence, actions taken, and artifacts touched. Everything else in an old
# tool result is replayable noise.
_SALVAGE_RE = re.compile(
    r"(error|fail|exception|traceback|assert|warning|missing|denied|refused"
    r"|exit code|returncode|not found|no such"
    r"|passed|ok\b|complete"
    r"|\btool_call\b|\"name\":"
    r"|[\w/.-]+\.(?:py|rs|cpp|c|h|hpp|js|ts|json|toml|txt|md|csv|ppm|cmake))",
    re.IGNORECASE,
)


def estimate_tokens(messages: list[dict[str, Any]] | str) -> int:
    if isinstance(messages, str):
        return len(messages) // _CHARS_PER_TOKEN
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, default=str))
    return total // _CHARS_PER_TOKEN


def _salvage(text: str, max_lines: int = 40) -> str:
    """Keep only the load-bearing lines of a folded-away message."""
    hits = [ln.strip() for ln in text.splitlines()
            if ln.strip() and _SALVAGE_RE.search(ln)]
    if not hits:
        # Nothing matched: keep head+tail so the fold is never a total blank.
        lines = [ln for ln in text.splitlines() if ln.strip()]
        hits = lines[:3] + (["..."] if len(lines) > 6 else []) + lines[-3:]
    if len(hits) > max_lines:
        hits = hits[: max_lines // 2] + ["..."] + hits[-max_lines // 2:]
    return "\n".join(hits)


def compact_messages(messages: list[dict[str, Any]],
                     threshold_tokens: int | None = None,
                     keep_recent: int | None = None,
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fold the older middle of a transcript into one salvage summary.

    Returns (messages, report). When under the threshold the input list is
    returned unchanged and report["compacted"] is False. The first message
    (system pack) and the last ``keep_recent`` messages survive verbatim —
    the model keeps its instructions and its working set; only stale
    intermediate tool chatter is folded.
    """
    if threshold_tokens is None:
        threshold_tokens = int(os.getenv("PGE_CONTEXT_COMPACT_THRESHOLD", "12000"))
    if keep_recent is None:
        keep_recent = int(os.getenv("PGE_CONTEXT_COMPACT_KEEP", "6"))

    before = estimate_tokens(messages)
    report = {"compacted": False, "tokens_before": before, "tokens_after": before}
    if before <= threshold_tokens or len(messages) <= 1 + keep_recent + 1:
        return messages, report

    head, middle, tail = messages[0], messages[1:-keep_recent], messages[-keep_recent:]
    folded_parts = []
    for m in middle:
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        folded_parts.append(f"[{m.get('role', '?')}] {_salvage(content)}")
    summary = {
        "role": "user",
        "content": ("CONTEXT COMPACTED (older turns folded; key evidence retained):\n"
                    + "\n".join(folded_parts)),
    }
    out = [head, summary, *tail]
    report.update(compacted=True, folded_messages=len(middle),
                  tokens_after=estimate_tokens(out))
    return out, report


# ---------------------------------------------------------------------------
# Ollama KV-cache pruning
# ---------------------------------------------------------------------------

def _ollama_native_base() -> str:
    """Derive the native Ollama API base from the OpenAI-compatible LLM_BASE_URL
    (strip a trailing /v1), overridable via OLLAMA_HOST."""
    host = os.getenv("OLLAMA_HOST")
    if host:
        return host.rstrip("/")
    base = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def prune_ollama_kv(model: str | None = None, timeout: int = 30) -> bool:
    """Unload ``model`` from the Ollama daemon (keep_alive=0), releasing its
    KV cache. Returns True on success; never raises — pruning is an
    optimization and must not take the loop down with it."""
    model = model or os.getenv("LLM_MODEL", "")
    if not model:
        return False
    try:
        req = urllib.request.Request(
            f"{_ollama_native_base()}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"KV prune skipped ({type(exc).__name__}: {exc})")
        return False


def kv_prune_interval() -> int:
    """Batches between prunes; 0 disables."""
    try:
        return max(0, int(os.getenv("PGE_KV_PRUNE_INTERVAL", "4")))
    except ValueError:
        return 4
