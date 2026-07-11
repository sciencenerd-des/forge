def test_llm_import_is_lazy_and_role_aware(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "general-model")
    monkeypatch.setenv("PGE_EXECUTOR_MODEL", "executor-model")

    import forge_runtime.llm as llm_module

    llm_module.invalidate_clients()
    executor = llm_module.client_for("executor")
    general = llm_module.client_for("general")

    assert executor is not general
    assert executor._forge_model == "executor-model"
    assert general._forge_model == "general-model"
