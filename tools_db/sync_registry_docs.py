"""Sync the LIVE Hermes tool registry into tool_docs.db.

Every tool Hermes can register (native, plugin, MCP) gets a row with its full
schema documentation, under category ``hermes:<toolset>``. The curated entries
(core/web/framework/...) written by build_tool_docs.py are left untouched and
remain the lean always-loaded index; registry rows are the deep documentation
pulled on demand via ``pge_get_tool_doc``.

Run after adding toolsets/plugins/MCP servers:
    ~/.hermes/hermes-agent/venv/bin/python3 tools_db/sync_registry_docs.py
(The pge plugin's post_tool_call hook also captures tools dynamically the
first time they are used, so unseen tools self-document at runtime.)
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "tool_docs.db")
HERMES_AGENT = os.path.expanduser("~/.hermes/hermes-agent")


def tool_row(entry):
    schema = entry.schema or {}
    desc = (schema.get("description") or "").strip()
    params = schema.get("parameters") or {}
    full_doc = (
        f"{desc}\n\nPARAMETERS (JSON schema):\n{json.dumps(params, indent=2)}"
    )
    when = desc.split(".")[0][:220] or f"Tool from toolset {entry.toolset}"
    return (entry.name, f"hermes:{entry.toolset}", f"Hermes registry (toolset `{entry.toolset}`)",
            when, full_doc)


def main():
    sys.path.insert(0, HERMES_AGENT)
    os.chdir(HERMES_AGENT)
    import model_tools  # noqa: F401  (importing registers every native tool)
    from tools.registry import registry

    rows = []
    for ts in registry.get_registered_toolset_names():
        for name in registry.get_tool_names_for_toolset(ts):
            entry = registry.get_entry(name)
            if entry is not None:
                rows.append(tool_row(entry))

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for name, cat, loc, when, doc in rows:
        cur.execute(
            "INSERT INTO tools(name,category,location,when_to_use,full_doc,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET category=excluded.category, "
            "location=excluded.location, when_to_use=excluded.when_to_use, "
            "full_doc=excluded.full_doc, updated_at=excluded.updated_at",
            (name, cat, loc, when, doc, now))
    con.commit()
    n = cur.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    nh = cur.execute("SELECT COUNT(*) FROM tools WHERE category LIKE 'hermes:%'").fetchone()[0]
    con.close()
    print(f"synced {len(rows)} registry tools -> tool_docs.db (total rows {n}, registry rows {nh})")


if __name__ == "__main__":
    main()
