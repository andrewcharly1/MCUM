from __future__ import annotations

import json
import uuid

from MCUM.core.state_compiler import compile_state


def test_compile_state_prioritizes_useful_evidence_and_respects_limits() -> None:
    compiled = compile_state(
        session_id="session-123",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Corregir flujo de login y rutas protegidas de auth",
        task_brief={
            "objective": "Corregir autenticacion y middleware",
            "expected_deliverable": "Flujo de auth estable",
            "success_criteria": "Login y rutas protegidas funcionando",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "sources_to_review": ["auth.ts", "middleware.ts", "route.ts"],
            "constraints": ["No romper sesiones existentes", "Mantener RLS"],
        },
        skill_selected="nextjs-supabase-auth",
        skill_status="active",
        dispatch_confidence=0.91,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=132,
        experiences=[
            {
                "id": "exp-1",
                "title": "Hot fix auth middleware",
                "category": "implementation_recipe",
                "content": {
                    "conclusion": "Centralize session refresh before protected routes.",
                    "context": "Use middleware guard on auth-sensitive paths only.",
                },
                "applicability": {"when": "Next.js + Supabase auth flows"},
                "not_applicable_cases": {"when_not": "Static public pages"},
                "_similarity": 0.95,
                "current_confidence": 0.92,
                "skill_name": "nextjs-supabase-auth",
                "project_id": "project-1",
                "source_artifacts": [{"path": "src/auth/middleware.ts"}],
            },
            {
                "id": "exp-2",
                "title": "Generic noisy dump",
                "category": "implementation_recipe",
                "content": {
                    "conclusion": "x" * 1200,
                    "context": "y" * 800,
                },
                "_similarity": 0.94,
                "current_confidence": 0.89,
            },
        ],
        failure_patterns=[
            {
                "id": "risk-1",
                "title": "JWT cookie drift",
                "category": "failure_pattern",
                "content": {
                    "conclusion": "Do not mix stale cookie state with refreshed auth context.",
                },
                "_similarity": 0.88,
                "current_confidence": 0.84,
            }
        ],
        conflict_cases=[],
        playbooks=[
            {
                "id": "pb-1",
                "title": "Auth middleware rollout",
                "objective": "Patch login + protected routes safely",
                "output_summary": "Validated route guards and token refresh",
                "commands": ["pytest -q", "npm run test"],
                "files_touched": ["middleware.ts", "app/login/page.tsx"],
                "reusable_when": "Auth regressions in Next.js + Supabase",
                "_similarity": 0.93,
            },
            {
                "id": "pb-2",
                "title": "Low value playbook",
                "objective": "misc",
                "output_summary": "z" * 1400,
                "_similarity": 0.20,
            },
        ],
        warnings=["cold start", "cross-project fallback"],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 520,
                "max_experiences": 1,
                "max_failure_patterns": 1,
                "max_conflict_cases": 0,
                "max_playbooks": 1,
                "max_warnings": 1,
                "max_sources_to_review": 2,
                "max_constraints": 1,
            }
        },
    )

    assert compiled.selected_counts["playbooks"] == 1
    assert compiled.selected_counts["experiences"] == 1
    assert compiled.selected_counts["failure_patterns"] == 1
    assert compiled.selected_counts["warnings"] == 1
    assert compiled.dropped_counts["experiences"] == 1
    assert compiled.dropped_counts["playbooks"] == 1
    assert compiled.was_truncated is True
    assert compiled.estimated_tokens <= compiled.token_budget
    assert compiled.selected_items["experiences"][0]["title"] == "Hot fix auth middleware"
    assert compiled.selected_items["playbooks"][0]["title"] == "Auth middleware rollout"
    assert any(
        reason in {"source_match", "source_overlap"}
        for reason in compiled.selected_items["experiences"][0]["_utility_reasons"]
    )
    assert compiled.task_brief["sources_to_review"] == ["auth.ts", "middleware.ts"]
    assert compiled.task_brief["constraints"] == ["No romper sesiones existentes"]
    metadata = compiled.to_metadata()
    assert metadata["selected_items_summary"]["experiences"][0]["utility_reasons"]
    assert any(
        reason in {"source_match", "source_overlap"}
        for reason in metadata["selected_items_summary"]["experiences"][0]["utility_reasons"]
    )
    context_block = compiled.to_context_block()
    assert "State compiler:" in context_block
    assert "Auth middleware rollout" in context_block
    assert "Generic noisy dump" not in context_block


