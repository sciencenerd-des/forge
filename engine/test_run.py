import os
import json
import datetime
from typing import List
from src.state.schema import Goal, Task, AgentState
from src.graph import app

# Mocking the LLM call for this test script 
# (In production, this would be handled by the node logic)
def run_autonomous_test():
    print("🚀 Initializing Autonomous Run...")
    
    # Define a starting goal
    initial_goal = Goal(
        id="goal_001",
        title="Weather Dashboard",
        description="Create a Python script that fetches weather data for 3 cities (London, New York, Tokyo) and prints a formatted summary.",
        status="active",
        success_criteria=["Fetches data for all 3 cities", "Prints formatted output", "No crashes"],
        priority=1
    )

    # Initialize state
    initial_state = {
        "goal": initial_goal,
        "task_queue": [],
        "active_task": None,
        "heartbeat": None,
        "history": [],
        "turn_count": 0,
        "current_run_id": "run_001",
        "timestamp": datetime.datetime.now()
    }

    print(f"Starting Goal: {initial_goal.title}")
    print(f"Success Criteria: {initial_goal.success_criteria}")
    print("-" * 40)

    # Execute the graph
    # This will trigger the Planner -> Executor -> Evaluator loop
    try:
        final_state = app.invoke(initial_state)
        print("-" * 40)
        print("🏁 Run Completed.")
        print(f"Final Summary: {final_state.get('heartbeat', {}).get('progress_summary', 'No summary returned')}")
        print(f"Final Decision: {final_state.get('decision')}")
    except Exception as e:
        print(f"❌ Error during execution: {e}")

if __name__ == "__main__":
    run_autonomous_test()
