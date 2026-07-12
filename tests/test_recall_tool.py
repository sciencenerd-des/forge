"""Phase 3: the executor's read-only memory paging contract."""
from types import SimpleNamespace

from forge_runtime.tools import ToolContext, ToolRequest, default_registry


class _Memory:
    def __init__(self):
        self.calls = []
        self.logs = []

    def search_memory(self, project_id, query, memory_type=None, limit=10):
        self.calls.append((project_id, query, memory_type, limit))
        return [SimpleNamespace(id="m1", memory_type="lesson", created_at=None, content="remember this")]

    def log_memory_recall(self, project_id, query, ids, task_id=None):
        self.logs.append((project_id, query, ids, task_id))


def test_recall_is_capped_read_only_and_cached(tmp_path):
    memory = _Memory()
    context = ToolContext(
        workspace=tmp_path, allow_write=False, allow_shell=False,
        memory_service=memory, project_id="p1", task_id="t1", recall_cache={})
    registry = default_registry()
    first = registry.execute(context, ToolRequest("recall_memory", {"query": "lesson", "limit": 5}))
    second = registry.execute(context, ToolRequest("recall_memory", {"query": "LESSON", "limit": 5}))
    assert first.ok and first.data["memory_ids"] == ["m1"]
    assert len(first.data["output"]) <= 1200
    assert second.ok and second.data["cached"] is True
    assert len(memory.calls) == 1
    assert memory.logs == [("p1", "lesson", ["m1"], "t1")]


def test_recall_without_durable_context_fails(tmp_path):
    result = default_registry().execute(
        ToolContext(workspace=tmp_path, allow_write=False, allow_shell=False),
        ToolRequest("recall_memory", {"query": "anything"}),
    )
    assert not result.ok
    assert "durable memory" in result.error
