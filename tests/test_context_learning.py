from __future__ import annotations

from MCUM.db.project_registry import (
    derive_context_effectiveness_profile,
    derive_dispatch_performance_profile,
    derive_retrieval_scope_profile,
)


def test_derive_context_effectiveness_profile_rewards_helpful_sections_and_reasons() -> None:
    logs = [
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "success",
            "context_tokens_in": 720,
            "retrieval_latency_ms": 180,
            "task_wall_clock_ms": 6400,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "compiled_context": {
                    "selected_items_summary": {
                        "playbooks": [{"token_cost": 140}, {"token_cost": 110}],
                        "experiences": [{"token_cost": 90}],
                        "failure_patterns": [{"token_cost": 60}],
                    }
                },
                "context_effectiveness": {
                    "items": [
                        {
                            "section": "playbooks",
                            "selected": True,
                            "effectiveness": "high",
                            "support_score": 0.92,
                            "utility_reasons": ["source_match", "commands"],
                        },
                        {
                            "section": "experiences",
                            "selected": True,
                            "effectiveness": "medium",
                            "support_score": 0.74,
                            "utility_reasons": ["source_overlap"],
                        },
                        {
                            "section": "failure_patterns",
                            "selected": False,
                            "effectiveness": "missed_opportunity",
                            "support_score": 0.81,
                            "utility_reasons": ["risk_fit"],
                        },
                    ]
                },
            },
        },
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "success",
            "context_tokens_in": 760,
            "retrieval_latency_ms": 210,
            "task_wall_clock_ms": 7200,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "compiled_context": {
                    "selected_items_summary": {
                        "playbooks": [{"token_cost": 120}],
                        "failure_patterns": [{"token_cost": 70}],
                    }
                },
                "context_effectiveness": {
                    "items": [
                        {
                            "section": "playbooks",
                            "selected": True,
                            "effectiveness": "medium",
                            "support_score": 0.66,
                            "utility_reasons": ["source_match", "files_touched"],
                        },
                        {
                            "section": "failure_patterns",
                            "selected": False,
                            "effectiveness": "missed_opportunity",
                            "support_score": 0.78,
                            "utility_reasons": ["risk_fit"],
                        },
                    ]
                },
            },
        },
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "failure",
            "context_tokens_in": 1450,
            "retrieval_latency_ms": 980,
            "task_wall_clock_ms": 22400,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "compiled_context": {
                    "selected_items_summary": {
                        "playbooks": [{"token_cost": 310}],
                        "conflict_cases": [{"token_cost": 180}],
                    }
                },
                "context_effectiveness": {
                    "items": [
                        {
                            "section": "playbooks",
                            "selected": True,
                            "effectiveness": "high",
                            "support_score": 0.88,
                            "utility_reasons": ["source_match"],
                        },
                        {
                            "section": "conflict_cases",
                            "selected": True,
                            "effectiveness": "miss",
                            "support_score": 0.20,
                            "utility_reasons": ["risk_fit"],
                        },
                    ]
                },
            },
        },
    ]

    profile = derive_context_effectiveness_profile(
        logs,
        skill_name="mcum-orchestrator",
        task_type="mejorar",
        execution_mode="ejecutar",
        min_samples=2,
        scope="same_project",
    )

    assert profile["active"] is True
    assert profile["sample_count"] == 3
    assert profile["section_adjustments"]["playbooks"] > 0
    assert profile["section_adjustments"]["failure_patterns"] > 0
    assert profile["section_adjustments"]["conflict_cases"] < 0
    assert profile["reason_adjustments"]["source_match"] > 0
    assert profile["reason_adjustments"]["risk_fit"] > 0
    assert profile["efficiency_adjustments"]["playbooks"] > 0
    assert profile["efficiency_adjustments"]["conflict_cases"] < 0
    assert profile["token_target_multipliers"]["playbooks"] >= 1.0
    assert profile["token_target_multipliers"]["conflict_cases"] < 1.0


