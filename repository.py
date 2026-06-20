from typing import List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Plan, State, Task


class Repository:
    """
    Handles persistence for State, Plan, and Task entities.
    Uses SQLAlchemy sessions to ensure transactional integrity.
    """
    def __init__(self, session: Session):
        self.session = session

    # --- State Operations ---
    
    def create_state(self, state: State) -> State:
        self.session.add(state)
        self.session.commit()
        self.session.refresh(state)
        return state

    def get_state(self, state_id: str) -> Optional[State]:
        return self.session.query(State).filter(State.id == state_id).first()

    def update_state(self, state_id: str, **kwargs) -> Optional[State]:
        state = self.get_state(state_id)
        if state:
            for key, value in kwargs.items():
                setattr(state, key, value)
            self.session.commit()
            self.session.refresh(state)
            return state
        return None

    def delete_state(self, state_id: str) -> bool:
        state = self.get_state(state_id)
        if state:
            self.session.delete(state)
            self.session.commit()
            return True
        return False

    # --- Plan Operations ---

    def create_plan(self, plan: Plan) -> Plan:
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        return plan

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        return self.session.query(Plan).filter(Plan.id == plan_id).first()

    def update_plan(self, plan_id: str, **kwargs) -> Optional[Plan]:
        plan = self.get_plan(plan_id)
        if plan:
            for key, value in kwargs.items():
                setattr(plan, key, value)
            self.session.commit()
            self.session.refresh(plan)
            return plan
        return None

    # --- Task Operations ---

    def create_task(self, task: Task) -> Task:
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.session.query(Task).filter(Task.id == task_id).first()

    def update_task(self, task_id: str, **kwargs) -> Optional[Task]:
        task = self.get_task(task_id)
        if task:
            for key, value in kwargs.items():
                setattr(task, key, value)
            self.session.commit()
            self.session.refresh(task)
            return task
        return None

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task:
            self.session.delete(task)
            self.session.commit()
            return True
        return False

    def get_tasks_by_plan(self, plan_id: str) -> List[Task]:
        return self.session.query(Task).filter(Task.plan_id == plan_id).all()

    def atomic_transition(self, func, *args, **kwargs):
        """
        Wrapper to ensure that complex multi-entity updates 
        happen within a single transaction.
        """
        try:
            result = func(*args, **kwargs)
            self.session.commit()
            return result
        except SQLAlchemyError as e:
            self.session.rollback()
            # Log error here in a real system
            raise e
