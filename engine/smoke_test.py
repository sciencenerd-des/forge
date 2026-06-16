import json
import datetime
import tempfile

from src.state.schema import Goal, AgentState
from src.graph import app

# Verify if local environment is ready
def verify_environment():
    print("🔍 Verifying Environment...")
    try:
        # Test if LangGraph can be imported
        from langgraph.graph import StateGraph
        print("✅ LangGraph library is available.")
        
        return True
    except ImportError as e:
        print(f"❌ Missing dependencies: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error during verification: {e}")
        return False

def run_smoke_test():
    if not verify_environment():
        print("🛑 Environment check failed. Please run 'pip install -r requirements.txt' and try again.")
        return

    print("🚀 Starting Smoke Test for Autonomy Engine...")
    
    # 1. Define a simple goal
    initial_goal = Goal(
        id="test_001",
        title="Hello World Task",
        description="Create a simple Python script that prints 'Hello, Autonomy!' to the console.",
        status="active",
        success_criteria=["Script exists", "Script prints the correct text"],
        priority=1
    )

    # 2. Initialize state
    initial_state = {
        "project_id": "smoke_test_project_unique_123",
        "goal": initial_goal,
        "task_queue": [],
        "active_task": None,
        "heartbeat": None,
        "history": [],
        "turn_count": 0,
        "current_run_id": "smoke_test_001",
        "timestamp": datetime.datetime.now(),
        "active_sandbox": {"workspace": tempfile.mkdtemp(prefix="forge-smoke-")},
    }

    print(f"Goal: {initial_goal.title}")
    print(f"Success Criteria: {initial_goal.success_criteria}")
    print("-" * 40)

    # 3. Execute the graph
    try:
        # We use the app.invoke to run the LangGraph
        # In a real scenario, this would be wrapped in a loop or a web server
        final_state = app.invoke(initial_state)
        
        print("-" * 40)
        print("✅ Smoke Test Completed.")
        print(f"Final Heartbeat: {final_state.get('heartbeat')}")
        print(f"Final Decision: {final_state.get('decision')}")
        print(f"Final Turn Count: {final_state.get('turn_count')}")
        
    except Exception as e:
        print(f"❌ Smoke Test Failed: {e}")
        # Provide detailed traceback for verification
        import traceback
        traceback.print_exc()
    finally:
        try:
            from app.database import engine
            engine.dispose()
            print("🔌 Database connections disposed.")
        except Exception as e:
            print(f"Could not dispose engine: {e}")

if __name__ == "__main__":
    run_smoke_test()
