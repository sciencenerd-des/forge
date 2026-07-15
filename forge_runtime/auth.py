"""Control-plane credential lifecycle.

Environment configuration remains the explicit override. When it is absent,
Forge creates a per-installation bearer token in FORGE_HOME with restrictive
permissions so a fresh local or container install is not shipped with a
shared default credential.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_FILENAME = "control-token"


def control_token_path() -> Path:
    home = Path(os.getenv("FORGE_HOME", str(Path.home() / ".forge"))).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home / TOKEN_FILENAME


def load_or_create_control_token() -> tuple[str, bool]:
    """Return ``(token, created)`` using env, then the local token file."""
    configured = os.getenv("FORGE_CONTROL_TOKEN")
    if configured:
        return configured, False

    path = control_token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    except OSError as exc:
        raise RuntimeError(f"cannot read control token at {path}: {exc}") from exc

    if token:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RuntimeError(f"cannot secure control token at {path}: {exc}") from exc
        return token, False

    token = secrets.token_urlsafe(32)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(token + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"cannot persist control token at {path}: {exc}") from exc
    return token, True