def test_derive_context_effectiveness_profile_learns_active_patterns() -> None:
    logs = [
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "success",
            "context_tokens_in": 640,
            "retrieval_latency_ms": 170,
            "task_wall_clock_ms": 5100,
            "log_metadata": {
                "task_brief": {"task_type": "analizar", "execution_mode": "analizar"},
                "compiled_context": {
                    "selected_items_summary": {
                        "active_patterns": [{"token_cost": 80}],
                    }
                },
                "context_effectiveness": {
                    "items": [
                        {
                            "section": "active_patterns",
                            "selected": True,
                            "effectiveness": "high",
                            "support_score": 0.9,
                            "utility_reasons": ["analysis_fit", "risk_fit"],
                        },
                        {
                            "section": "active_patterns",
                            "selected": False,
                            "effectiveness": "missed_opportunity",
                            "support_score": 0.75,
                            "utility_reasons": ["analysis_fit"],
                        },
                    ]
                },
            },
        }
    ]

    profile = derive_context_effectiveness_profile(
        logs,
        skill_name="mcum-orchestrator",
        task_type="analizar",
        execution_mode="analizar",
        min_samples=1,
        scope="same_project",
    )

    assert profile["active"] is True
    assert profile["section_adjustments"]["active_patterns"] > 0
    assert profile["efficiency_adjustments"]["active_patterns"] > 0
    assert profile["token_target_multipliers"]["active_patterns"] >= 1.0
    assert profile["reason_adjustments"]["analysis_fit"] > 0


def test_derive_retrieval_scope_profile_prefers_cross_project_when_it_performs_better() -> None:
    logs = [
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "partial",
            "context_tokens_in": 980,
            "retrieval_latency_ms": 180,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "project_scope": "same_project",
                "context_effectiveness": {
                    "summary": {
                        "selected_items": 2,
                        "high_value_selected": 0,
                        "missed_opportunities": 1,
                        "items_evaluated": 3,
                    }
                },
            },
        },
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "success",
            "context_tokens_in": 1080,
            "retrieval_latency_ms": 260,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "project_scope": "cross_project_fallback",
                "context_effectiveness": {
                    "summary": {
                        "selected_items": 3,
                        "high_value_selected": 2,
                        "missed_opportunities": 0,
                        "items_evaluated": 3,
                    }
                },
            },
        },
        {
            "skill_used": "mcum-orchestrator",
            "outcome": "success",
            "context_tokens_in": 1120,
            "retrieval_latency_ms": 280,
            "log_metadata": {
                "task_brief": {"task_type": "mejorar", "execution_mode": "ejecutar"},
                "project_scope": "cross_project_fallback",
                "context_effectiveness": {
                    "summary": {
                        "selected_items": 3,
                        "high_value_selected": 2,
                        "missed_opportunities": 0,
                        "items_evaluated": 3,
                    }
                },
            },
        },
    ]

    profile = derive_retrieval_scope_profile(
        logs,
        skill_name="mcum-orchestrator",
        task_type="mejorar",
        execution_mode="ejecutar",
        min_samples=2,
        scope="blended",
    )

    assert profile["active"] is True
    assert profile["eager_cross_project"] is True
    assert profile["cross_project_fallback_only_if_no_project_hits"] is False
    assert profile["recommended_cross_project_memories"] >= 1
    assert profile["score_delta"] > 0


def test_derive_dispatch_performance_profile_learns_from_corrections() -> None:
    logs = [
        {
            "skill_used": "html-dashboard-expert",
            "outcome": "success",
            "log_metadata": {
                "task_brief": {"task_type": "crear", "execution_mode": "ejecutar"},
                "selected_skill": "ui-ux-pro-max",
                "final_skill": "html-dashboard-expert",
                "skill_correction": {"changed": True, "implicit": True},
            },
        },
        {
            "skill_used": "html-dashboard-expert",
            "outcome": "success",
            "log_metadata": {
                "task_brief": {"task_type": "crear", "execution_mode": "ejecutar"},
                "selected_skill": "ui-ux-pro-max",
                "final_skill": "html-dashboard-expert",
                "skill_correction": {"changed": True, "implicit": True},
            },
        },
        {
            "skill_used": "html-dashboard-expert",
            "outcome": "success",
            "log_metadata": {
                "task_brief": {"task_type": "crear", "execution_mode": "ejecutar"},
                "selected_skill": "html-dashboard-expert",
                "final_skill": "html-dashboard-expert",
                "skill_correction": {"changed": False, "implicit": False},
            },
        },
    ]

    profile = derive_dispatch_performance_profile(
        logs,
        task_type="crear",
        execution_mode="ejecutar",
        min_samples=2,
        scope="same_project",
    )

    assert profile["active"] is True
    assert profile["sample_count"] == 3
    assert profile["priority_adjustments"]["html-dashboard-expert"] > 0
    assert profile["priority_adjustments"]["ui-ux-pro-max"] < 0
    assert profile["score_adjustments"]["html-dashboard-expert"] > 0
    assert profile["score_adjustments"]["ui-ux-pro-max"] < 0
