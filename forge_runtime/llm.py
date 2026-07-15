"""Lazy, role-aware OpenAI-compatible client factory."""
from __future__ import annotations

import json
import os
import urllib.request
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

import forge_config
from app.database import SessionLocal
from app.services import MemoryService

PLANNER_SCHEMA = {
    "type": "json_schema", "json_schema": {"name": "planner_output", "strict": True,
    "schema": {"type": "object", "additionalProperties": False, "properties": {
        "new_tasks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "priority": {"type": "integer"}},
        "required": ["title", "description", "priority"]}}}, "required": ["new_tasks"]}}}
EVALUATOR_SCHEMA = {
    "type": "json_schema", "json_schema": {"name": "evaluator_output", "strict": True,
    "schema": {"type": "object", "additionalProperties": False, "properties": {
        "decision": {"type": "string", "enum": ["complete", "blocked", "continue"]},
        "task_completed": {"type": "boolean"}, "reason": {"type": "string"},
        "missing_items": {"type": "array", "items": {"type": "string"}}},
        "required": ["decision", "task_completed", "reason", "missing_items"]}}}
EXECUTOR_SCHEMA = {"type": "json_schema", "json_schema": {"name": "executor_action", "strict": False,
    "schema": {"type": "object", "properties": {"type": {"type": "string", "enum": ["tool_call", "heartbeat"]},
    "name": {"type": "string", "enum": ["write_file", "read_file", "run_command", "notebook_cell"]},
    "arguments": {"type": "object"}, "progress_summary": {"type": "string"},
    "next_task_description": {"type": "string"}, "blocker": {"type": ["string", "null"]},
    "resume_instruction": {"type": "string"}}, "required": ["type"]}}}


def extract_json(raw: str) -> str:
    """Extract one JSON object from model prose, thinking tags, or fences.

    Small local models (e.g. Gemma on Ollama) frequently wrap the real object
    in junk — a leading ``json`` token, a `````json`` fence, or even a
    fake outer ``{"`` before the fence (``json\\n{"```json\\n{...}`````).
    When a fence is present anywhere it holds the true payload, so it is tried
    before the brace-window fallback.
    """
    text = (raw or "").strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1].strip()
    fence = text.find("```")
    if fence != -1:
        inner = text[fence + 3:]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        inner = inner.lstrip("\n")
        end = inner.find("```")
        if end != -1:
            inner = inner[:end]
        first, last = inner.find("{"), inner.rfind("}")
        if 0 <= first < last:
            return inner[first:last + 1]
    text = text.removeprefix("json").strip()
    first, last = text.find("{"), text.rfind("}")
    if first < 0 or last <= first:
        raise ValueError("model response did not contain a JSON object")
    return text[first:last + 1]


def _schema_prompt(schema: dict[str, Any]) -> str:
    body = schema.get("json_schema", {}).get("schema", schema)
    name = schema.get("json_schema", {}).get("name", "response")
    properties = ", ".join(body.get("properties", {}).keys())
    required = ", ".join(body.get("required", []))
    example = {key: ("tool_call" if key == "type" else "") for key in body.get("properties", {})}
    return (f"Return exactly one JSON object named {name}. Keys: {properties}. "
            f"Required keys: {required}. No markdown, prose, or thinking in the JSON response. "
            f"Example shape: {json.dumps(example)}")


def detect_model(base_url: str, fallback: str) -> str:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=10) as response:
            for item in json.load(response).get("data", []):
                model = str(item.get("id", ""))
                if model and "embedding" not in model.lower():
                    return model
    except (OSError, ValueError):
        pass
    return fallback


@lru_cache(maxsize=32)
def client_for(role: str = "general") -> OpenAI:
    profile = forge_config.provider_for(role)
    model = profile["model"]
    if model == "auto":
        model = detect_model(profile["base_url"], forge_config.DEFAULT_MODEL)
    client = OpenAI(api_key=profile["api_key"], base_url=profile["base_url"], timeout=profile["timeout"])
    client._forge_model = model  # type: ignore[attr-defined]
    return client


def invalidate_clients() -> None:
    client_for.cache_clear()


class LLM:
    def __init__(self, role: str = "general"):
        self.role = role

    @property
    def model(self) -> str:
        return getattr(client_for(self.role), "_forge_model")

    def _create(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None = None, max_tokens: int = 4096):
        if forge_config.llm_dialect(self.role) == "ollama":
            request_messages = list(messages)
            if schema is not None:
                request_messages.append({"role": "system", "content": _schema_prompt(schema)})
            profile = forge_config.provider_for(self.role)
            base_url = profile["base_url"].rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            body: dict[str, Any] = {"model": self.model, "messages": request_messages,
                                    "stream": False, "options": {"temperature": 0.1, "num_predict": max_tokens}}
            if schema is not None:
                body["format"] = "json"
            payload = json.dumps(body).encode()
            request = urllib.request.Request(f"{base_url}/api/chat", data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {profile['api_key']}"})
            with urllib.request.urlopen(request, timeout=profile["timeout"]) as response:
                result = json.load(response)
            content = result.get("message", {}).get("content", "")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1,
                                  "max_tokens": max_tokens}
        reasoning_effort = os.getenv("FORGE_LLM_REASONING_EFFORT")
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
        if schema is not None:
            kwargs["response_format"] = schema
        return client_for(self.role).chat.completions.create(**kwargs)

    def generate_chat(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None = None) -> str:
        return self._create(messages, schema=schema).choices[0].message.content or ""

    def generate(self, prompt: str, schema: dict[str, Any] | None = None) -> str:
        return self.generate_chat([{"role": "user", "content": prompt}], schema)

    def generate_json(self, prompt: str, schema: dict[str, Any], max_tokens: int = 4096) -> dict[str, Any]:
        raw = self._create([{"role": "user", "content": prompt}], schema, max_tokens).choices[0].message.content or ""
        return json.loads(extract_json(raw))

    def reason(self, messages: list[dict[str, Any]], max_tokens: int = 700) -> str:
        return self.generate_chat(messages)[:2500]


def mcp_forge_memory_create_checkpoint(project_id: str, summary: str, current_state_json: str, next_actions_json: str) -> str:
    with SessionLocal() as db:
        checkpoint = MemoryService(db).create_checkpoint(project_id=project_id, summary=summary,
            current_state=json.loads(current_state_json), next_actions=json.loads(next_actions_json))
        return json.dumps({"status": "success", "checkpoint_id": str(checkpoint.id)})


llm = LLM("general")
planner_llm = LLM("planner")
executor_llm = LLM("executor")
