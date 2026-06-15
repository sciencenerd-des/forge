
import os
import json
import time
import sys
import uuid
from typing import Dict, Optional
from src.state.schema import AgentState, Task, Goal, Heartbeat
from src.nodes.planner_node import planner_node
from src.nodes.executor_node import executor_node
from src.nodes.evaluator_node import evaluator_node
from app.database import SessionLocal
from app.services import MemoryService
from src.runtime import active_goal_query

def run_test_loop():
    # Unique ID for this specific test run
    project_id = f"test-project-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    service = MemoryService(db)
    
    # Create project
    service.create_project(
        project_id=project_id,
        name="Test Project",
        repo_path="/Users/biswajitmondal/Developer/hermes_memory",
        description="Test Project Description"
    )
    db.commit()
    
    # Create a dummy goal
    goal_data = {
        "title": "Test Goal",
        "description": "Create a file named 'test.txt' with content 'hello'",
        "success_criteria": ["[1] File 'test.txt' exists with correct content"],
        "priority": 1
    }
    
    # Use the service to create the goal so it's properly committed
    new_goal = service.create_goal(
        project_id=project_id, 
        title=goal_data["title"], 
        description=goal_data["description"], 
        success_criteria=goal_data["success_criteria"], 
        priority=goal_data["priority"]
    )
    
    # Create the task
    task = Task(
        id="task-1",
        title="Write test file",
        description="Write to /Users/biswajitmondal/test.txt", # Valid path for this user
        status="active",
        priority=1
    )
    service.create_task(
        project_id=project_id, 
        goal_id=new_goal.id, 
        title=task.title, 
        description=task.description, 
        status="active", 
        priority=task.priority
    )
    db.commit()
    
    state = {
        "project_id": project_id,
        "goal": new_goal,
        "task_queue": [task],
        "active_task": task,
        "task_attempts": {"task-1": 0},
        "history": [],
        "turn_count": 0,
        "current_run_id": "test-run-1",
        "last_eval": None,
        "heartbeat": None
    }

    print(f"Starting test loop for goal: {new_goal.title}")
    for i in range(5):
        print(f"\n--- Turn {i+1} ---")
        state["turn_count"] += 1
        
        # 1. Planner
        state = planner_node(state)
        print(f"Planner output: {state.get('active_task').title if state.get('active_task') else 'None'}")
        
        # 2. Executor
        state = executor_node(state)
        print(f"Executor heartbeat: {state.get('heartbeat').progress_summary if state.get('heartbeat') else 'None'}")
        
        # 3. Evaluator
        state = evaluator_node(state)
        print(f"Evaluator decision: {state.get('decision')}")
        
        if state.get("decision") == "complete":
            print("Loop finished successfully!")
            break
        if state.get("decision") == "blocked":
            print("Loop blocked.")
            break
        if i == 4:
            print("Loop reached max turns without completion.")

if __name__ == "__main__":
    run_test_loop()
