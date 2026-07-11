from pathlib import Path


def test_engine_imports_use_forge_llm_boundary():
    root = Path(__file__).parents[2]
    forbidden = []
    for path in (root / "engine" / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from hermes_tools import" in text or "import hermes_tools" in text:
            forbidden.append(str(path.relative_to(root)))
    assert forbidden == []


def test_orm_metadata_is_canonical_forge_with_legacy_aliases():
    from app.models import ForgeGoal, ForgeProject, HermesGoal, HermesProject

    assert ForgeProject.__tablename__ == "forge_projects"
    assert ForgeGoal.__tablename__ == "forge_goals"
    assert HermesProject is ForgeProject
    assert HermesGoal is ForgeGoal


def test_rename_migration_is_guarded_and_compatibility_views_are_read_only():
    root = Path(__file__).parents[2]
    migration = (root / "migrations/002_rename_hermes_to_forge.sql").read_text(encoding="utf-8")
    assert "to_regclass('public.hermes_'" in migration
    assert "ALTER TABLE" in migration
    assert "CREATE VIEW" in migration
    assert "BEGIN;" in migration and "COMMIT;" in migration
