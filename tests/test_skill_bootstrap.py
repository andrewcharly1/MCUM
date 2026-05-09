from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from MCUM.sisl import skill_bootstrap
from MCUM import workspace_session


def test_derive_bootstrap_payload_extracts_experiences_and_cases(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: demo-skill
description: |
  Expert workflow for dashboard delivery.
skill_version: "1.2.0"
---

# Demo Skill

## Objetivo
Generar dashboards ejecutivos HTML con validacion minima.

## Input Contract
- titulo
- kpis

## Directivas Deterministas
1. No usar React.
2. Solicitar NEED_INFO si faltan KPI.

## Anti-patrones
- No conectar PostgreSQL directamente.
- No prometer backend cuando la skill es estatica.

## Golden Dataset

### Caso Valido 1
- **Input:** Haz un dashboard ejecutivo de flota.
- **Output esperado:** status OK con html renderizable.

### Caso Invalido 1
- **Input:** Crea un dashboard React con backend.
- **Output esperado:** INVALID_INPUT.
""",
        encoding="utf-8",
    )

    payload = skill_bootstrap.derive_bootstrap_payload("demo-skill", skill_md_path=str(skill_md))

    assert payload["skill_version"] == "1.2.0"
    assert len(payload["experiences"]) >= 3
    assert any(item["category"] == "failure_pattern" for item in payload["experiences"])
    assert any(item["category"] == "evaluation_policy" for item in payload["experiences"])
    assert payload["cases"][0]["type"] == "valid"
    assert payload["cases"][1]["type"] == "negative"


def test_bootstrap_skill_from_doc_persists_experiences_and_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: demo-skill
description: "Bootstrap me"
---

# Demo Skill

## Purpose
Bootstrap this skill.

## Requirements
- project id
- objective

## Anti-patterns
- no backend
""",
        encoding="utf-8",
    )
    saved: list[dict] = []

    monkeypatch.setattr(
        skill_bootstrap,
        "save_experience",
        lambda **kwargs: saved.append(kwargs) or f"exp-{len(saved)}",
    )
    monkeypatch.setattr(
        skill_bootstrap,
        "generate_and_save",
        lambda skill_name, max_tests=8: {
            "skill_name": skill_name,
            "total_generated": 6,
            "saved_ids": ["t1", "t2"],
            "breakdown": {"factual": 3, "negative": 2, "adversarial": 1},
        },
    )

    result = skill_bootstrap.bootstrap_skill_from_doc(
        "demo-skill",
        project_id="project-1",
        skill_md_path=str(skill_md),
        max_tests=6,
    )

    assert result["experiences_seeded"] >= 3
    assert result["tests_generated"] == 6
    assert result["tests_saved"] == 2
    assert all(item["project_id"] == "project-1" for item in saved)
    assert all(item["is_synthetic"] is True for item in saved)


def test_derive_bootstrap_payload_rule_only_doc_still_reaches_minimum_memories(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: demo-skill
description: "Industrial Go standard."
---

# Demo Skill

## Instrucciones y Estándares

### 1. Manejo de Errores
- Prohibido ignorar errores con `_`.
- No usar panic en producción.

### 2. Telemetría
- Usar logging estructurado.
""",
        encoding="utf-8",
    )

    payload = skill_bootstrap.derive_bootstrap_payload("demo-skill", skill_md_path=str(skill_md))

    assert len(payload["experiences"]) >= 3
    assert any(item["category"] == "failure_pattern" for item in payload["experiences"])
    assert any(item["category"] == "testing_strategy" for item in payload["experiences"])


def test_skill_bootstrap_cli_runs_bootstrap_and_optional_sisl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_calls: list[dict] = []
    args = argparse.Namespace(
        project_path=str(tmp_path),
        project_name="MCUM",
        skill_name=["html-dashboard-expert"],
        max_tests=8,
        run_sisl=True,
        target_ckl=0.85,
        writeback_mode="disabled",
        sisl_dry_run=True,
        no_persist_eval=False,
        quiet=True,
    )

    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "bootstrap_skill_from_doc",
        lambda skill_name, project_id=None, max_tests=8: {
            "skill_name": skill_name,
            "experiences_seeded": 4,
            "tests_generated": 8,
            "tests_saved": 8,
            "test_breakdown": {"factual": 4, "negative": 2, "adversarial": 2},
            "skill_version": "1.0.0",
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "run_sisl_cycle",
        lambda **kwargs: {
            "ckl_score": 0.81,
            "baseline_ckl_score": 0.74,
            "proposals_n": 1,
            "high_conf_n": 1,
            "applied": [],
            "gate_result": None,
            "report_id": "report-1",
            "report_version": "1.0.1",
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "log_entry",
        lambda **kwargs: log_calls.append(kwargs) or "log-1",
    )

    exit_code = workspace_session._run_skill_bootstrap(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"skill_name": "html-dashboard-expert"' in output
    assert '"tests_generated": 8' in output
    assert log_calls[0]["title"] == "Skill bootstrap cycle"
    assert log_calls[0]["log_metadata"]["results"][0]["sisl_cycle"]["report_id"] == "report-1"
