import json

from evals.goal_suite import GOALS, run_suite


def test_suite_defines_eight_goals():
    assert len(GOALS) == 8
    assert [slug for slug, _ in GOALS] == ["snake", "digits", "lru", "rpn", "json-parser", "tic-tac-toe", "dijkstra", "email-validator"]


def test_suite_report_shape_for_stubbed_subprocess(monkeypatch, tmp_path):
    class Result:
        returncode = 1
        stderr = "failed"
        stdout = ""

    monkeypatch.setattr("evals.goal_suite.subprocess.run", lambda *args, **kwargs: Result())
    monkeypatch.setattr("evals.goal_suite._verdict", lambda project, slug: {
        "slug": slug, "goal_status": "active", "file_changes": 0, "latest_test_status": None,
    })
    output = tmp_path / "report.json"
    report = run_suite(model="stub", timeout=1, max_turns=1, output=output, only=1)
    assert report["total"] == 1
    assert report["summary"]["failed"] == 1
    assert json.loads(output.read_text())["goals"][0]["status"] == "failed"
