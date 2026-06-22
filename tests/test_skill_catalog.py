from __future__ import annotations

from pathlib import Path

import pytest

from MCUM.db import skill_catalog


def test_discover_local_skills_extracts_structured_routing_from_frontmatter(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "html-dashboard-expert"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: html-dashboard-expert
description: "Dashboards HTML puros"
routing_triggers: ["dashboard", "html", "kpi"]
routing_anti: ["react", "nextjs"]
routing_priority: 8
---

# HTML Dashboard Expert
""",
        encoding="utf-8",
    )

    discovered = skill_catalog.discover_local_skills(tmp_path)

    assert discovered[0]["skill_name"] == "html-dashboard-expert"
    routing = discovered[0]["metadata"]["routing"]
    assert routing["triggers"] == ["dashboard", "html", "kpi"]
    assert routing["anti"] == ["react", "nextjs"]
    assert routing["priority"] == 8
    assert routing["has_explicit_routing"] is True


def test_discover_local_skills_can_extract_routing_from_description_sections(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "backend-analyzer-coder"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: backend-analyzer-coder
description: |
  Arquitectura backend robusta.
  ## TRIGGER KEYWORDS (match ANY):
  - "analizar backend", "disenar api", "idempotencia"
  ## ANTI-TRIGGER (NO activar si):
  - "dashboard", "css"
---

# Backend Analyzer
""",
        encoding="utf-8",
    )

    discovered = skill_catalog.discover_local_skills(tmp_path)

    routing = discovered[0]["metadata"]["routing"]
    assert routing["triggers"] == ["analizar backend", "disenar api", "idempotencia"]
    assert routing["anti"] == ["dashboard", "css"]
    assert routing["has_explicit_routing"] is False


def test_merge_runtime_metadata_keeps_paths_for_multiple_runtimes() -> None:
    merged = skill_catalog._merge_runtime_metadata(
        {
            "runtime_paths": {
                "windows": r"C:\skills\MCUM",
            }
        },
        skill_path="/mnt/c/skills/MCUM",
        runtime_id="wsl",
    )

    assert merged["runtime_paths"]["windows"] == r"C:\skills\MCUM"
    assert merged["runtime_paths"]["wsl"] == "/mnt/c/skills/MCUM"
    assert merged["last_synced_runtime"] == "wsl"


def test_resolve_skill_path_prefers_current_runtime_specific_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "MCUM"
    runtime_path.mkdir()
    record = {
        "skill_path": r"C:\skills\MCUM",
        "metadata": {
            "runtime_paths": {
                "windows": r"C:\skills\MCUM",
                "wsl": str(runtime_path),
            }
        },
    }

    monkeypatch.setattr(skill_catalog, "get_runtime_id", lambda: "wsl")

    assert skill_catalog.resolve_skill_path(record) == str(runtime_path)
