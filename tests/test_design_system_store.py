from __future__ import annotations

from MCUM.db import design_system_store


def test_normalize_design_system_spec_preserves_tokens_and_reference_artifacts() -> None:
    spec = {
        "product_identity": {
            "product_name": "Control Room",
            "target_users": "Operations leads",
            "platforms": ["web", "presentation"],
            "design_principles": ["dense", "auditable"],
        },
        "design_tokens": {"colors": {"primary": "#123456"}},
        "component_guidelines": {"buttons": {"primary": "solid"}},
        "reference_artifacts": [{"kind": "screenshot", "path": "refs/control.png"}],
        "confidence_score": 0.87,
    }

    normalized = design_system_store.normalize_design_system_spec(spec)

    assert normalized["product_identity"]["product_name"] == "Control Room"
    assert normalized["design_tokens"]["colors"]["primary"] == "#123456"
    assert normalized["component_guidelines"]["buttons"]["primary"] == "solid"
    assert normalized["reference_artifacts"][0]["path"] == "refs/control.png"
    assert normalized["design_brief"]["confidence_score"] == 0.87


def test_save_design_system_version_upserts_profile_and_deprecates_previous_approved(monkeypatch) -> None:
    class _FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""
            self.executed: list[tuple[str, tuple]] = []

        def execute(self, query: str, params: tuple = ()) -> None:
            self.last_query = query
            self.executed.append((query, params))

        def fetchone(self) -> dict:
            if "INSERT INTO project_registry.design_system_profiles" in self.last_query:
                return {
                    "id": "profile-1",
                    "project_id": "project-1",
                    "product_name": "Portal",
                    "audience": "Analysts",
                    "platform_targets": ["web"],
                    "design_maturity": "approved",
                }
            if "SELECT COALESCE(MAX(version_number)" in self.last_query:
                return {"next_version": 3}
            if "INSERT INTO project_registry.design_system_versions" in self.last_query:
                return {
                    "id": "version-3",
                    "profile_id": "profile-1",
                    "version_number": 3,
                    "status": "approved",
                    "source_kind": "reference_image",
                }
            return {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    cursor = _FakeCursor()
    monkeypatch.setattr(design_system_store, "ensure_design_system_schema", lambda: None)
    monkeypatch.setattr(
        design_system_store,
        "get_or_create_project",
        lambda **kwargs: {"id": "project-1", "project_name": kwargs.get("project_name"), "project_path": kwargs.get("project_path")},
    )
    monkeypatch.setattr(design_system_store, "get_db", lambda: _FakeConnection())
    monkeypatch.setattr(design_system_store, "get_cursor", lambda conn: cursor)

    result = design_system_store.save_design_system_version(
        project_path="C:/workspace/app",
        project_name="Portal",
        design_system={
            "product_identity": {
                "product_name": "Portal",
                "target_users": "Analysts",
                "platforms": ["web"],
            },
            "design_tokens": {"colors": {"primary": "#0F766E"}},
        },
        status="approved",
        source_kind="reference_image",
    )

    assert result["design_system_profile_id"] == "profile-1"
    assert result["design_system_version_id"] == "version-3"
    assert result["version_number"] == 3
    assert any("SET status = 'deprecated'" in query for query, _params in cursor.executed)


def test_design_system_skill_exists_with_persistence_contract() -> None:
    skill_path = design_system_store.Path(__file__).resolve().parents[2] / "design-system-orchestrator" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "name: design-system-orchestrator" in content
    assert "python -m MCUM.db.design_system_store upsert" in content
    assert "reference_image" in content
