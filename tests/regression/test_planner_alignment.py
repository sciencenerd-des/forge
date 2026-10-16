from engine.src.nodes.planner_node import _alignment_keywords


def test_build_goal_accepts_equivalent_implementation_subtasks():
    goal = _alignment_keywords("Build a robust reverse Polish notation calculator")

    for task in (
        "Define data structures and core types",
        "Implement stack operations",
        "Create an expression parser",
        "Write unit tests",
    ):
        assert goal & _alignment_keywords(task)


def test_unrelated_topical_task_still_has_no_alignment():
    goal = _alignment_keywords("Diagnose PostgreSQL connection failures")
    drift = _alignment_keywords("Publish a weather dashboard")

    assert not goal & drift
