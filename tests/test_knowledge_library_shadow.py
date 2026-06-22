from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from MCUM.core.dispatcher import DispatchResult
from MCUM.core.session_manager import OrchestratorSession
from MCUM.core.state_compiler import compile_state
from MCUM.core import session_manager
from MCUM.core.knowledge_library_preflight import build_library_preflight
from MCUM.core.knowledge_library_router import route_task_to_knowledge_library
from MCUM.db import knowledge_library_shadow


ROOT = Path(__file__).resolve().parents[1]
FLAGS_FILE = ROOT / "directives" / "knowledge_library_flags.json"


def test_live_flags_default_to_active_cable_mode_with_safe_guardrails() -> None:
    flags = json.loads(FLAGS_FILE.read_text(encoding="utf-8"))

    assert flags["library_mode"] == "controlled_shadow"
    assert flags["default_phase"] == "activation"
    assert flags["flags"]["knowledge_library_enabled"] is True
    assert flags["flags"]["summary_first_retrieval_enabled"] is True
    assert flags["flags"]["shadow_mode_enabled"] is False
    assert flags["flags"]["mcum_orchestrator_cable_enabled"] is True
    assert flags["flags"]["experience_writeback_enabled"] is False
    assert flags["flags"]["playbook_writeback_enabled"] is False
    assert flags["flags"]["pattern_writeback_enabled"] is False
    assert flags["rollout"]["phase_4_activation"] is True


def test_preflight_blocks_when_flags_are_disabled_explicitly() -> None:
    plan = build_library_preflight(
        task_description="Consultar doctrina sin habilitar rollout",
        task_brief={"execution_mode": "analizar"},
        flags={
            "enabled": False,
            "read_path_enabled": False,
            "write_path_enabled": False,
            "integration_mode": "off",
            "full_read_enabled": False,
        },
    )

    assert plan.enabled is False
    assert plan.allow_read_path is False
    assert plan.reason == "library_disabled_by_flag"


def test_preflight_uses_active_mode_when_cable_enabled() -> None:
    plan = build_library_preflight(
        task_description="Consultar doctrina con cableado operativo habilitado",
        task_brief={"execution_mode": "analizar"},
    )

    assert plan.enabled is True
    assert plan.allow_read_path is True
    assert plan.integration_mode == "active"
    assert plan.reason == "summary_first_read_path"


def test_router_prefers_ddd_for_bounded_context_tasks() -> None:
    plan = route_task_to_knowledge_library(
        "Definir bounded contexts y ubiquitous language para el dominio de pagos",
        task_brief={"objective": "Diseñar el domain model inicial"},
    )

    assert "ddd" in plan["top_methodologies"]
    assert "sap-ddd-knowledgebase" in plan["preferred_repositories"]
    assert "bounded-context" in plan["preferred_concepts"]
    assert "ddd" in plan["methodology_lenses"]
    assert any("bounded contexts" in query.lower() for query in plan["expanded_queries"])


def test_router_prefers_pmbok_for_project_governance_tasks() -> None:
    plan = route_task_to_knowledge_library(
        "Mejorar stakeholder engagement, governance y value delivery del proyecto",
        task_brief={"objective": "Usar mejores practicas de project management"},
    )

    assert "pmbok" in plan["top_methodologies"]
    assert "LOCAL_PDFS" in plan["preferred_repositories"]
    assert "stakeholder-engagement" in plan["preferred_concepts"]
    assert "pmbok" in plan["methodology_lenses"]


def test_router_marks_conflict_for_domain_and_governance_blend() -> None:
    plan = route_task_to_knowledge_library(
        "Definir bounded contexts y governance para iniciar un nuevo proyecto",
        task_brief={"objective": "Combinar DDD y PMBOK sin perder foco"},
    )

    conflict = plan["conflict_profile"]
    assert conflict["active"] is True
    assert conflict["comparison_required"] is True
    assert set(conflict["methodologies"]) == {"ddd", "pmbok"}


