"""Validated, file-backed provider profiles for Forge.

The store is deliberately small and deterministic: one JSON document, atomic
writes, restrictive permissions, and explicit environment overrides. Callers
that expose profiles must use :func:`masked_profile` rather than serializing a
profile directly.
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROLES = ("default", "planner", "executor", "auditor", "evaluator", "steward", "general")
_FIELDS = ("base_url", "model", "api_key", "auth_mode")


def store_path() -> Path:
    home = Path(os.getenv("FORGE_HOME", str(Path.home() / ".forge"))).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home / "providers.json"


def _validate(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("providers.json must be an object with version=1")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("providers.json profiles must be an object")
    clean: dict[str, dict[str, str]] = {}
    for role, profile in profiles.items():
        if not isinstance(role, str) or not role or not isinstance(profile, dict):
            raise ValueError("provider roles and profiles must be strings and objects")
        unknown = set(profile) - set(_FIELDS)
        if unknown:
            raise ValueError(f"unknown provider fields for {role}: {sorted(unknown)}")
        clean[role] = {key: str(value) for key, value in profile.items() if value is not None}
    return {"version": 1, "profiles": clean}


def load() -> dict[str, Any]:
    path = store_path()
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {"version": 1, "profiles": {}}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid providers.json: {exc}") from exc


def save(document: dict[str, Any]) -> None:
    validated = _validate(document)
    path = store_path()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(validated, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def masked_profile(profile: dict[str, Any]) -> dict[str, Any]:
    masked = dict(profile)
    key = str(masked.get("api_key", ""))
    masked["api_key"] = f"{key[:3]}…{key[-4:]}" if len(key) > 7 else ("••••" if key else "")
    return masked


def upsert(role: str, profile: dict[str, Any]) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"unsupported provider role: {role}")
    document = load()
    existing = document["profiles"].get(role, {})
    merged = {**existing, **profile}
    document["profiles"][role] = merged
    save(document)
    return merged


def delete(role: str) -> None:
    document = load()
    document["profiles"].pop(role, None)
    save(document)


def test_connection(profile: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    base_url = str(profile.get("base_url", "")).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return {"status": "invalid", "message": "base_url must use HTTP or HTTPS"}
    request = urllib.request.Request(f"{base_url}/models", method="GET")
    api_key = str(profile.get("api_key", ""))
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        models = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
        return {"status": "reachable", "models": [model for model in models if model]}
    except urllib.error.HTTPError as exc:
        return {"status": "auth_error" if exc.code in (401, 403) else "unreachable", "code": exc.code}
    except (OSError, ValueError) as exc:
        return {"status": "unreachable", "message": str(exc)}


def env_profile(role: str) -> dict[str, str]:
    upper = role.upper()
    return {
        "model": os.getenv(f"PGE_{upper}_MODEL", ""),
        "base_url": os.getenv(f"FORGE_{upper}_BASE_URL", ""),
        "api_key": os.getenv(f"FORGE_{upper}_API_KEY", ""),
    }


def resolve(role: str, defaults: dict[str, str]) -> dict[str, str]:
    document = load()
    profile = {**document["profiles"].get("default", {}), **document["profiles"].get(role, {})}
    result = {**defaults, **{key: value for key, value in profile.items() if value}}
    env = {key: value for key, value in env_profile(role).items() if value}
    return {**result, **env}
