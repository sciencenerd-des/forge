import os

import forge_config
from app.models import HermesGoal, HermesProject


# A per-project workspaces ROOT, resolved from config. We NEVER fall back to the
# framework's own source tree — doing so made the executor "work" in the wrong
# directory, record nothing, then hunt for files that were never created there
# (the hallucinated-search loop). See forge_config.workspaces_root().
WORKSPACES_ROOT = str(forge_config.workspaces_root())
DEFAULT_WORKSPACE = WORKSPACES_ROOT  # retained name; no longer the framework dir


def active_goal_query(db, project_id: str):
    """Return the newest unfinished goal, falling back to the newest goal."""
    query = db.query(HermesGoal).filter(HermesGoal.project_id == project_id)
    goal = (query.filter(HermesGoal.status != "completed")
            .order_by(HermesGoal.created_at.desc()).first())
    return goal or query.order_by(HermesGoal.created_at.desc()).first()


# Directories the loop must NEVER treat as a project workspace — operating
# here would let the executor mutate the framework / agent source itself.
_FORBIDDEN_WORKSPACES = forge_config.forbidden_workspaces()


def project_workspace(db, project_id: str) -> str:
    """Resolve (and CREATE if needed) the project's own directory.

    A declared repo_path that does not yet exist is CREATED — never silently
    redirected to a fallback. If no repo_path is set, a dedicated per-project
    directory under WORKSPACES_ROOT is used. The framework's own source trees
    are forbidden as workspaces.
    """
    project = db.query(HermesProject).filter(HermesProject.id == project_id).first()
    candidate = project.repo_path if project else None
    if candidate:
        candidate = os.path.abspath(os.path.expanduser(candidate))
        if candidate.rstrip("/") in {f.rstrip("/") for f in _FORBIDDEN_WORKSPACES}:
            candidate = None  # never operate in framework source
    if not candidate:
        candidate = os.path.join(WORKSPACES_ROOT, f"project_{project_id[:12]}")
    try:
        os.makedirs(candidate, exist_ok=True)
        # Persist the resolved path so searches/tests/evidence all agree.
        if project is not None and project.repo_path != candidate:
            project.repo_path = candidate
            db.commit()
    except Exception as e:
        print(f"project_workspace: could not create {candidate}: {e}")
    return candidate
