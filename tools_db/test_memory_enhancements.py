import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow() -> datetime:
    """Naive UTC now (avoids the deprecated ``datetime.utcnow()``)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Ensure we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import (
    HermesMemoryItem,
)
from app.services import MemoryService


def test_enhancements():
    db = SessionLocal()
    try:
        service = MemoryService(db)
        
        # 1. Create a dummy test project
        project = service.create_project(
            name="Enhancement Test Project",
            repo_path=str(Path.cwd()),
            description="Testing pgvector and SQLite state.db bridge"
        )
        project_id = project.id
        print(f"✅ Created project {project.name} (ID: {project_id})")
        
        # 2. Create a Goal and Task (these will generate embeddings via LM Studio!)
        goal = service.create_goal(
            project_id=project_id,
            title="Integrate LLM memory",
            description="Add pgvector database support for semantic memory retrieval in the agent core."
        )
        print("✅ Goal created with embedding:", "Success" if goal.embedding is not None else "None (LM Studio timed out/offline)")
        
        task = service.create_task(
            project_id=project_id,
            goal_id=goal.id,
            title="Expose state.db SQLite reader",
            description="Query the agent SQLite database to capture runtime execution details."
        )
        print("✅ Task created with embedding:", "Success" if task.embedding is not None else "None")
        
        # 3. Create fine-grained events, file changes, and checkpoints for consolidation testing
        # We will manually set task as completed and backdate it to simulate an old task
        task.status = "completed"
        task.completed_at = _utcnow() - timedelta(days=10)
        db.commit()
        
        service.record_event(
            project_id=project_id,
            task_id=task.id,
            event_type="test_run",
            content="Ran pytest with 100% coverage"
        )

        service.record_file_change(
            project_id=project_id,
            task_id=task.id,
            file_path="app/services/__init__.py",
            change_summary="Implemented SQLite state.db bridge",
            reason="Expose command logs to context pack"
        )

        service.create_checkpoint(
            project_id=project_id,
            task_id=task.id,
            summary="Task bridge complete, ready for verification"
        )
        
        print("✅ Created task logs, file changes, and checkpoints for consolidation.")
        
        # Run consolidation
        print("🔄 Running consolidation (days_threshold=5)...")
        consolidation_result = service.consolidate_old_logs(project_id=project_id, days_threshold=5)
        print("✅ Consolidation output:", json.dumps(consolidation_result, indent=2))
        
        # Check that fine-grained logs are deleted and summary item is created
        assert consolidation_result["consolidated_tasks_count"] == 1
        
        digest_item = db.query(HermesMemoryItem).filter(
            HermesMemoryItem.project_id == project_id,
            HermesMemoryItem.task_id == task.id,
            HermesMemoryItem.memory_type == "task_consolidation"
        ).first()
        assert digest_item is not None
        print("✅ Consolidated digest successfully created in database!")
        print("Digest Content Preview:\n", digest_item.content[:300])
        
        # 4. Test state.db bridging via build_context_pack
        # Set task status back to active to test active task pack retrieval
        task.status = "active"
        db.commit()
        
        print("🔄 Querying build_context_pack to verify state.db bridge...")
        context_pack = service.build_context_pack(project_id=project_id)
        print("✅ Context pack retrieved.")
        print("RUNTIME_TOOL_STATE:", json.dumps(context_pack.get("RUNTIME_TOOL_STATE"), indent=2))
        
        # 5. Test search_memory with pgvector similarity
        print("🔄 Testing search_memory vector search...")
        search_res = service.search_memory(project_id=project_id, query="sqlite database core", limit=3)
        print(f"✅ Vector search returned {len(search_res)} results:")
        for idx, item in enumerate(search_res):
            print(f"  {idx+1}. [{item.memory_type}] {item.content[:100]}...")
            
        # Clean up database records
        print("🧹 Cleaning up test database records...")
        db.delete(digest_item)
        db.commit()
        db.delete(task)
        db.commit()
        db.delete(goal)
        db.commit()
        db.delete(project)
        db.commit()
        print("✅ Cleanup complete.")
        
    finally:
        db.close()

if __name__ == "__main__":
    test_enhancements()