def test_shadow_retrieval_returns_ranked_hits_when_explicitly_enabled(monkeypatch) -> None:
    class _FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""
            self.executed: list[tuple[str, tuple[Any, ...] | tuple]] = []

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query
            self.executed.append((query, params))

        def fetchall(self) -> list[dict]:
            if "FROM knowledge_library.summaries" in self.last_query:
                return [
                    {
                        "summary_id": "sum-1",
                        "summary_level": "section",
                        "summary_title": "Stakeholders",
                        "summary_text": "Engage stakeholders continuously to preserve value delivery.",
                        "token_count": 18,
                        "document_id": "doc-1",
                        "document_title": "PMBOK 7",
                        "source_path": "C:/repo/PMBOK 7th Edition.pdf",
                        "source_repository": None,
                        "section_id": "sec-1",
                        "section_heading": "Stakeholders",
                        "section_path": "PMBOK 7 > Stakeholders",
                        "section_page_start": 34,
                        "section_page_end": 35,
                        "chunk_id": "chunk-1",
                        "chunk_order": 1,
                        "chunk_page_start": 34,
                        "chunk_page_end": 35,
                        "authority_tier": "canonical",
                        "lexical_rank": 0.62,
                        "methodology_score": 0.91,
                        "section_concept_score": 0.74,
                        "chunk_concept_score": 0.66,
                        "repository_bonus": 0.12,
                    }
                ]
            return []

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
    monkeypatch.setattr(knowledge_library_shadow, "get_db", lambda: _FakeConnection())
    monkeypatch.setattr(knowledge_library_shadow, "get_cursor", lambda conn: cursor)

    result = knowledge_library_shadow.retrieve_knowledge_library_shadow(
        "stakeholder engagement project management",
        task_brief={"execution_mode": "analizar"},
        flags={
            "flags": {
                "knowledge_library_enabled": True,
                "summary_first_retrieval_enabled": True,
                "document_ingestion_enabled": False,
                "full_document_read_enabled": False,
                "shadow_mode_enabled": True,
                "mcum_orchestrator_cable_enabled": False,
            }
        },
        policy={
            "retrieval": {
                "default_mode": "summary_first",
                "max_sections_per_document": 2,
                "max_chunks_per_section": 2,
                "token_budget": {
                    "summary_only": 600,
                    "section_then_chunk": 1400,
                    "deep_read": 2400,
                    "full_document": 5000,
                },
            },
            "citation": {"required": True},
            "allowed_execution_modes": ["analizar", "proponer", "validar", "ejecutar"],
        },
    )

    assert result["enabled"] is True
    assert result["shadow_mode"] is True
    assert result["applied_mode"] == "summary_first"
    assert len(result["hits"]) == 1
    assert result["hits"][0]["knowledge_library"]["document_title"] == "PMBOK 7"
    assert result["hits"][0]["knowledge_library"]["methodology_score"] > 0.0
    assert result["metadata"]["route_plan"]["top_methodologies"]
    assert result["metadata"]["taxonomy_signal_counts"]["preferred_methodologies"] >= 1
    assert any(len(params) >= 6 for _query, params in cursor.executed)


