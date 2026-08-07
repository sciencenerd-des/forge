from evals.goal_suite import GOALS


def test_suite_defines_eight_goals():
    assert len(GOALS) == 8
    assert [slug for slug, _ in GOALS] == [
        "snake",
        "digits",
        "lru",
        "rpn",
        "json-parser",
        "tic-tac-toe",
        "dijkstra",
        "email-validator",
    ]


def test_goal_suite_delegates_to_production_orchestrator(monkeypatch, tmp_path):
    import evals.goal_suite as suite

    captured = {}
    expected = {"schema_version": "2.0", "goals": [], "completed": 0, "total": 0}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(suite, "run_production_suite", fake_run)
    output = tmp_path / "report.json"
    report = suite.run_suite(
        model="stub",
        base_url="http://127.0.0.1:1234/v1",
        timeout=1,
        max_turns=2,
        output=output,
        only=1,
    )

    assert report is expected
    assert captured["goals"] is GOALS
    assert captured["model"] == "stub"
    assert captured["base_url"] == "http://127.0.0.1:1234/v1"
    assert captured["only"] == 1
    assert captured["output"] == output
    assert captured["child_env"]["PGE_MAX_TURNS"] == "2"
    assert "configure" not in captured
    assert not output.exists(), "the adapter must not synthesize a second result format"


def test_goal_suite_module_has_no_subprocess_compatibility_path():
    import evals.goal_suite as suite

    assert not hasattr(suite, "subprocess")
    assert not hasattr(suite, "_verdict")
