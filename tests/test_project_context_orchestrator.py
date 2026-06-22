from __future__ import annotations

from MCUM.core.project_context_orchestrator import (
    build_project_context_envelope,
    build_worker_context_slice,
)


def test_project_context_envelope_is_project_first_and_task_aware(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "MCUM.core.project_context_orchestrator.query_unified_graph",
        lambda **kwargs: {
            "status": "success",
            "entities": [
                {
                    "id": "code-1",
                    "entity_type": "code_symbol",
                    "title": "renderDashboard",
                    "summary": "Frontend renderer",
                },
                {
                    "id": "design-1",
                    "entity_type": "design_system",
                    "title": "Product design system",
                    "summary": "Approved tokens",
                },
                {
                    "id": "spec-1",
                    "entity_type": "spec_contract",
                    "title": "Update dashboard",
                    "summary": "Acceptance criteria",
                },
            ],
            "relations": [],
        },
    )

    def fake_persist(**kwargs):
        captured.update(kwargs)
        return "pack-1"

    monkeypatch.setattr(
        "MCUM.core.project_context_orchestrator.persist_context_pack",
        fake_persist,
    )

    envelope = build_project_context_envelope(
        session_id="session-1",
        project_id="11111111-1111-1111-1111-111111111111",
        project_name="Demo",
        project_path="C:/workspace/Demo",
        task_description="Mejorar el frontend y validar sus pruebas",
        task_brief={
            "task_type": "mejorar",
            "objective": "Mejorar el diseno del dashboard",
            "success_criteria": "Pruebas aprobadas",
        },
        selected_skill="mcum-orchestrator",
        skill_status="active",
        graph_intelligence={"snapshot_id": "snapshot-1"},
        execution_policy={"graph_intelligence": {"enabled": True, "persist_context_packs": True}},
    )

    assert envelope["project"]["path"] == "C:/workspace/Demo"
    assert envelope["query_plan"]["project_first"] is True
    assert envelope["query_plan"]["allow_cross_project"] is False
    assert envelope["design_context"][0]["title"] == "Product design system"
    assert envelope["spec_test_context"][0]["title"] == "Update dashboard"
    assert envelope["context_pack_id"] == "pack-1"
    assert captured["project_id"] == "11111111-1111-1111-1111-111111111111"


def test_worker_context_slice_keeps_trace_and_respects_budget() -> None:
    envelope = {
        "version": "1.0",
        "envelope_hash": "envelope-1",
        "context_pack_id": "pack-1",
        "snapshot": {"snapshot_id": "snapshot-1"},
        "project": {"id": "project-1", "name": "Demo", "path": "C:/workspace/Demo"},
        "query_plan": {"primary_intent": "change"},
        "task_contract": {"objective": "Implement bounded change"},
        "selected_skill": {"name": "mcum-orchestrator", "status": "active"},
        "graph_context": {
            "code_locations": [{"title": f"code-{index}", "summary": "x" * 500} for index in range(8)],
            "primary_entities": [{"title": f"entity-{index}", "summary": "y" * 500} for index in range(8)],
        },
        "operational_memory": {
            "experiences": [{"title": f"experience-{index}", "summary": "z" * 500} for index in range(8)],
            "patterns": [],
            "failures": [],
        },
        "design_context": [],
        "spec_test_context": [],
        "constraints": [],
        "references": [],
    }

    context_slice = build_worker_context_slice(
        envelope,
        role="validator",
        mode="read_only",
        max_tokens=350,
    )

    assert context_slice["context_pack_id"] == "pack-1"
    assert context_slice["graph_snapshot_id"] == "snapshot-1"
    assert context_slice["project"]["path"] == "C:/workspace/Demo"
    assert context_slice["worker"]["writeback"] == "coordinator_only"
    assert context_slice["token_estimate"] <= 350