def test_shadow_retrieval_merges_semantic_concepts_into_route_plan(monkeypatch) -> None:
    class _FakeCursor:
        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query

        def fetchall(self) -> list[dict]:
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(knowledge_library_shadow, "get_db", lambda: _FakeConnection())
    monkeypatch.setattr(knowledge_library_shadow, "get_cursor", lambda conn: _FakeCursor())
    monkeypatch.setattr(
        knowledge_library_shadow,
        "route_task_to_knowledge_library",
        lambda *args, **kwargs: {
            "task_text": "cognitive load and team ownership",
            "methodology_scores": {"team_topologies": 0.4},
            "matched_terms": {"team_topologies": ["cognitive load"]},
            "top_methodologies": ["team_topologies"],
            "preferred_repositories": ["team-topologies-community-materials"],
            "preferred_concepts": [],
            "concept_scores": {},
            "concept_matches": {},
            "methodology_lenses": {"team_topologies": ["Keep cognitive load explicit."]},
            "conflict_profile": {"active": False, "comparison_required": False, "methodologies": ["team_topologies"]},
            "expanded_queries": ["cognitive load team ownership"],
        },
    )
    monkeypatch.setattr(
        knowledge_library_shadow,
        "rank_concepts_semantically",
        lambda *args, **kwargs: [
            {
                "concept_slug": "cognitive-load",
                "concept_name": "Cognitive Load",
                "methodology_slug": "team_topologies",
                "semantic_score": 0.71,
            }
        ],
    )

    result = knowledge_library_shadow.retrieve_knowledge_library_shadow(
        "Reducir cognitive load del equipo",
        task_brief={"execution_mode": "analizar"},
    )

    assert result["enabled"] is True
    assert result["metadata"]["route_plan"]["semantic_concepts"][0]["concept_slug"] == "cognitive-load"
    assert "cognitive-load" in result["metadata"]["route_plan"]["preferred_concepts"]


