from __future__ import annotations

from datetime import datetime, timezone

from MCUM.db import project_registry


class _CursorStub:
    def __init__(self, fetchone_results: list[dict], fetchall_results: list[list[dict]] | None = None) -> None:
        self._fetchone_results = list(fetchone_results)
        self._fetchall_results = list(fetchall_results or [])
        self.executed: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> dict:
        if not self._fetchone_results:
            return {}
        return dict(self._fetchone_results.pop(0))

    def fetchall(self) -> list[dict]:
        if not self._fetchall_results:
            return []
        return [dict(row) for row in self._fetchall_results.pop(0)]


class _CursorManager:
    def __init__(self, cursor: _CursorStub) -> None:
        self._cursor = cursor

    def __enter__(self) -> _CursorStub:
        return self._cursor

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnManager:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_audit_memory_governance_flags_contamination_signals(monkeypatch) -> None:
    cursor = _CursorStub(
        [
            {
                "total_experiences": 10,
                "unique_titles": 7,
                "low_confidence_experiences": 2,
                "low_validation_experiences": 5,
                "verbose_experiences": 3,
                "dominant_category_count": 7,
            },
            {
                "exact_duplicate_groups": 1,
                "exact_duplicate_experiences": 2,
            },
            {
                "total_playbooks": 8,
                "never_reused_playbooks": 5,
                "low_reuse_playbooks": 6,
                "verbose_playbooks": 3,
                "compact_playbooks": 2,
            },
        ]
    )
    monkeypatch.setattr(project_registry, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(project_registry, "get_cursor", lambda conn: _CursorManager(cursor))

    result = project_registry.audit_memory_governance(
        project_id="project-1",
        policy={
            "memory_targets": {
                "max_experience_duplicate_ratio": 0.18,
                "max_experience_verbose_ratio": 0.22,
                "max_low_validation_experience_ratio": 0.35,
                "max_playbook_never_reused_ratio": 0.45,
                "max_playbook_verbose_ratio": 0.20,
                "min_playbook_compact_ratio": 0.40,
            }
        },
    )

    assert result["project_id"] == "project-1"
    assert result["experience_metrics"]["duplicate_ratio"] == 0.3
    assert result["experience_metrics"]["exact_duplicate_experiences"] == 2
    assert result["playbook_metrics"]["never_reused_ratio"] == 0.625
    assert "memory_exact_duplicates_found" in result["reasons"]
    assert "memory_duplicates_high" in result["reasons"]
    assert "memory_low_validation_high" in result["reasons"]
    assert "playbook_reuse_low" in result["reasons"]
    assert result["severity"] in {"medium", "high"}


def test_detect_maintenance_delta_surfaces_memory_audit_reasons(monkeypatch) -> None:
    cursor = _CursorStub(
        [
            {
                "last_activity_at": datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
                "latest_task_day": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc).date(),
                "new_logs": 0,
                "new_tasks": 0,
                "new_successes": 0,
                "new_failures": 0,
                "new_partials": 0,
                "partial_missing_artifacts": 0,
                "avg_confidence": None,
                "avg_context_tokens_in": None,
                "avg_context_tokens_out": None,
                "avg_task_wall_clock_ms": None,
                "avg_retrieval_latency_ms": None,
                "p90_retrieval_latency_ms": None,
                "p90_task_wall_clock_ms": None,
                "new_improvements": 0,
            },
            {"latest_metrics_day": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc).date()},
            {"latest_kpi_day": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc).date()},
            {"candidate_skills": 1, "active_skills": 5},
        ]
    )
    monkeypatch.setattr(project_registry, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(project_registry, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        project_registry,
        "audit_memory_governance",
        lambda **kwargs: {
            "project_id": "project-1",
            "severity": "medium",
            "contamination_score": 0.41,
            "reasons": ["memory_exact_duplicates_found", "memory_duplicates_high", "playbook_reuse_low"],
            "experience_metrics": {"exact_duplicate_experiences": 2, "exact_duplicate_ratio": 0.2},
            "playbook_metrics": {},
        },
    )
    monkeypatch.setattr(
        project_registry,
        "analyze_anti_loop_dispatch_effectiveness",
        lambda **kwargs: {
            "project_id": "project-1",
            "enabled": True,
            "reasons": ["anti_loop_dispatch_tuning_needed"],
            "recommended_action": "tune_anti_loop_dispatch_bias",
            "recommendation": "increase_bias",
            "metrics": {"hinted_tasks": 8},
        },
    )
    monkeypatch.setattr(
        project_registry,
        "analyze_memory_governor_effectiveness",
        lambda **kwargs: {
            "project_id": "project-1",
            "enabled": True,
            "reasons": ["memory_governor_tuning_needed"],
            "recommended_action": "tune_memory_governor",
            "recommendation": "tighten",
            "metrics": {"contamination_score": 0.48},
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_recent_operational_metrics",
        lambda **kwargs: {
            "total_tasks": 0,
            "success_rate": 0.0,
            "token_efficiency_per_1k": 0.0,
        },
    )

    result = project_registry.detect_maintenance_delta(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "delta_window_hours": 24,
            "token_targets": {
                "max_recent_context_tokens_in": 9999,
                "max_recent_context_tokens_out": 9999,
            },
            "latency_targets": {
                "max_recent_retrieval_p90_ms": 999999,
                "max_recent_task_p90_ms": 999999,
            },
            "quality_targets": {
                "min_recent_success_rate": 0.1,
                "max_partial_missing_artifacts": 99,
            },
            "catalog_targets": {
                "max_candidate_active_ratio": 99.0,
            },
            "memory_targets": {},
            "skill_factory": {"enabled": False},
            "skip_if_no_new_logs": True,
        },
    )

    assert result["memory_audit"]["contamination_score"] == 0.41
    assert "memory_exact_duplicates_found" in result["reasons"]
    assert "memory_duplicates_high" in result["reasons"]
    assert "playbook_reuse_low" in result["reasons"]
    assert "audit_memory_governance" in result["recommended_actions"]
    assert "consolidate_duplicate_experiences" in result["recommended_actions"]
    assert "tune_anti_loop_dispatch_bias" in result["recommended_actions"]
    assert "tune_memory_governor" in result["recommended_actions"]
    assert result["fresh_signal_present"] is True
    assert result["should_run"] is True


def test_analyze_anti_loop_dispatch_effectiveness_recommends_bias_increase(monkeypatch) -> None:
    cursor = _CursorStub(
        [],
        fetchall_results=[
            [
                {
                    "outcome": "failure",
                    "confidence_score": 0.41,
                    "context_tokens_in": 980,
                    "task_wall_clock_ms": 32000,
                    "skill_used": "mcum-orchestrator",
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.82,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "failure",
                    "confidence_score": 0.42,
                    "context_tokens_in": 1020,
                    "task_wall_clock_ms": 34000,
                    "skill_used": "mcum-orchestrator",
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.79,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 8, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "success",
                    "confidence_score": 0.91,
                    "context_tokens_in": 760,
                    "task_wall_clock_ms": 22000,
                    "skill_used": "validator-skill",
                    "log_metadata": {
                        "selected_skill": "validator-skill",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.77,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 7, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "success",
                    "confidence_score": 0.9,
                    "context_tokens_in": 740,
                    "task_wall_clock_ms": 21000,
                    "skill_used": "validator-skill",
                    "log_metadata": {
                        "selected_skill": "validator-skill",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.76,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 6, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "failure",
                    "confidence_score": 0.39,
                    "context_tokens_in": 990,
                    "task_wall_clock_ms": 31000,
                    "skill_used": "mcum-orchestrator",
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.81,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 5, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "failure",
                    "confidence_score": 0.4,
                    "context_tokens_in": 995,
                    "task_wall_clock_ms": 31500,
                    "skill_used": "mcum-orchestrator",
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.8,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 4, 0, tzinfo=timezone.utc),
                },
            ]
        ],
    )
    monkeypatch.setattr(project_registry, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(project_registry, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        project_registry,
        "load_execution_policy",
        lambda: {
            "anti_loop": {
                "warning_risk_threshold": 0.35,
                "dispatch_preference_score_boost": 0.08,
                "dispatch_preference_priority_boost": 0.5,
            }
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_anti_loop_dispatch_tuning_history",
        lambda **kwargs: {
            "maintenance_name": "daily_guard",
            "updated_runs": 0,
            "last_direction": None,
            "last_updated_at": None,
            "hours_since_last_update": None,
            "same_direction_streak": 0,
            "recent_updates": [],
        },
    )

    result = project_registry.analyze_anti_loop_dispatch_effectiveness(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "anti_loop_dispatch_tuning": {
                "enabled": True,
                "min_hinted_tasks": 6,
                "min_preferred_selected": 2,
                "target_success_rate": 0.82,
                "low_success_rate": 0.55,
                "success_margin": 0.08,
                "target_preferred_selection_rate": 0.45,
                "low_preferred_selection_rate": 0.20,
                "score_step": 0.01,
                "priority_step": 0.1,
                "min_score_boost": 0.04,
                "max_score_boost": 0.14,
                "min_priority_boost": 0.2,
                "max_priority_boost": 1.0,
            }
        },
    )

    assert result["recommended_action"] == "tune_anti_loop_dispatch_bias"
    assert result["recommendation"] == "increase_bias"
    assert result["suggested_score_boost"] == 0.09
    assert result["suggested_priority_boost"] == 0.6
    assert result["metrics"]["hinted_tasks"] == 6
    assert result["metrics"]["preferred_selected_rate"] == 0.3333


def test_analyze_anti_loop_dispatch_effectiveness_respects_recent_adjustment_cooldown(monkeypatch) -> None:
    cursor = _CursorStub(
        [],
        fetchall_results=[
            [
                {
                    "outcome": "failure",
                    "confidence_score": 0.41,
                    "context_tokens_in": 980,
                    "task_wall_clock_ms": 32000,
                    "skill_used": "mcum-orchestrator",
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_method": "semantic",
                        "dispatch_hints": {
                            "preferred_skills": ["validator-skill"],
                            "loop_risk": 0.82,
                            "warning_risk_threshold": 0.35,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
                }
            ]
            * 6
        ],
    )
    monkeypatch.setattr(project_registry, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(project_registry, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        project_registry,
        "load_execution_policy",
        lambda: {
            "anti_loop": {
                "warning_risk_threshold": 0.35,
                "dispatch_preference_score_boost": 0.08,
                "dispatch_preference_priority_boost": 0.5,
            }
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_anti_loop_dispatch_tuning_history",
        lambda **kwargs: {
            "maintenance_name": "daily_guard",
            "updated_runs": 1,
            "last_direction": "increase",
            "last_updated_at": "2026-04-02T08:00:00+00:00",
            "hours_since_last_update": 4.0,
            "same_direction_streak": 1,
            "recent_updates": [],
        },
    )

    result = project_registry.analyze_anti_loop_dispatch_effectiveness(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "anti_loop_dispatch_tuning": {
                "enabled": True,
                "min_hinted_tasks": 6,
                "min_preferred_selected": 2,
                "min_hours_between_adjustments": 24,
                "reversal_cooldown_hours": 72,
            }
        },
    )

    assert result["recommended_action"] is None
    assert result["recommendation"] == "no_change"
    assert result["reason"] == "recent_adjustment_cooldown"
    assert result["stability_guard"]["active"] is True
    assert result["stability_guard"]["reason"] == "recent_adjustment_cooldown"


def test_summarize_recent_operational_metrics_returns_compact_kpis(monkeypatch) -> None:
    cursor = _CursorStub(
        [],
        fetchall_results=[
            [
                {
                    "outcome": "success",
                    "confidence_score": 0.92,
                    "context_tokens_in": 800,
                    "context_tokens_out": 120,
                    "retrieval_latency_ms": 220,
                    "task_wall_clock_ms": 18000,
                    "log_metadata": {
                        "selected_skill": "validator-skill",
                        "dispatch_hints": {"preferred_skills": ["validator-skill"]},
                        "memory_governance": {
                            "adaptive_filter_applied": False,
                            "total_filtered_count": 0,
                            "fallback_applied": False,
                        },
                        "playbook_memory_governance": {
                            "adaptive_filter_applied": False,
                            "filtered_count": 0,
                            "fallback_applied": False,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
                },
                {
                    "outcome": "partial",
                    "confidence_score": 0.61,
                    "context_tokens_in": 1000,
                    "context_tokens_out": 140,
                    "retrieval_latency_ms": 450,
                    "task_wall_clock_ms": 26000,
                    "log_metadata": {
                        "selected_skill": "mcum-orchestrator",
                        "dispatch_hints": {"preferred_skills": ["validator-skill"]},
                        "memory_governance": {
                            "adaptive_filter_applied": True,
                            "total_filtered_count": 1,
                            "fallback_applied": False,
                        },
                        "playbook_memory_governance": {
                            "adaptive_filter_applied": True,
                            "filtered_count": 2,
                            "fallback_applied": False,
                        },
                    },
                    "created_at": datetime(2026, 4, 2, 8, 0, tzinfo=timezone.utc),
                },
            ]
        ],
    )
    monkeypatch.setattr(project_registry, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(project_registry, "get_cursor", lambda conn: _CursorManager(cursor))

    result = project_registry.summarize_recent_operational_metrics(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
    )

    assert result["total_tasks"] == 2
    assert result["success_rate"] == 0.5
    assert result["partial_rate"] == 0.5
    assert result["avg_total_context_tokens"] == 1030.0
    assert result["token_efficiency_per_1k"] == 0.4854
    assert result["memory_governor_local_filter_rate"] == 0.5
    assert result["playbook_governor_local_filter_rate"] == 0.5
    assert result["governor_local_filter_activation_rate"] == 0.5
    assert result["governor_filtered_items_total"] == 3
    assert result["governor_filtered_items_per_task"] == 1.5
    assert result["governor_fallback_rate"] == 0.0
    assert result["anti_loop_hinted_rate"] == 1.0
    assert result["anti_loop_preferred_selected_rate"] == 0.5
    assert result["anti_loop_preferred_success_rate"] == 1.0


def test_analyze_memory_governor_effectiveness_recommends_tighten(monkeypatch) -> None:
    monkeypatch.setattr(
        project_registry,
        "load_execution_policy",
        lambda: {
            "memory_governor": {
                "assist_penalty_weight": 0.12,
                "cross_project_risk": 0.06,
                "verbosity_soft_cap": 720,
            }
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_memory_governor_tuning_history",
        lambda **kwargs: {
            "maintenance_name": "daily_guard",
            "updated_runs": 0,
            "last_direction": None,
            "last_updated_at": None,
            "hours_since_last_update": None,
            "same_direction_streak": 0,
            "recent_updates": [],
        },
    )

    result = project_registry.analyze_memory_governor_effectiveness(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "memory_governor_tuning": {
                "enabled": True,
                "min_tasks": 6,
                "high_contamination_threshold": 0.45,
                "target_success_rate_tighten": 0.78,
                "target_avg_context_tokens_in": 980,
                "step_assist_penalty_weight": 0.01,
                "step_cross_project_risk": 0.01,
                "step_verbosity_soft_cap": 40,
            }
        },
        operational_summary={
            "total_tasks": 8,
            "success_rate": 0.88,
            "avg_context_tokens_in": 1100,
            "token_efficiency_per_1k": 0.9,
        },
        memory_audit={
            "severity": "high",
            "contamination_score": 0.51,
        },
    )

    assert result["recommended_action"] == "tune_memory_governor"
    assert result["recommendation"] == "tighten"
    assert result["suggested_assist_penalty_weight"] == 0.13
    assert result["suggested_cross_project_risk"] == 0.07
    assert result["suggested_verbosity_soft_cap"] == 680


def test_analyze_memory_governor_effectiveness_respects_recent_adjustment_cooldown(monkeypatch) -> None:
    monkeypatch.setattr(
        project_registry,
        "load_execution_policy",
        lambda: {
            "memory_governor": {
                "assist_penalty_weight": 0.12,
                "cross_project_risk": 0.06,
                "verbosity_soft_cap": 720,
            }
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_memory_governor_tuning_history",
        lambda **kwargs: {
            "maintenance_name": "daily_guard",
            "updated_runs": 1,
            "last_direction": "tighten",
            "last_updated_at": "2026-04-02T08:00:00+00:00",
            "hours_since_last_update": 4.0,
            "same_direction_streak": 1,
            "recent_updates": [],
        },
    )

    result = project_registry.analyze_memory_governor_effectiveness(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "memory_governor_tuning": {
                "enabled": True,
                "min_tasks": 6,
                "min_hours_between_adjustments": 24,
            }
        },
        operational_summary={
            "total_tasks": 8,
            "success_rate": 0.9,
            "avg_context_tokens_in": 1200,
            "token_efficiency_per_1k": 0.9,
        },
        memory_audit={
            "severity": "high",
            "contamination_score": 0.5,
        },
    )

    assert result["recommended_action"] is None
    assert result["recommendation"] == "no_change"
    assert result["reason"] == "recent_adjustment_cooldown"
    assert result["stability_guard"]["active"] is True


def test_analyze_memory_governor_effectiveness_recommends_tighten_from_local_filter_pressure(monkeypatch) -> None:
    monkeypatch.setattr(
        project_registry,
        "load_execution_policy",
        lambda: {
            "memory_governor": {
                "assist_penalty_weight": 0.12,
                "cross_project_risk": 0.06,
                "verbosity_soft_cap": 720,
            }
        },
    )
    monkeypatch.setattr(
        project_registry,
        "summarize_memory_governor_tuning_history",
        lambda **kwargs: {
            "maintenance_name": "daily_guard",
            "updated_runs": 0,
            "last_direction": None,
            "last_updated_at": None,
            "hours_since_last_update": None,
            "same_direction_streak": 0,
            "recent_updates": [],
        },
    )

    result = project_registry.analyze_memory_governor_effectiveness(
        project_id="project-1",
        since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        policy={
            "memory_governor_tuning": {
                "enabled": True,
                "min_tasks": 6,
                "high_contamination_threshold": 0.45,
                "high_local_filter_activation_rate": 0.18,
                "high_filtered_items_per_task": 0.45,
                "target_success_rate_tighten": 0.78,
                "target_avg_context_tokens_in": 980,
            }
        },
        operational_summary={
            "total_tasks": 8,
            "success_rate": 0.86,
            "avg_context_tokens_in": 1050,
            "token_efficiency_per_1k": 0.82,
            "governor_local_filter_activation_rate": 0.38,
            "governor_filtered_items_per_task": 0.75,
        },
        memory_audit={
            "severity": "medium",
            "contamination_score": 0.33,
        },
    )

    assert result["recommended_action"] == "tune_memory_governor"
    assert result["recommendation"] == "tighten"
    assert result["reason"] == "local_filter_pressure_high"
