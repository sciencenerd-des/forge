from pathlib import Path


def test_generates_and_reuses_installation_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge-home"))

    from forge_runtime.auth import load_or_create_control_token

    first, created = load_or_create_control_token()
    second, created_again = load_or_create_control_token()
    token_path = Path(tmp_path / "forge-home" / "control-token")

    assert created is True
    assert created_again is False
    assert first == second
    assert token_path.read_text().strip() == first
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_environment_token_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path / "forge-home"))
    monkeypatch.setenv("FORGE_CONTROL_TOKEN", "explicit-token")

    from forge_runtime.auth import load_or_create_control_token

    token, created = load_or_create_control_token()

    assert token == "explicit-token"
    assert created is False
    assert not (tmp_path / "forge-home" / "control-token").exists()


def test_existing_token_permissions_are_repaired(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_CONTROL_TOKEN", raising=False)
    home = tmp_path / "forge-home"
    home.mkdir()
    token_path = home / "control-token"
    token_path.write_text("existing-token\n")
    token_path.chmod(0o644)
    monkeypatch.setenv("FORGE_HOME", str(home))

    from forge_runtime.auth import load_or_create_control_token

    token, created = load_or_create_control_token()

    assert token == "existing-token"
    assert created is False
    assert token_path.stat().st_mode & 0o777 == 0o600