def test_shadow_retrieval_diversifies_conflicting_methodology_hits(monkeypatch) -> None:
    class _FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query

        def fetchall(self) -> list[dict]:
            if "FROM knowledge_library.summaries" not in self.last_query:
                return []
            return [
                {
                    "summary_id": "sum-pmbok",
                    "summary_level": "section",
                    "summary_title": "Governance",
                    "summary_text": "Structured governance aligns stakeholders.",
                    "token_count": 12,
                    "document_id": "doc-pmbok",
                    "document_title": "PMBOK 7",
                    "source_path": "C:/repo/PMBOK 7th Edition.pdf",
                    "source_repository": "LOCAL_PDFS",
                    "section_id": "sec-pmbok",
                    "section_heading": "Governance",
                    "section_path": "PMBOK 7 > Governance",
                    "section_page_start": 20,
                    "section_page_end": 21,
                    "chunk_id": "chunk-pmbok",
                    "chunk_order": 1,
                    "chunk_page_start": 20,
                    "chunk_page_end": 21,
                    "authority_tier": "canonical",
                    "matched_methodology_slug": "pmbok",
                    "lexical_rank": 0.9,
                    "methodology_score": 0.7,
                    "section_concept_score": 0.2,
                    "chunk_concept_score": 0.1,
                    "repository_bonus": 0.12,
                },
                {
                    "summary_id": "sum-ddd",
                    "summary_level": "section",
                    "summary_title": "Bounded Contexts",
                    "summary_text": "Bounded contexts preserve language consistency.",
                    "token_count": 12,
                    "document_id": "doc-ddd",
                    "document_title": "Core Concepts",
                    "source_path": "C:/repo/0002-core-concepts.md",
                    "source_repository": "sap-ddd-knowledgebase",
                    "section_id": "sec-ddd",
                    "section_heading": "Bounded Contexts",
                    "section_path": "DDD > Bounded Contexts",
                    "section_page_start": None,
                    "section_page_end": None,
                    "chunk_id": "chunk-ddd",
                    "chunk_order": 1,
                    "chunk_page_start": None,
                    "chunk_page_end": None,
                    "authority_tier": "secondary",
                    "matched_methodology_slug": "ddd",
                    "lexical_rank": 0.52,
                    "methodology_score": 0.65,
                    "section_concept_score": 0.41,
                    "chunk_concept_score": 0.32,
                    "repository_bonus": 0.0,
                },
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(knowledge_library_shadow, "get_db", lambda: _FakeConnection())
    monkeypatch.setattr(knowledge_library_shadow, "get_cursor", lambda conn: _FakeCursor())
    monkeypatch.setattr(
        knowledge_library_shadow,
        "route_task_to_knowledge_library",
        lambda *args, **kwargs: {
            "task_text": "bounded contexts and governance",
            "methodology_scores": {"ddd": 0.48, "pmbok": 0.44},
            "matched_terms": {"ddd": ["bounded contexts"], "pmbok": ["governance"]},
            "top_methodologies": ["ddd", "pmbok"],
            "preferred_repositories": ["sap-ddd-knowledgebase", "LOCAL_PDFS"],
            "preferred_concepts": ["bounded-context", "project-governance"],
            "concept_scores": {"bounded-context": 0.5, "project-governance": 0.34},
            "concept_matches": {},
            "methodology_lenses": {"ddd": ["Protect domain boundaries."], "pmbok": ["Optimize for value delivery."]},
            "conflict_profile": {
                "active": True,
                "comparison_required": True,
                "requires_diverse_hits": True,
                "methodologies": ["ddd", "pmbok"],
                "summary": "Compare domain boundaries with governance controls.",
            },
            "expanded_queries": ["bounded contexts governance"],
        },
    )
    monkeypatch.setattr(knowledge_library_shadow, "rank_concepts_semantically", lambda *args, **kwargs: [])

    result = knowledge_library_shadow.retrieve_knowledge_library_shadow(
        "bounded contexts and governance",
        task_brief={"execution_mode": "analizar"},
    )

    methodologies = {
        item["knowledge_library"].get("matched_methodology_slug")
        for item in result["hits"]
    }
    assert {"ddd", "pmbok"}.issubset(methodologies)


def test_compile_state_can_render_knowledge_library_section() -> None:
    compiled = compile_state(
        session_id="session-kl",
        project_name="MCUM",
        project_id="project-1",
        project_scope="same_project",
        task_description="Analizar stakeholders y gobernanza de proyecto",
        task_brief={
            "objective": "Usar la biblioteca gobernada para enriquecer analisis",
            "expected_deliverable": "Contexto con guidance citada",
            "success_criteria": "Seccion knowledge_library visible",
            "execution_mode": "analizar",
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.91,
        dispatch_method="semantic",
        auto_dispatch_result=None,
        retrieval_mode="semantic_project",
        retrieval_latency_ms=25,
        experiences=[],
        active_patterns=[],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        knowledge_library_hits=[
            {
                "id": "kl-summary:1",
                "title": "PMBOK 7 :: Stakeholders",
                "category": "knowledge_library",
                "content": {
                    "conclusion": "Engage stakeholders continuously to preserve value delivery.",
                    "context": "source=C:/repo/PMBOK 7th Edition.pdf; section=Stakeholders; locator=p34-35; mode=summary_first",
                },
                "source_artifacts": [{"path": "C:/repo/PMBOK 7th Edition.pdf"}],
                "evidence_refs": [{"path": "C:/repo/PMBOK 7th Edition.pdf", "section": "Stakeholders"}],
                "knowledge_library": {
                    "document_title": "PMBOK 7",
                    "section_heading": "Stakeholders",
                    "mode": "summary_first",
                    "page_start": 34,
                    "page_end": 35,
                },
                "_similarity": 0.8,
            }
        ],
        knowledge_library_mode="summary_first",
        knowledge_library_metadata={
            "hits_retrieved": 1,
            "tokens_used_estimate": 90,
            "metadata": {
                "route_plan": {
                    "top_methodologies": ["ddd", "pmbok"],
                    "methodology_lenses": {
                        "ddd": ["Protect domain boundaries before discussing implementation details."],
                        "pmbok": ["Optimize for value delivery and stakeholder outcomes."],
                    },
                    "conflict_profile": {
                        "active": True,
                        "methodologies": ["ddd", "pmbok"],
                        "summary": "Compare domain boundaries with governance controls.",
                    },
                }
            },
        },
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 700,
                "max_playbooks": 0,
                "max_experiences": 0,
                "max_active_patterns": 0,
                "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_knowledge_library_hits": 1,
            }
        },
    )

    assert compiled.selected_counts["knowledge_library"] == 1
    context_block = compiled.to_context_block()
    assert "Knowledge library" in context_block
    assert "Knowledge route: methodologies=ddd, pmbok" in context_block
    assert "Knowledge conflict: ddd, pmbok" in context_block
    assert compiled.to_metadata()["knowledge_library_mode"] == "summary_first"


