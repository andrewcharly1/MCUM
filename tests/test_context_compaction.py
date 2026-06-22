from __future__ import annotations

from MCUM.core.state_compiler import compile_state


def _make_verbose_text(prefix: str, repeat: int = 80) -> str:
    return " ".join([prefix] * repeat)


def test_compile_state_prefers_token_efficient_playbooks_under_budget_pressure() -> None:
    compiled = compile_state(
        session_id="session-budget-1",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Reducir costo de contexto sin perder confianza",
        task_brief={
            "objective": "Compaction con mejor presupuesto de tokens",
            "expected_deliverable": "Contexto compacto y confiable",
            "success_criteria": "Seleccionar evidencia de menor costo cuando cubre la misma necesidad",
            "execution_mode": "ejecutar",
            "sources_to_review": ["core/state_compiler.py", "db/project_registry.py", "directives/retrieval_policy.json"],
            "constraints": ["No bajar confianza", "No romper el flujo actual", "Mantener trazabilidad"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.93,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=58,
        experiences=[],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[
            {
                "id": "pb-heavy",
                "title": "Verbose playbook",
                "objective": "Same retrieval need",
                "output_summary": _make_verbose_text("costly", 90),
                "commands": ["pytest -q"],
                "files_touched": ["core/state_compiler.py"],
                "_similarity": 0.99,
            },
            {
                "id": "pb-compact",
                "title": "Compact playbook",
                "objective": "Same retrieval need",
                "output_summary": "Direct context compaction rule with compact evidence.",
                "commands": ["pytest -q"],
                "files_touched": ["core/state_compiler.py"],
                "_similarity": 0.91,
            },
        ],
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 520,
                "max_playbooks": 1,
                "max_experiences": 0,
                "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_warnings": 0,
            }
        },
    )

    assert [item["id"] for item in compiled.selected_items["playbooks"]] == ["pb-compact"]
    assert compiled.selected_items["playbooks"][0]["_utility_profile"]["budget_fit"] > 0
    assert compiled.was_truncated is True
    assert 0 < compiled.to_metadata()["budget_fill_ratio"] <= 1


def test_compile_state_reserves_space_for_later_sections() -> None:
    compiled = compile_state(
        session_id="session-budget-2",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Preservar memoria de experiencia cuando el presupuesto aprieta",
        task_brief={
            "objective": "Compaction que proteja secciones posteriores",
            "expected_deliverable": "Experiencia util conservada",
            "success_criteria": "No dejar sin espacio a experiencias compactas por un playbook demasiado caro",
            "execution_mode": "ejecutar",
            "sources_to_review": ["core/state_compiler.py"],
            "constraints": ["Mantener contexto minimo util"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.91,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=44,
        experiences=[
            {
                "id": "exp-compact",
                "title": "Compact experience",
                "category": "implementation_recipe",
                "content": {
                    "conclusion": "A short experience should survive the budget guard.",
                    "context": "It is small but actionable.",
                },
                "applicability": {"when": "When protecting later sections matters"},
                "not_applicable_cases": {"when_not": "When no later section needs to fit"},
                "_similarity": 0.87,
                "current_confidence": 0.89,
            }
        ],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[
            {
                "id": "pb-heavy",
                "title": "Very expensive playbook",
                "objective": "Reserve pressure test",
                "output_summary": _make_verbose_text("reserve", 120),
                "commands": ["pytest -q"],
                "files_touched": ["core/state_compiler.py"],
                "_similarity": 0.98,
            }
        ],
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 420,
                "max_playbooks": 1,
                "max_experiences": 1,
                "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_warnings": 0,
            }
        },
    )

    assert compiled.selected_counts["experiences"] == 1
    assert [item["id"] for item in compiled.selected_items["experiences"]] == ["exp-compact"]
    assert compiled.selected_counts["playbooks"] == 0
    assert compiled.dropped_counts["playbooks"] == 1


def test_compile_state_degrades_cleanly_without_active_patterns() -> None:
    compiled = compile_state(
        session_id="session-budget-3",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Mantener el compilador limpio cuando no hay patrones activos",
        task_brief={
            "objective": "Degradar sin active_patterns",
            "expected_deliverable": "Compilacion estable sin seccion de patrones",
            "success_criteria": "No romper metadata ni contexto cuando active_patterns viene vacio",
            "execution_mode": "analizar",
            "sources_to_review": ["core/state_compiler.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.88,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=12,
        experiences=[],
        active_patterns=[],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 240,
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
    assert compiled.selected_counts["active_patterns"] == 0
    assert metadata["selected_items_summary"]["active_patterns"] == []
    assert "Active patterns" not in compiled.to_context_block()
