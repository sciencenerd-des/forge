"""Headroom compression for goal-scoped PGE context packs.

Authority-bearing fields are never compressed. Bulk retrieval context is
compressed and both forms are persisted in PostgreSQL for audit/replay.
"""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from typing import Any

from .models import HermesContextCompressionSnapshot

PROTECTED_KEYS = {
    "PROJECT",
    "ACTIVE_TASK",
    "NON_NEGOTIABLE_CONSTRAINTS",
    "DECISIONS_ALREADY_MADE",
    "MEMORY_EVIDENCE",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compress_context_pack(db, *, project_id: str, goal_id: str | None,
                          task_id: str | None, pack: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("PGE_HEADROOM_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return pack

    bulk = {key: value for key, value in pack.items() if key not in PROTECTED_KEYS}
    source_hash = hashlib.sha256(_stable_json(bulk).encode("utf-8")).hexdigest()

    try:
        import headroom
        from headroom import compress

        version = f"{getattr(headroom, '__version__', 'unknown')}:pge-fields-v2"
        existing = db.query(HermesContextCompressionSnapshot).filter(
            HermesContextCompressionSnapshot.project_id == project_id,
            HermesContextCompressionSnapshot.goal_id == goal_id,
            HermesContextCompressionSnapshot.task_id == task_id,
            HermesContextCompressionSnapshot.source_hash == source_hash,
            HermesContextCompressionSnapshot.compressor_version == version,
        ).order_by(HermesContextCompressionSnapshot.created_at.desc()).first()
        if existing:
            compressed_bulk = existing.compressed_context
            snapshot = existing
        else:
            query = (
                f"Retain only facts needed for goal {pack.get('PROJECT', {}).get('goal', '')!r} "
                f"and active task {pack.get('ACTIVE_TASK', {}).get('title', '')!r}. "
                "Preserve failures, blockers, file paths, commands, and next actions."
            )
            compressed_bulk = {}
            tokens_before = tokens_after = tokens_saved = 0
            transforms = []
            for index, (key, value) in enumerate(bulk.items()):
                call_id = f"context_pack_{index}"
                messages = [
                    {"role": "user", "content": f"{query} Context field: {key}."},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {"name": "load_postgres_context", "arguments": "{}"},
                    }]},
                    # Headroom 0.24 SmartCrusher recognizes normal JSON output;
                    # compact separators currently bypass its JSON router.
                    {"role": "tool", "tool_call_id": call_id,
                     "content": json.dumps(value, sort_keys=True, default=str)},
                ]
                result = compress(
                    messages,
                    model=os.getenv("PGE_HEADROOM_MODEL", "gpt-4o"),
                    model_limit=int(os.getenv("PGE_HEADROOM_MODEL_LIMIT", "65536")),
                    protect_recent=0,
                    min_tokens_to_compress=int(os.getenv("PGE_HEADROOM_MIN_TOKENS", "80")),
                    kompress_model=os.getenv("PGE_HEADROOM_KOMPRESS_MODEL", "disabled"),
                )
                compressed_text = result.messages[-1].get("content") or "null"
                compressed_bulk[key] = json.loads(compressed_text)
                tokens_before += result.tokens_before
                tokens_after += result.tokens_after
                tokens_saved += result.tokens_saved
                transforms.extend(result.transforms_applied)

            snapshot = HermesContextCompressionSnapshot(
                project_id=project_id,
                goal_id=goal_id,
                task_id=task_id,
                source_hash=source_hash,
                source_memory_ids=[item["id"] for item in pack.get("MEMORY_EVIDENCE", [])],
                raw_context=bulk,
                compressed_context=compressed_bulk,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tokens_saved=tokens_saved,
                compression_ratio=(tokens_saved / tokens_before if tokens_before else 0),
                transforms_applied=sorted(set(transforms)),
                compressor_version=version,
            )
            db.add(snapshot)
            db.commit()
            db.refresh(snapshot)

        output = deepcopy(pack)
        output.update(compressed_bulk)
        output["CONTEXT_COMPRESSION"] = {
            "snapshot_id": snapshot.id,
            "source_hash": source_hash,
            "compressor": f"headroom-ai/{snapshot.compressor_version}",
            "tokens_before": snapshot.tokens_before,
            "tokens_after": snapshot.tokens_after,
            "tokens_saved": snapshot.tokens_saved,
            "compression_ratio": float(snapshot.compression_ratio or 0),
            "transforms": snapshot.transforms_applied or [],
            "reversible_from_postgres": True,
        }
        return output
    except Exception as exc:
        output = deepcopy(pack)
        output["CONTEXT_COMPRESSION"] = {
            "status": "fallback_uncompressed",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "source_hash": source_hash,
        }
        return output