def test_session_begin_records_shadow_metadata_without_changing_live_memory(monkeypatch) -> None:
    session_start_calls: list[dict] = []
    log_calls: list[dict] = []
    session_end_calls: list[dict] = []
    adjust_calls: list[dict] = []

    monkeypatch.setattr(
        session_manager,
        "load_intake_policy",
        lambda: {
            "required_fields": [],
            "optional_fields": [],
            "allowed_task_types": ["analizar"],
            "allowed_execution_modes": ["analizar"],
            "require_user_confirmation": False,
            "block_if_missing_required_fields": False,
        },
    )
    monkeypatch.setattr(
        session_manager,
        "load_execution_policy",
        lambda: {
            "strict_mode": False,
            "require_task_brief": False,
            "allow_cross_project_fallback": True,
            "block_on_policy_violation": False,
            "max_playbooks": 2,
            "min_playbook_similarity": 0.28,
            "state_compiler": {
                "max_context_tokens": 800,
                "max_playbooks": 0,
                "max_experiences": 0,
                "max_active_patterns": 0,
                "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_knowledge_library_hits": 1,
            },
        },
    )
    monkeypatch.setattr(session_manager, "sync_skill_catalog", lambda: {"skills_synced": 2})
    monkeypatch.setattr(session_manager, "get_skill_record", lambda skill_name: {"status": "active"})
    monkeypatch.setattr(session_manager, "get_or_create_project", lambda **kwargs: {"id": "project-1", "project_name": "MCUM"})
    monkeypatch.setattr(session_manager, "get_retrieval_scope_profile", lambda **kwargs: {})
    monkeypatch.setattr(session_manager, "get_context_effectiveness_profile", lambda **kwargs: {})
    monkeypatch.setattr(session_manager, "get_dispatch_performance_profile", lambda **kwargs: {})
    monkeypatch.setattr(
        session_manager,
        "dispatch",
        lambda **kwargs: DispatchResult(
            skill_name="mcum-orchestrator",
            confidence=0.9,
            match_method="semantic",
            alternatives=[],
            triggered_by="semantic_score=0.90",
        ),
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_for_task",
        lambda *args, **kwargs: {
            "experiences": [],
            "active_patterns": [],
            "failure_patterns": [],
            "conflict_cases": [],
            "feedback_signals": {},
            "retrieval_mode": "semantic_project",
            "project_scope": "same_project",
            "warnings": [],
            "total_retrieved": 0,
            "tokens_used_estimate": 15,
            "scope_learning_profile": {},
            "memory_governance": {},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_session_playbooks",
        lambda *args, **kwargs: {
            "playbooks": [],
            "search_scope": "same_project",
            "warnings": [],
            "memory_governance": {},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_knowledge_library_shadow",
        lambda *args, **kwargs: {
            "enabled": True,
            "shadow_mode": True,
            "applied_mode": "summary_first",
            "hits": [
                {
                    "id": "kl-summary:1",
                    "title": "PMBOK 7 :: Stakeholders",
                    "category": "knowledge_library",
                    "content": {
                        "conclusion": "Engage stakeholders continuously to preserve value delivery.",
                        "context": "source=C:/repo/PMBOK 7th Edition.pdf; section=Stakeholders; locator=p34-35; mode=summary_first",
                    },
                    "knowledge_library": {"document_title": "PMBOK 7", "section_heading": "Stakeholders", "mode": "summary_first"},
                    "_similarity": 0.8,
                }
            ],
            "warnings": [],
            "tokens_used_estimate": 90,
            "metadata": {"shadow_enabled": True, "mcum_orchestrator_cable_enabled": False},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "analyze_problem_loop",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:kl",
            "problem_signature": "consult methodology",
            "loop_risk": 0.1,
            "risk_level": "low",
            "recommendation": "observe_only",
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "enrich_loop_state_with_strategy",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:kl",
            "problem_signature": "consult methodology",
            "strategy_fingerprint": "strategy:shadow",
            "strategy_signature": "knowledge shadow",
            "loop_risk": 0.1,
            "risk_level": "low",
            "recommendation": "observe_only",
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "log_session_start",
        lambda **kwargs: session_start_calls.append(kwargs) or {"log_id": "session-start"},
    )
    monkeypatch.setattr(session_manager, "record_retrieval_run", lambda **kwargs: "retrieval-run-1")
    monkeypatch.setattr(session_manager, "log_entry", lambda **kwargs: log_calls.append(kwargs) or "decision-log")
    monkeypatch.setattr(session_manager, "finalize_retrieval_run", lambda **kwargs: True)
    monkeypatch.setattr(session_manager, "adjust_confidence", lambda **kwargs: adjust_calls.append(kwargs) or True)
    monkeypatch.setattr(
        session_manager,
        "finalize_loop_state",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:kl",
            "strategy_fingerprint": "strategy:shadow",
            "loop_risk": 0.1,
            "risk_level": "low",
            "recommendation": "observe_only",
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "log_session_end",
        lambda **kwargs: session_end_calls.append(kwargs) or "session-end-log",
    )
    monkeypatch.setattr(session_manager, "mark_skill_used", lambda skill_name: True)

    session = OrchestratorSession(
        project_path="C:/repo",
        project_name="MCUM",
        task_description="Analizar stakeholders y mejores practicas para iniciar un proyecto",
        task_brief={"confirmed": True, "execution_mode": "analizar"},
        verbose=False,
        auto_improve=False,
    )

    ctx = session.begin()

    assert ctx.knowledge_library_mode == "summary_first"
    assert len(ctx.knowledge_library_hits) == 1
    assert ctx.compiled_state is not None
    assert ctx.compiled_state.selected_counts["knowledge_library"] == 1
    assert session_start_calls[0]["extra_metadata"]["knowledge_library"]["hits_retrieved"] == 1
    assert log_calls[0]["log_metadata"]["knowledge_library"]["mode"] == "summary_first"

    close_result = session.close(
        session_manager.TaskResult(
            task_description="Analizar stakeholders y mejores practicas para iniciar un proyecto",
            skill_used=ctx.skill_selected,
            outcome="success",
            confidence_score=0.89,
            output_summary="Sesion de prueba cerrada correctamente.",
            validation_summary="knowledge_library se mantuvo consultiva durante el cierre.",
        )
    )

    assert close_result["log_id"] == "decision-log"
    assert close_result["record_status"] == "recorded"
    assert adjust_calls == []
    assert session_end_calls[0]["extra_metadata"]["knowledge_library"]["mode"] == "summary_first"


def test_live_bridge_reports_active_integration_mode_and_not_shadow(monkeypatch) -> None:
    class _FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query

        def fetchall(self) -> list[dict]:
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(knowledge_library_shadow, "get_db", lambda: _FakeConnection())
    monkeypatch.setattr(knowledge_library_shadow, "get_cursor", lambda conn: _FakeCursor())

    result = knowledge_library_shadow.retrieve_knowledge_library_shadow(
        "bounded contexts domain model boundaries",
        task_brief={"execution_mode": "analizar"},
    )

    assert result["enabled"] is True
    assert result["shadow_mode"] is False
    assert result["metadata"]["integration_mode"] == "active"
    assert result["metadata"]["mcum_orchestrator_cable_enabled"] is True
