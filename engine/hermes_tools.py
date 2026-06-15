import os
import json
import urllib.request
from openai import OpenAI
from app.database import SessionLocal
from app.services import MemoryService

# ---------------------------------------------------------------------------
# Local LM Studio client
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")

client = OpenAI(
    api_key="not-needed",
    base_url=BASE_URL,
    timeout=300.0,
)


def _detect_model() -> str:
    """Resolve the model id to use.

    Priority: env LLM_MODEL -> first non-embedding model loaded in LM Studio.
    This avoids the hard-coded '-qat' mismatch that produced 400 'terminated'
    whenever a different Gemma variant was loaded.
    """
    env = os.getenv("LLM_MODEL")
    if env:
        return env
    try:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=10) as r:
            data = json.load(r)["data"]
        for m in data:
            if "embedding" not in m["id"].lower():
                return m["id"]
    except Exception as e:
        print(f"Model auto-detect failed ({e}); falling back to default.")
    return "google/gemma-4-12b-qat"


MODEL = _detect_model()
print(f"[hermes_tools] Using LLM model: {MODEL}")

# ---------------------------------------------------------------------------
# JSON schemas for grammar-constrained structured output
# ---------------------------------------------------------------------------
PLANNER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "planner_output",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "new_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "priority": {"type": "integer"},
                        },
                        "required": ["title", "description", "priority"],
                    },
                }
            },
            "required": ["new_tasks"],
        },
    },
}

EVALUATOR_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "evaluator_output",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {"type": "string", "enum": ["complete", "blocked", "continue"]},
                "task_completed": {"type": "boolean"},
                "reason": {"type": "string"},
                "missing_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Contract checklist item ids ([C1]...) NOT yet proven by evidence",
                },
            },
            "required": ["decision", "task_completed", "reason", "missing_items"],
        },
    },
}

# Executor emits EITHER a tool_call OR a heartbeat. A single permissive object
# (strict disabled) lets the model fill the relevant fields while still being
# guaranteed to return a valid JSON object.
EXECUTOR_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "executor_action",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["tool_call", "heartbeat"]},
                "name": {
                    "type": "string",
                    "enum": [
                        "write_file",
                        "read_file",
                        "run_command",
                        "notebook_cell",
                    ],
                },
                "arguments": {"type": "object"},
                "progress_summary": {"type": "string"},
                "next_task_description": {"type": "string"},
                "blocker": {"type": ["string", "null"]},
                "resume_instruction": {"type": "string"},
            },
            "required": ["type"],
        },
    },
}


class LLM:
    """Local LLM wrapper.

    Every call disables the model's chain-of-thought (``reasoning_effort:
    "none"``) and, when a schema is supplied, constrains the output to valid
    JSON via ``response_format``. Gemma 4 is a *thinking* model: without
    reasoning disabled it spends the entire token budget on CoT and never emits
    the JSON the PGE nodes require -- which is exactly why the loop stalled.
    """

    def _create_with_retry(self, messages, schema=None, max_tokens=4096, attempts=3):
        """Survive transient backend stalls (compute contention from concurrent
        big prefills). Final failure still raises — node-level guards convert
        it into a failed turn instead of a crashed run."""
        import time as _time
        last = None
        for i in range(attempts):
            try:
                return self._create(messages, schema=schema, max_tokens=max_tokens)
            except Exception as e:
                last = e
                wait = 15 * (i + 1)
                print(f"[hermes_tools] LLM attempt {i+1}/{attempts} failed ({str(e)[:80]}); retry in {wait}s")
                _time.sleep(wait)
        raise last

    def __init__(self, model: str | None = None):
        self.model = model or MODEL

    def _create(self, messages, schema=None, max_tokens=4096, reasoning="none"):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=0.1,
            max_tokens=max_tokens,
            extra_body={"reasoning_effort": reasoning},
        )
        if schema is not None:
            kwargs["response_format"] = schema
        return client.chat.completions.create(**kwargs)

    def generate(self, prompt: str, schema=None) -> str:
        try:
            response = self._create_with_retry([{"role": "user", "content": prompt}], schema=schema)
            return response.choices[0].message.content or ""
        except Exception as e:
            print(f"Error calling local LLM: {e}")
            raise e

    def reason(self, messages: list, max_tokens: int = 700) -> str:
        """Free-form bounded reasoning pass (no schema). The output is prose
        meant to be fed back into a strict ACT call — never parsed as JSON."""
        try:
            r = self._create_with_retry(messages, schema=None,
                                        max_tokens=max_tokens)
            txt = r.choices[0].message.content or ""
            return txt.split("</think>")[-1].strip()[:2500]
        except Exception as e:
            print(f"[hermes_tools] reason() unavailable: {e}")
            return ""

    def generate_chat(self, messages: list, schema=None) -> str:
        try:
            response = self._create_with_retry(messages, schema=schema)
            return response.choices[0].message.content or ""
        except Exception as e:
            print(f"Error calling local LLM chat: {e}")
            raise e

    def generate_json(self, prompt: str, schema, max_tokens=4096) -> dict:
        """Return a parsed dict. Raises on unparseable output."""
        raw = ""
        try:
            response = self._create(
                [{"role": "user", "content": prompt}], schema=schema, max_tokens=max_tokens
            )
            raw = response.choices[0].message.content or ""
            return json.loads(raw)
        except json.JSONDecodeError:
            return _salvage_json(raw)
        except Exception as e:
            print(f"Error calling local LLM (json): {e}")
            raise e


def _salvage_json(raw: str) -> dict:
    """Best-effort recovery if the model ever returns stray text around JSON."""
    s = (raw or "").strip()
    if "</think>" in s:
        s = s.split("</think>")[-1].strip()
    if "```json" in s:
        s = s.split("```json")[1].split("```")[0].strip()
    elif "```" in s:
        s = s.split("```")[1].split("```")[0].strip()
    first, last = s.find("{"), s.rfind("}")
    if first != -1 and last != -1 and last > first:
        s = s[first : last + 1]
    return json.loads(s)


llm = LLM()
planner_llm = LLM(os.getenv("PGE_PLANNER_MODEL") or MODEL)
executor_llm = LLM(os.getenv("PGE_EXECUTOR_MODEL") or MODEL)


def mcp_hermes_memory_create_checkpoint(project_id: str, summary: str, current_state_json: str, next_actions_json: str) -> str:
    db = SessionLocal()
    try:
        service = MemoryService(db)
        state = json.loads(current_state_json)
        actions = json.loads(next_actions_json)
        # Create the checkpoint in Postgres
        checkpoint = service.create_checkpoint(
            project_id=project_id,
            summary=summary,
            current_state=state,
            next_actions=actions
        )
        return json.dumps({
            "status": "success",
            "message": "Checkpoint created",
            "checkpoint_id": str(checkpoint.id)
        })
    except Exception as e:
        print(f"Error creating checkpoint: {e}")
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        db.close()
