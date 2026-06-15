"""Forge runtime configuration — the single source of truth for every
machine-specific value: paths, the database URL, the default project, and model
provider profiles.

Everything resolves from environment variables with sane defaults rooted at
``FORGE_HOME`` (default ``~/.forge``). This is what makes Forge plug-and-play:
there are no hardcoded home paths, project ids, database names, or model
backends anywhere in the engine — they all funnel through here.

Override anything via a ``.env`` file or real environment variables; see
``.env.example`` for the full list.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Home + filesystem layout
# ---------------------------------------------------------------------------

def home() -> Path:
    """Forge's writable home directory (created on demand)."""
    p = Path(os.getenv("FORGE_HOME", str(Path.home() / ".forge"))).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def repo_root() -> Path:
    """The Forge source tree root (this file's directory)."""
    return Path(__file__).resolve().parent


def workspaces_root() -> Path:
    """Root under which the loop creates each project's own working directory.
    Never the framework tree itself — see :func:`forbidden_workspaces`."""
    p = Path(os.getenv("FORGE_WORKSPACES", str(home() / "workspaces"))).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_db_path() -> str:
    """SQLite gateway/state mirror path (read by the engine + tools_db)."""
    return os.getenv("FORGE_STATE_DB", str(home() / "state.db"))


def tooldocs_db_path() -> str:
    """Tool-documentation SQLite DB. Ships in the repo under ``tools_db/`` and
    is overridable for a writable copy."""
    default = str(repo_root() / "tools_db" / "tool_docs.db")
    return os.getenv("FORGE_TOOLDOCS_DB", default)


def forbidden_workspaces() -> set[str]:
    """Directories the loop must NEVER treat as a project workspace — operating
    here would let the executor mutate Forge's own source or home."""
    extra = {p for p in os.getenv("FORGE_FORBIDDEN_WORKSPACES", "").split(os.pathsep) if p}
    return {str(repo_root()), str(home())} | extra


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

def database_url() -> str:
    """Engine database (Postgres — the engine relies on ARRAY columns).

    Default matches the bundled ``docker-compose.yml`` Postgres service so
    ``docker compose up`` is plug-and-play. Override with ``DATABASE_URL``.
    """
    return os.getenv(
        "DATABASE_URL",
        "postgresql://forge:forge@localhost:5432/forge",
    )


def control_database_url() -> str:
    """Control-plane (GUI) database. Lightweight; defaults to local SQLite."""
    return os.getenv(
        "FORGE_CONTROL_DATABASE_URL",
        os.getenv("DATABASE_URL", f"sqlite:///{home() / 'forge-control.db'}"),
    )


# ---------------------------------------------------------------------------
# Default project (resolve-or-create — never a hardcoded UUID)
# ---------------------------------------------------------------------------

def default_project_id() -> str | None:
    """Env-pinned default project id, if any. When unset, callers should
    resolve-or-create via :func:`ensure_default_project`."""
    return os.getenv("FORGE_DEFAULT_PROJECT") or None


def ensure_default_project(db, name: str = "default") -> str:
    """Return the default project's id, creating one on first run so the loop
    never depends on a specific user's UUID. Honors ``FORGE_DEFAULT_PROJECT``."""
    from app.models import HermesProject
    pinned = default_project_id()
    if pinned:
        row = db.query(HermesProject).filter(HermesProject.id == pinned).first()
        if row:
            return row.id
    row = (db.query(HermesProject)
           .filter(HermesProject.name == name)
           .order_by(HermesProject.created_at.asc()).first())
    if row:
        return row.id
    repo = str(workspaces_root() / name)
    Path(repo).mkdir(parents=True, exist_ok=True)
    proj = HermesProject(id=pinned, name=name, repo_path=repo,
                         description="Default Forge project (auto-created).")
    db.add(proj)
    db.commit()
    return proj.id


# ---------------------------------------------------------------------------
# Model providers (generic OpenAI-compatible — LM Studio / Ollama / vLLM / cloud)
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = os.getenv("FORGE_LLM_BASE_URL", os.getenv("LLM_BASE_URL", "http://localhost:1234/v1"))
DEFAULT_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-12b-qat")
DEFAULT_API_KEY = os.getenv("FORGE_LLM_API_KEY", "not-needed")
DEFAULT_TIMEOUT = float(os.getenv("FORGE_LLM_TIMEOUT", "300"))

# Roles whose model can be routed independently (a coding-specialist on the
# executor/planner, a general model on the rest, for example).
ROLES = ("planner", "executor", "auditor", "evaluator", "steward", "general")


def provider_for(role: str = "general") -> dict:
    """Resolve the OpenAI-compatible provider profile for a loop role.

    Precedence (per field): role-specific env -> global env -> default. e.g.
    ``PGE_EXECUTOR_MODEL`` > ``LLM_MODEL`` > built-in default; and
    ``FORGE_EXECUTOR_BASE_URL`` > ``FORGE_LLM_BASE_URL`` > built-in default.
    """
    R = role.upper()
    return {
        "model": os.getenv(f"PGE_{R}_MODEL", DEFAULT_MODEL),
        "base_url": os.getenv(f"FORGE_{R}_BASE_URL", DEFAULT_BASE_URL),
        "api_key": os.getenv(f"FORGE_{R}_API_KEY", DEFAULT_API_KEY),
        "timeout": DEFAULT_TIMEOUT,
    }
