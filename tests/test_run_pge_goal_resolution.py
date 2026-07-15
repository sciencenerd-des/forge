import run_pge


def test_new_project_rejects_blank_name():
    try:
        run_pge.create_new_project("  ")
    except ValueError as error:
        assert "empty" in str(error)
    else:
        raise AssertionError("blank project name should be rejected")
