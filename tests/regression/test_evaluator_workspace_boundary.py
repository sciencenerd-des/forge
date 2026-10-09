from types import SimpleNamespace

from forge_runtime.sandbox import SnapshotResult


def test_evaluator_executes_contract_commands_through_workspace():
    """Pin the production boundary: container artifacts are not on the host mirror."""
    from engine.src.nodes.evaluator_node import _run_contract_command
    from forge_runtime.sandbox import Workspace

    class RecordingWorkspace(Workspace):
        root = "/workspace"

        def __init__(self):
            self.commands = []

        def run(self, command, cwd=None, timeout=120, env=None):
            self.commands.append((command, cwd, timeout, env))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    workspace = RecordingWorkspace()
    result = _run_contract_command(workspace, "python3 -c 'print(1)'", timeout=60)

    assert result.returncode == 0
    assert workspace.commands == [("python3 -c 'print(1)'", None, 60, None)]


def test_canonical_eval_results_use_exported_snapshot_and_final_contract(tmp_path):
    from engine.src.nodes.evaluator_node import _canonical_eval_results

    class SnapshotWorkspace:
        def __init__(self):
            self.destinations = []

        def export_snapshot(self, destination):
            self.destinations.append(destination)
            return SnapshotResult(path=str(tmp_path), digest="digest", file_count=1)

    captured = {}

    def fake_verify(slug, path, *, runner):
        captured.update(slug=slug, path=path, runner=runner)
        return {
            "verdict": "accepted",
            "checks": [{"name": "behavior", "passed": True, "detail": "ok"}],
        }

    workspace = SnapshotWorkspace()
    results, accepted = _canonical_eval_results(
        "rpn", workspace, verify_goal_fn=fake_verify, runner="container-runner"
    )

    assert accepted is True
    assert results[0]["id"] == "canonical:1"
    assert captured == {"slug": "rpn", "path": str(tmp_path), "runner": "container-runner"}
    assert len(workspace.destinations) == 1
