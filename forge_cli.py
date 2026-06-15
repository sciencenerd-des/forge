"""``forge`` — the operator entry point.

Sets up the two package roots (so ``app``/``src`` resolve in the split layout)
then dispatches to the engine. Installed as a console script by pyproject.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / "engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _cmd_run(args: argparse.Namespace) -> int:
    """Run the autonomy loop (attached, or detached with --detached)."""
    import run_pge
    project = args.project or run_pge.resolve_default_project()
    if args.detached:
        from pge_launcher import launch_pge
        import json
        print(json.dumps(launch_pge(project, source="cli:run"), indent=2))
        return 0
    run_pge.run_pge(project, args.goal, args.desc, None)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Serve the control-plane API behind the web console."""
    import uvicorn
    uvicorn.run("control_plane.api:app",
                host=os.getenv("FORGE_CONTROL_HOST", "127.0.0.1"),
                port=int(os.getenv("FORGE_CONTROL_PORT", "8787")),
                reload=args.reload)
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    """Print the resolved configuration (paths, DB, default provider)."""
    import forge_config as c
    import json
    provider = c.provider_for("executor")
    print(json.dumps({
        "home": str(c.home()),
        "workspaces": str(c.workspaces_root()),
        "state_db": c.state_db_path(),
        "database": _redact_url(c.database_url()),
        "control_database": _redact_url(c.control_database_url()),
        "provider": {key: value for key, value in provider.items() if key != "api_key"},
    }, indent=2))
    return 0


def _redact_url(value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(value)
    if not parsed.username and not parsed.password:
        return value
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="forge", description="Forge autonomous coding harness")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="run the autonomy loop")
    pr.add_argument("--project", default=None, help="project UUID (default: resolve/create)")
    pr.add_argument("--goal", default=None, help="goal title (omit to resume the DB goal)")
    pr.add_argument("--desc", default=None, help="goal description")
    pr.add_argument("--detached", action="store_true", help="run detached (survives this shell)")
    pr.set_defaults(func=_cmd_run)

    ps = sub.add_parser("serve", help="serve the control-plane API")
    ps.add_argument("--reload", action="store_true", help="auto-reload (dev)")
    ps.set_defaults(func=_cmd_serve)

    sub.add_parser("config", help="print resolved configuration").set_defaults(func=_cmd_config)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