def test_compile_state_metadata_normalizes_uuid_identifiers() -> None:
    exp_id = uuid.uuid4()
    compiled = compile_state(
        session_id="session-uuid",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Revisar scheduler MCUM",
        task_brief={
            "objective": "Normalizar metadata serializable",
            "expected_deliverable": "Metadata json-safe",
            "success_criteria": "to_metadata no rompe json.dumps",
            "execution_mode": "analizar",
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.88,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=12,
        experiences=[
            {
                "id": exp_id,
                "title": "UUID-backed experience",
                "category": "implementation_recipe",
                "content": {"conclusion": "Convert identifiers to strings before metadata logging."},
                "_similarity": 0.81,
                "current_confidence": 0.9,
            }
        ],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
        execution_policy=None,
    )

    metadata = compiled.to_metadata()
    assert metadata["selected_items_summary"]["experiences"][0]["id"] == str(exp_id)


def test_compile_state_applies_historical_context_learning_bias() -> None:
    without_learning = compile_state(
        session_id="session-a",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Mejorar wrapper y logging de workspace session",
        task_brief={
            "task_type": "mejorar",
            "objective": "Ajustar wrapper con mejor logging",
            "expected_deliverable": "Wrapper mas estable",
            "success_criteria": "Cambios validados",
            "execution_mode": "ejecutar",
            "sources_to_review": ["workspace_session.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.9,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=10,
        experiences=[
            {
                "id": "exp-a",
                "title": "Very similar generic note",
                "category": "implementation_recipe",
                "content": {"conclusion": "General improvements without file anchors."},
                "_similarity": 0.95,
                "current_confidence": 0.86,
            },
            {
                "id": "exp-b",
                "title": "Wrapper logging recipe",
                "category": "implementation_recipe",
                "content": {"conclusion": "Update workspace_session logging flow."},
                "_similarity": 0.66,
                "current_confidence": 0.68,
                "source_artifacts": [{"path": "workspace_session.py"}],
            },
        ],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
            execution_policy={"state_compiler": {"max_experiences": 2}},
    )

    with_learning = compile_state(
        session_id="session-b",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Mejorar wrapper y logging de workspace session",
        task_brief={
            "task_type": "mejorar",
            "objective": "Ajustar wrapper con mejor logging",
            "expected_deliverable": "Wrapper mas estable",
            "success_criteria": "Cambios validados",
            "execution_mode": "ejecutar",
            "sources_to_review": ["workspace_session.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.9,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=10,
        experiences=[
            {
                "id": "exp-a",
                "title": "Very similar generic note",
                "category": "implementation_recipe",
                "content": {"conclusion": "General improvements without file anchors."},
                "_similarity": 0.95,
                "current_confidence": 0.86,
            },
            {
                "id": "exp-b",
                "title": "Wrapper logging recipe",
                "category": "implementation_recipe",
                "content": {"conclusion": "Update workspace_session logging flow."},
                "_similarity": 0.66,
                "current_confidence": 0.68,
                "source_artifacts": [{"path": "workspace_session.py"}],
            },
        ],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
            execution_policy={"state_compiler": {"max_experiences": 2}},
        effectiveness_profile={
            "active": True,
            "scope": "same_project",
            "sample_count": 4,
            "section_adjustments": {"experiences": 0.01},
            "reason_adjustments": {"source_match": 0.05},
        },
    )

    without_exp = next(item for item in without_learning.selected_items["experiences"] if item["id"] == "exp-b")
    with_exp = next(item for item in with_learning.selected_items["experiences"] if item["id"] == "exp-b")
    assert with_exp["_utility_score"] > without_exp["_utility_score"]
    assert any(key.startswith("history_") for key in with_exp["_utility_profile"])
    learning_summary = with_learning.to_metadata()["learning_profile_summary"]
    assert learning_summary["sample_count"] == 4
    assert learning_summary["reason_adjustments"]["source_match"] > 0


def test_compile_state_adapts_section_limits_and_token_targets_from_history() -> None:
    compiled = compile_state(
        session_id="session-plan",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Investigar riesgo de regresion y ajustar wrapper",
        task_brief={
            "task_type": "mejorar",
            "objective": "Reducir regresiones del wrapper",
            "expected_deliverable": "Plan de hardening validado",
            "success_criteria": "Riesgos principales cubiertos",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.9,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=10,
        experiences=[],
        failure_patterns=[
            {
                "id": "risk-a",
                "title": "Regression after lifecycle change",
                "category": "failure_pattern",
                "content": {"conclusion": "Watch close()/abort() paths after wrapper edits."},
                "_similarity": 0.8,
                "current_confidence": 0.86,
            },
            {
                "id": "risk-b",
                "title": "Policy downgrade bug",
                "category": "failure_pattern",
                "content": {"conclusion": "Validation can silently downgrade outcomes."},
                "_similarity": 0.74,
                "current_confidence": 0.8,
            },
        ],
        conflict_cases=[],
        playbooks=[
            {
                "id": "pb-a",
                "title": "Wrapper regression checklist",
                "objective": "Validate wrapper changes",
                "output_summary": "Run wrapper-focused smoke checks.",
                "commands": ["pytest -q"],
                "_similarity": 0.87,
            },
            {
                "id": "pb-b",
                "title": "Secondary wrapper flow",
                "objective": "Check alternate lifecycle path",
                "output_summary": "Review record-only close flow.",
                "commands": ["python workspace_session.py record"],
                "_similarity": 0.65,
            },
        ],
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 900,
                "max_playbooks": 2,
                "max_failure_patterns": 1,
                "max_experiences": 0,
                "max_conflict_cases": 0,
                "max_adaptive_slot_shift": 1,
            }
        },
        effectiveness_profile={
            "active": True,
            "scope": "same_project",
            "sample_count": 5,
            "section_adjustments": {
                "playbooks": -0.06,
                "failure_patterns": 0.08,
            },
            "efficiency_adjustments": {
                "playbooks": -0.03,
                "failure_patterns": 0.04,
            },
            "token_target_multipliers": {
                "playbooks": 0.78,
                "failure_patterns": 1.22,
            },
            "reason_adjustments": {"risk_fit": 0.03},
        },
    )

    metadata = compiled.to_metadata()
    assert metadata["adaptive_section_limits"]["playbooks"] == 1
    assert metadata["adaptive_section_limits"]["failure_patterns"] == 2
    assert compiled.selected_counts["playbooks"] == 1
    assert compiled.selected_counts["failure_patterns"] == 2
    assert metadata["section_token_targets"]["failure_patterns"] >= metadata["section_token_targets"]["playbooks"]
    assert metadata["learning_profile_summary"]["efficiency_adjustments"]["failure_patterns"] > 0
    assert metadata["learning_profile_summary"]["token_target_multipliers"]["playbooks"] < 1.0
    assert "Adaptive section plan:" in compiled.to_context_block()


def test_compile_state_includes_active_patterns_and_serializes_metadata() -> None:
    compiled = compile_state(
        session_id="session-patterns",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Aplicar contexto neuro simbolico con patrones activos",
        task_brief={
            "task_type": "mejorar",
            "objective": "Incluir patrones activos en el contexto compilado",
            "expected_deliverable": "Contexto con active_patterns",
            "success_criteria": "Seleccionar el patron mas compacto y serializar metadata JSON-safe",
            "execution_mode": "analizar",
            "sources_to_review": ["core/state_compiler.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.9,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=18,
        experiences=[],
        active_patterns=[
                {
                    "id": "pat-heavy",
                    "name": "Verbose active pattern",
                    "description": " ".join(["noisy"] * 240),
                "category": "architecture_pattern",
                "status": "active",
                "experience_count": 8,
                "avg_score": 0.97,
                "context_diversity": 5,
                "evidence_ids": ["exp-1", "exp-2", "exp-3"],
                "evidence_projects": ["project-1"],
                "evidence_skills": ["mcum-orchestrator"],
                "_combined_score": 0.93,
            },
            {
                "id": "pat-compact",
                "name": "Compact active pattern",
                "description": "Compact rule for token-aware context compilation.",
                "category": "architecture_pattern",
                "status": "active",
                "experience_count": 4,
                "avg_score": 0.91,
                "context_diversity": 3,
                "evidence_ids": ["exp-4"],
                "evidence_projects": ["project-1"],
                "evidence_skills": ["mcum-orchestrator"],
                "_combined_score": 0.92,
            },
        ],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
            execution_policy={
                "state_compiler": {
                    "max_context_tokens": 700,
                    "max_active_patterns": 1,
                    "max_experiences": 0,
                    "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_playbooks": 0,
                "max_warnings": 0,
            }
        },
    )

    metadata = compiled.to_metadata()
    json.dumps(metadata)
    assert compiled.selected_counts["active_patterns"] == 1
    assert [item["id"] for item in compiled.selected_items["active_patterns"]] == ["pat-compact"]
    assert "Active patterns" in compiled.to_context_block()
    assert metadata["selected_items_summary"]["active_patterns"][0]["title"] == "Compact active pattern"
    assert metadata["selected_items_summary"]["active_patterns"][0]["status"] == "active"
    assert metadata["selected_items_summary"]["active_patterns"][0]["evidence_ids"] == ["exp-4"]
    assert metadata["budget_fill_ratio"] > 0


def test_compile_state_prefers_compact_validated_playbooks_over_verbose_ones() -> None:
    compiled = compile_state(
        session_id="session-compact",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Corregir wrapper MCUM y validar cierre de sesion",
        task_brief={
            "task_type": "corregir",
            "objective": "Aplicar fix seguro y validar wrapper",
            "expected_deliverable": "Fix estable del wrapper",
            "success_criteria": "Sesion cierra y valida sin regresiones",
            "execution_mode": "ejecutar",
            "sources_to_review": ["workspace_session.py", "tests/test_workspace_session.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.94,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=18,
        experiences=[],
        active_patterns=[],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[
            {
                "id": "pb-compact",
                "title": "Wrapper unicode output fix",
                "objective": "Repair console-safe wrapper printing",
                "output_summary": "Patch wrapper output handling, keep stdout safe, and preserve clean session close.",
                "validation_summary": "Validated with py_compile and wrapper smoke tests on Windows.",
                "commands": ["python -m py_compile workspace_session.py", "pytest -q"],
                "files_touched": ["workspace_session.py", "tests/test_workspace_session.py"],
                "reusable_when": "Console encoding or wrapper close-path regressions appear again.",
                "_similarity": 0.86,
                "confidence_score": 0.81,
            },
            {
                "id": "pb-verbose",
                "title": "Wrapper unicode output fix verbose archive",
                "objective": "Repair console-safe wrapper printing",
                "output_summary": ("Patch wrapper output handling and document every observed console path. " * 30).strip(),
                "validation_summary": "",
                "commands": ["python -m py_compile workspace_session.py", "pytest -q", "python workspace_session.py run"],
                "files_touched": [
                    "workspace_session.py",
                    "tests/test_workspace_session.py",
                    "docs/wrapper-notes.md",
                    "reports/wrapper-run.json",
                    "reports/wrapper-run-2.json",
                ],
                "reusable_when": "Wrapper issues in general.",
                "_similarity": 0.89,
                "confidence_score": 0.81,
            },
        ],
        warnings=[],
        execution_policy={"state_compiler": {"max_playbooks": 1, "max_context_tokens": 480}},
    )

    selected_playbook = compiled.selected_items["playbooks"][0]
    metadata_playbook = compiled.to_metadata()["selected_items_summary"]["playbooks"][0]

    assert selected_playbook["id"] == "pb-compact"
    assert any(
        reason in {"compact_playbook", "validated_compact_playbook"}
        for reason in selected_playbook["_utility_reasons"]
    )
    assert metadata_playbook["compactness_score"] > 0.7
    assert metadata_playbook["compactness_profile"]["bloat_penalty"] == 0.0
    assert "[compact=" in compiled.to_context_block()
