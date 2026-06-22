"""
Policy loading and task brief normalization for MCUM strict mode.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INTAKE_POLICY_FILE = ROOT / "directives" / "intake_policy.json"
EXECUTION_POLICY_FILE = ROOT / "directives" / "execution_policy.json"
MAINTENANCE_POLICY_FILE = ROOT / "directives" / "maintenance_policy.json"
PATTERN_POLICY_FILE = ROOT / "directives" / "pattern_policy.json"


DEFAULT_INTAKE_POLICY = {
    "version": "1.0.0",
    "strict_intake": True,
    "required_fields": [
        "project_path",
        "task_type",
        "objective",
        "expected_deliverable",
        "success_criteria",
        "execution_mode",
    ],
    "optional_fields": [
        "sources_to_review",
        "constraints",
        "risk_level",
        "validation_required",
    ],
    "allowed_task_types": [
        "analizar",
        "crear",
        "corregir",
        "mejorar",
        "planificar",
        "validar",
        "automatizar",
    ],
    "allowed_execution_modes": [
        "analizar",
        "proponer",
        "ejecutar",
    ],
    "require_user_confirmation": True,
    "block_if_missing_required_fields": True,
}


DEFAULT_EXECUTION_POLICY = {
    "version": "1.0.0",
    "strict_mode": True,
    "min_dispatch_confidence": 0.80,
    "require_active_session": True,
    "require_task_brief": True,
    "require_validation_before_success": True,
    "require_artifacts_for_success": True,
    "project_first_retrieval": True,
    "allow_cross_project_fallback": True,
    "cross_project_fallback_only_if_no_project_hits": True,
    "max_cross_project_memories": 1,
    "max_playbooks": 3,
    "min_playbook_similarity": 0.28,
    "autonomous_writeback": "candidate",
    "autonomous_writeback_targets": [
        "html-dashboard-expert",
        "go-industrial-expert",
        "ui-ux-pro-max",
    ],
    "autonomous_writeback_exclude": [],
    "sisl_mode": "db_only",
    "skill_factory_mode": "candidate_bootstrap",
    "skill_factory_min_gap_occurrences": 2,
    "skill_factory_low_confidence_threshold": 0.72,
    "skill_factory_max_candidates_per_cycle": 1,
    "skill_factory_min_active_tests": 8,
    "skill_factory_min_successful_uses": 2,
    "skill_factory_min_success_rate": 0.75,
    "block_on_unconfirmed_brief": True,
    "block_on_policy_violation": True,
    "execution_profiles": {
        "enabled": True,
        "default": "lite",
        "auto": {
            "full_task_types": [
                "crear",
                "corregir",
                "mejorar",
                "automatizar",
            ],
            "lite_task_types": [
                "analizar",
                "validar",
                "planificar",
            ],
            "full_risk_levels": [
                "alto",
                "high",
                "critico",
                "critical",
            ],
        },
        "profiles": {
            "fast": {
                "description": "Lowest overhead path for explicit quick diagnostics.",
                "session_controls": {
                    "no_auto_improve": True,
                    "skip_daily_guard": True,
                    "skip_runtime_artifact": True,
                    "suppress_autonomy_hooks": True,
                },
                "spec_contract": {
                    "persist": False,
                    "block_on_persist_failure": False,
                },
                "code_graph": {
                    "enabled": False,
                    "auto_sync": False,
                    "auto_query": False,
                },
                "graph_intelligence": {
                    "enabled": False,
                    "sync_on_session_begin": False,
                    "sync_on_task_end": False,
                },
                "max_playbooks": 0,
                "min_playbook_similarity": 0.34,
                "retrieval_policy": {
                    "top_relevant_slots": 1,
                    "failure_pattern_slot": 0,
                    "conflict_slot": 0,
                    "pattern_slot": 0,
                    "feedback_signal_slot": 1,
                    "max_token_budget": 900,
                    "allow_cross_project_fallback": False,
                    "max_cross_project_memories": 0,
                },
                "state_compiler": {
                    "max_context_tokens": 700,
                    "max_experiences": 1,
                    "max_failure_patterns": 0,
                    "max_conflict_cases": 0,
                    "max_playbooks": 0,
                    "max_warnings": 2,
                    "effectiveness_history_limit": 10,
                    "min_effectiveness_samples": 999,
                    "max_learned_reason_adjustments": 0,
                },
            },
            "lite": {
                "description": "Default lean path for analysis, validation and planning.",
                "session_controls": {
                    "no_auto_improve": True,
                    "skip_daily_guard": True,
                    "skip_runtime_artifact": False,
                    "suppress_autonomy_hooks": False,
                },
                "spec_contract": {
                    "persist": False,
                    "block_on_persist_failure": False,
                },
                "max_playbooks": 1,
                "min_playbook_similarity": 0.30,
                "retrieval_policy": {
                    "top_relevant_slots": 2,
                    "failure_pattern_slot": 1,
                    "conflict_slot": 0,
                    "pattern_slot": 1,
                    "feedback_signal_slot": 2,
                    "max_token_budget": 1600,
                    "allow_cross_project_fallback": True,
                    "max_cross_project_memories": 1,
                },
                "state_compiler": {
                    "max_context_tokens": 1000,
                    "max_experiences": 2,
                    "max_failure_patterns": 1,
                    "max_conflict_cases": 0,
                    "max_playbooks": 1,
                    "max_warnings": 3,
                    "effectiveness_history_limit": 20,
                    "min_effectiveness_samples": 3,
                    "max_learned_reason_adjustments": 1,
                },
            },
            "full": {
                "description": "Complete precision path for write tasks, high risk, and automation.",
                "session_controls": {
                    "no_auto_improve": False,
                    "skip_daily_guard": False,
                    "skip_runtime_artifact": False,
                    "suppress_autonomy_hooks": False,
                },
            },
        },
    },
    "tool_preference": {
        "enabled": True,
        "prefer_mcp_tools_before_terminal": True,
        "prefer_generic_safe_tools": True,
        "avoid_scratch_for_repeated_patterns": True,
        "covered_patterns": {
            "postgres_inventory": "mcum_db_overview",
            "project_listing": "mcum_db_list_projects",
            "mintral_project_listing": "mcum_db_list_projects",
            "recent_project_activity": "mcum_db_recent_activity",
            "session_or_log_id_search": "mcum_db_search_ids",
            "dynamic_readonly_sql": "mcum_db_readonly_sql",
            "tests": "mcum_run_tests",
            "python_compile_check": "mcum_compile_python",
            "task_result_record": "mcum_record_task_result",
            "static_html_preview": "mcum_start_static_server",
            "non_mutating_intake": "mcum_prepare_intake",
            "multi_agent_plan": "mcum_generate_multi_plan",
            "sisl_review": "mcum_run_sisl_dry_run",
            "skill_factory_review": "mcum_review_skill_factory",
        },
        "terminal_fallback_requires_reason": True,
    },
    "scratch_policy": {
        "enabled": True,
        "default_disposition": "temp",
        "allowed_root": ".agent/runtime/scratch",
        "avoid_client_brain_scratch": True,
        "forbid_secret_materialization": True,
        "require_metadata": True,
        "metadata_fields": [
            "purpose",
            "disposition",
            "created_by",
            "task_id",
            "delete_after_use",
            "mcp_tool_considered",
        ],
        "delete_temp_after_use": True,
        "promote_repeated_pattern_after": 2,
        "allowed_dispositions": [
            "temp",
            "artifact",
            "keep",
        ],
    },
    "spec_contract": {
        "enabled": True,
        "persist": True,
        "always_attach": True,
        "default_mode": "lite",
        "full_task_types": [
            "crear",
            "corregir",
            "mejorar",
            "automatizar",
        ],
        "lite_task_types": [
            "analizar",
            "validar",
            "planificar",
        ],
        "full_risk_levels": [
            "alto",
            "high",
            "critico",
            "critical",
        ],
        "block_on_low_score": False,
        "min_score": 0.55,
        "block_on_persist_failure": True,
    },
    "opportunistic_daily_guard": {
        "enabled": True,
        "maintenance_name": "daily_guard",
        "modes": [
            "run",
            "record",
            "frontend-qa",
            "multi-run",
        ],
        "skip_worker_sessions": True,
        "non_blocking": True,
        "record_queued_run": True,
        "env_guard": "MCUM_DAILY_GUARD_CHILD",
        "disable_env": "MCUM_DISABLE_OPPORTUNISTIC_GUARD",
        "log_dir_name": "daily_guard",
    },
    "state_compiler": {
        "enabled": True,
        "max_context_tokens": 1400,
        "max_experiences": 4,
        "max_code_graph_hits": 4,
        "max_failure_patterns": 2,
        "max_conflict_cases": 1,
        "max_playbooks": 2,
        "max_warnings": 4,
        "max_sources_to_review": 4,
        "max_constraints": 4,
        "effectiveness_history_limit": 60,
        "min_effectiveness_samples": 3,
        "max_learned_reason_adjustments": 3,
        "adaptive_section_limits_enabled": True,
        "adaptive_token_targets_enabled": True,
        "max_adaptive_slot_shift": 1,
    },
    "code_graph": {
        "enabled": True,
        "auto_sync": True,
        "coordinator_only_sync": True,
        "auto_filters": True,
        "max_hits": 8,
        "depth": 1,
        "auto_query": True,
        "max_file_bytes": 1000000,
        "experience_backfill_limit": 500,
        "excluded_dirs": [
            ".git",
            "node_modules",
            "dist",
            "build",
            ".venv",
            "venv",
            "__pycache__",
        ],
    },
    "multi_agent": {
        "enabled": True,
        "default_max_workers": 3,
        "max_write_workers": 1,
        "default_iteration_budget": 2,
        "parallelize_min_complexity": 0.58,
        "allowed_task_types": [
            "analizar",
            "corregir",
            "mejorar",
            "crear",
            "automatizar",
            "planificar",
            "validar",
        ],
        "merge_policy": {
            "default": "coordinator_decides",
            "require_validator_for_write_tasks": True,
            "max_worker_highlights": 3,
            "max_highlight_chars": 160,
            "include_phase_metrics": True,
        },
        "worker_sessions": {
            "suppress_autonomy_hooks": True,
            "writeback_mode": "coordinator_only",
        },
        "execution": {
            "parallel_read_only_workers": True,
            "max_parallel_read_only": 2,
            "stop_on_first_failure": True,
            "auto_promote_run_when_complex": True,
            "require_worker_commands_for_auto_promote": True,
            "map_primary_command_to_write_worker": True,
        },
    },
    "worker_runner": {
        "enabled": True,
        "default_runner": "powershell",
        "model_aware_runner": "minimax_sdk",
        "model_aware_workers_default": True,
        "minimax_sdk": {
            "enabled": True,
            "protocol": "auto",
            "default_model": "MiniMax-M3",
            "temperature": 0.1,
            "max_output_tokens": 1200,
            "max_prompt_chars": 9000,
            "timeout_seconds": 60,
        },
        "codex_exec": {
            "enabled": True,
            "binary": "codex",
            "sandbox": "workspace-write",
            "skip_git_repo_check": True,
            "color": "never",
            "approval_policy": "never",
            "pass_reasoning_effort": True,
            "max_prompt_chars": 7000,
        },
        "gemini_cli": {
            "enabled": True,
            "binary": "gemini.cmd",
            "default_model": "gemini-2.5-flash",
            "approval_mode": "yolo",
            "output_format": "json",
            "include_project_path": True,
            "skip_trust": True,
            "max_prompt_chars": 7000,
        },
        "spreadsheet_extractor": {
            "enabled": True,
            "max_sheets": 20,
            "max_rows": 25,
            "max_cols": 30,
            "max_scan_rows": 200,
            "max_cell_chars": 180,
        },
    },
    "frontend_qa": {
        "enabled": True,
        "provider": "playwright_mcp",
        "default_profile": "fast",
        "default_base_urls": {
            "next": "http://localhost:3000",
            "vite": "http://localhost:5173",
            "astro": "http://localhost:4321",
            "angular": "http://localhost:4200",
            "unknown": "http://localhost:3000",
        },
        "mcp": {
            "server_name": "playwright",
            "command": "npx",
            "package": "@playwright/mcp@latest",
            "use_yes_flag": True,
            "headless": True,
            "browser": "chrome",
            "caps": ["testing", "storage"],
            "isolated": True,
            "viewport_size": "1280x720",
            "output_mode": "file",
            "snapshot_mode": "full",
            "test_id_attribute": "data-testid",
            "timeout_action_ms": 5000,
            "timeout_navigation_ms": 60000,
        },
        "checks": ["render_smoke", "critical_text_visible", "console_error_scan"],
        "profiles": {
            "fast": {
                "checks": ["render_smoke", "critical_text_visible", "console_error_scan"],
                "mcp": {
                    "caps": ["testing"],
                    "timeout_action_ms": 3000,
                    "timeout_navigation_ms": 30000,
                },
                "token_controls": {
                    "avoid_screenshots_by_default": True,
                    "max_screenshots": 0,
                    "max_viewports": 1,
                },
            },
            "standard": {
                "checks": [
                    "render_smoke",
                    "accessibility_snapshot",
                    "critical_text_visible",
                    "console_error_scan",
                    "responsive_viewport_smoke",
                ],
                "mcp": {
                    "caps": ["testing", "storage"],
                    "timeout_action_ms": 5000,
                    "timeout_navigation_ms": 45000,
                },
                "token_controls": {
                    "avoid_screenshots_by_default": False,
                    "max_screenshots": 1,
                    "max_viewports": 2,
                },
            },
            "strict": {
                "checks": [
                    "render_smoke",
                    "accessibility_snapshot",
                    "critical_text_visible",
                    "primary_navigation",
                    "form_or_cta_interaction",
                    "console_error_scan",
                    "responsive_viewport_smoke",
                    "auth_state_if_required",
                ],
                "mcp": {
                    "caps": ["testing", "storage"],
                    "timeout_action_ms": 7000,
                    "timeout_navigation_ms": 60000,
                },
                "token_controls": {
                    "avoid_screenshots_by_default": False,
                    "max_screenshots": 8,
                    "max_viewports": 2,
                },
            },
        },
        "token_controls": {
            "minimal_caps": True,
            "prefer_accessibility_snapshot": True,
            "persist_outputs_to_file": True,
            "avoid_vision_by_default": True,
        },
    },
    "model_router": {
        "enabled": True,
        "strategy": "cost_aware",
        "models": {
            "architect": "gpt-5.5",
            "coder": "gpt-5.3-codex",
            "fast": "gpt-5.4-mini",
        },
        "relative_cost_weights": {
            "gpt-5.5": 3.0,
            "gpt-5.3-codex": 1.0,
            "gpt-5.4-mini": 0.35,
        },
        "deep_model_complexity_threshold": 0.78,
        "deep_model_risk_levels": ["alto", "high", "critico", "critical"],
        "mini_model_max_complexity": 0.46,
    },
    "memory_governor": {
        "enabled": True,
        "mode": "assist",
        "preserve_at_least": 1,
        "assist_bonus_weight": 0.08,
        "assist_penalty_weight": 0.12,
        "same_project_bonus": 0.05,
        "cross_project_risk": 0.06,
        "verbosity_soft_cap": 720,
        "cold_risk_threshold": 0.42,
        "quarantine_risk_threshold": 0.62,
        "quarantine_confidence_threshold": 0.42,
        "quarantine_states_filterable": [
            "quarantined"
        ],
        "never_filter_item_kinds": [
            "active_pattern"
        ],
        "adaptive_soft_filter_enabled": True,
        "adaptive_soft_filter_item_kinds": [
            "playbook",
            "failure_pattern",
            "conflict_case"
        ],
        "adaptive_soft_filter_states_filterable": [
            "quarantined"
        ],
        "adaptive_soft_filter_min_items": 3,
        "adaptive_soft_filter_min_filterable_items": 1,
        "adaptive_soft_filter_min_safe_items": 1,
        "adaptive_soft_filter_state_ratio_threshold": 0.34,
    },
    "anti_loop": {
        "enabled": True,
        "lookback_limit": 30,
        "recent_days": 21,
        "repeat_problem_threshold": 2,
        "repeat_strategy_failure_threshold": 2,
        "repeat_error_threshold": 2,
        "warning_risk_threshold": 0.35,
        "high_risk_threshold": 0.65,
        "max_recent_outcomes": 5,
        "escalate_to_multi_run_on_high_risk": True,
        "allow_preferred_write_skill_hint": True,
        "rerank_dispatch_on_high_risk": True,
        "dispatch_preference_score_boost": 0.08,
        "dispatch_preference_priority_boost": 0.5,
        "divergence_angles": [
            "conservative",
            "critical",
            "alternate_skill",
        ],
    },
}


DEFAULT_MAINTENANCE_POLICY = {
    "version": "1.0.0",
    "maintenance_name": "daily_guard",
    "min_hours_between_runs": 20,
    "delta_window_hours": 24,
    "refresh_daily_metrics": True,
    "snapshot_window_days": 14,
    "skip_if_no_new_logs": True,
    "token_targets": {
        "max_recent_context_tokens_in": 950,
        "max_recent_context_tokens_out": 220,
    },
    "latency_targets": {
        "max_recent_retrieval_p90_ms": 15000,
        "max_recent_task_p90_ms": 45000,
    },
    "quality_targets": {
        "min_recent_success_rate": 0.75,
        "max_partial_missing_artifacts": 1,
    },
    "catalog_targets": {
        "max_candidate_active_ratio": 3.0,
    },
    "memory_targets": {
        "max_experience_duplicate_ratio": 0.18,
        "max_exact_duplicate_ratio": 0.01,
        "max_experience_verbose_ratio": 0.22,
        "max_low_validation_experience_ratio": 0.35,
        "max_playbook_never_reused_ratio": 0.45,
        "max_playbook_verbose_ratio": 0.20,
        "min_playbook_compact_ratio": 0.40,
    },
    "pattern_intelligence": {
        "enabled": True,
        "run_on_new_tasks": True,
    },
    "self_heal": {
        "enabled": True,
        "max_actions_per_run": 3,
        "safe_actions": [
            "refresh_daily_metrics",
            "snapshot_project_kpis",
            "audit_memory_governance",
            "consolidate_duplicate_experiences",
            "analyze_pattern_candidates",
        ],
        "force_executes_safe_actions": True,
        "adaptive_actions": [
            "run_skill_factory",
            "tune_anti_loop_dispatch_bias",
            "tune_memory_governor",
        ],
        "repeat_required_for_adaptive_actions": True,
        "manual_review_triggers": [
            "critical_error",
            "schema_drift",
            "unexpected_exception",
            "policy_violation",
        ],
        "continue_on_action_error": True,
        "halt_on_manual_review": True,
        "rollback_on_action_failure": True,
    },
    "skill_factory": {
        "enabled": True,
        "lookback_days": 30,
        "min_occurrences": 2,
        "low_confidence_threshold": 0.72,
        "max_candidates": 1,
        "max_pending_results": 12,
        "max_monitoring_results": 5,
        "max_candidate_ratio_for_bootstrap": 3.0,
        "enable_candidate_family_consolidation": True,
        "min_active_tests": 8,
        "min_successful_uses": 2,
        "min_success_rate": 0.75,
        "min_lifecycle_score": 0.78,
        "min_testing_uses": 2,
        "activation_score": 0.82,
        "rollback_score": 0.55,
    },
    "anti_loop_dispatch_tuning": {
        "enabled": True,
        "lookback_days": 21,
        "history_lookback_days": 30,
        "history_limit": 6,
        "min_hinted_tasks": 6,
        "min_preferred_selected": 2,
        "target_success_rate": 0.82,
        "low_success_rate": 0.55,
        "success_margin": 0.08,
        "target_preferred_selection_rate": 0.45,
        "low_preferred_selection_rate": 0.20,
        "min_hours_between_adjustments": 24,
        "reversal_cooldown_hours": 72,
        "score_step": 0.01,
        "priority_step": 0.1,
        "min_score_boost": 0.04,
        "max_score_boost": 0.14,
        "min_priority_boost": 0.2,
        "max_priority_boost": 1.0,
    },
    "memory_governor_tuning": {
        "enabled": True,
        "lookback_days": 21,
        "history_lookback_days": 30,
        "history_limit": 6,
        "min_tasks": 6,
        "high_contamination_threshold": 0.45,
        "low_contamination_threshold": 0.22,
        "target_success_rate_tighten": 0.78,
        "low_success_rate_relax": 0.65,
        "target_avg_context_tokens_in": 980,
        "high_local_filter_activation_rate": 0.18,
        "high_filtered_items_per_task": 0.45,
        "min_hours_between_adjustments": 24,
        "reversal_cooldown_hours": 72,
        "step_assist_penalty_weight": 0.01,
        "step_cross_project_risk": 0.01,
        "step_verbosity_soft_cap": 40,
        "min_assist_penalty_weight": 0.08,
        "max_assist_penalty_weight": 0.22,
        "min_cross_project_risk": 0.03,
        "max_cross_project_risk": 0.14,
        "min_verbosity_soft_cap": 520,
        "max_verbosity_soft_cap": 920,
    },
}

DEFAULT_PATTERN_POLICY = {
    "version": "2.0.0",
    "mode": "shadow",
    "auto_promote": False,
    "embedding": {
        "backend": "sentence-transformers",
        "model_name": "all-MiniLM-L6-v2",
        "dimension": 384,
        "batch_size": 32,
        "require_semantic_model": True,
    },
    "eligibility": {
        "included_categories": [
            "architecture_pattern",
            "implementation_recipe",
            "testing_strategy",
            "prompting_heuristic",
            "failure_pattern",
            "evaluation_policy",
            "stack_decision",
        ],
        "excluded_categories": ["regulatory_rule"],
        "exclude_synthetic": True,
        "min_confidence": 0.55,
        "max_experiences_per_run": 1000,
    },
    "clustering": {
        "algorithm_version": "semantic-components-v2",
        "pair_similarity_threshold": 0.82,
        "centroid_similarity_threshold": 0.78,
        "max_group_size": 250,
        "max_cluster_size": 30,
    },
    "quality_gates": {
        "min_support": 3,
        "min_context_diversity": 2,
        "min_distinct_projects_global": 2,
        "min_cohesion": 0.80,
        "min_avg_confidence": 0.75,
        "max_open_conflicts": 0,
        "review_score": 0.78,
    },
    "lifecycle": {
        "candidate_ttl_days": 90,
        "min_usage_before_health_decision": 5,
        "degraded_success_rate": 0.50,
        "manual_accept_to_status": "draft",
    },
    "maintenance": {
        "enabled": True,
        "action": "analyze_pattern_candidates",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_policy(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(defaults)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(defaults)
    return _deep_merge(defaults, raw)


def _write_policy(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def load_intake_policy() -> dict[str, Any]:
    return _load_policy(INTAKE_POLICY_FILE, DEFAULT_INTAKE_POLICY)


def load_execution_policy() -> dict[str, Any]:
    return _load_policy(EXECUTION_POLICY_FILE, DEFAULT_EXECUTION_POLICY)


def load_maintenance_policy() -> dict[str, Any]:
    return _load_policy(MAINTENANCE_POLICY_FILE, DEFAULT_MAINTENANCE_POLICY)


def load_pattern_policy() -> dict[str, Any]:
    return _load_policy(PATTERN_POLICY_FILE, DEFAULT_PATTERN_POLICY)


def update_execution_policy(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = load_execution_policy()
    updated = _deep_merge(current, patch or {})
    return _write_policy(EXECUTION_POLICY_FILE, updated)


def resolve_execution_profile_name(
    task_brief: dict[str, Any] | None,
    execution_policy: dict[str, Any] | None = None,
    requested: str | None = None,
) -> str:
    policy = execution_policy or load_execution_policy()
    profile_policy = dict(policy.get("execution_profiles") or {})
    profiles = dict(profile_policy.get("profiles") or {})
    if not bool(profile_policy.get("enabled", True)) or not profiles:
        return str(requested or (task_brief or {}).get("execution_profile") or "full")

    raw_name = str(
        requested
        or (task_brief or {}).get("execution_profile")
        or (task_brief or {}).get("mcum_execution_profile")
        or "auto"
    ).strip().lower()
    if not raw_name or raw_name == "auto":
        auto = dict(profile_policy.get("auto") or {})
        task_type = str((task_brief or {}).get("task_type") or "").strip().lower()
        risk_level = str((task_brief or {}).get("risk_level") or "").strip().lower()
        full_task_types = {str(item).lower() for item in auto.get("full_task_types") or []}
        full_risk_levels = {str(item).lower() for item in auto.get("full_risk_levels") or []}
        if task_type in full_task_types or risk_level in full_risk_levels:
            raw_name = "full"
        else:
            raw_name = str(profile_policy.get("default") or "lite").strip().lower()

    if raw_name not in profiles:
        raw_name = str(profile_policy.get("default") or "lite").strip().lower()
    if raw_name not in profiles:
        raw_name = next(iter(profiles.keys()))
    return raw_name


def apply_execution_profile(
    execution_policy: dict[str, Any] | None,
    task_brief: dict[str, Any] | None = None,
    requested: str | None = None,
) -> dict[str, Any]:
    base = deepcopy(execution_policy or load_execution_policy())
    profile_policy = dict(base.get("execution_profiles") or {})
    profiles = dict(profile_policy.get("profiles") or {})
    if not bool(profile_policy.get("enabled", True)) or not profiles:
        base["_execution_profile"] = str(requested or (task_brief or {}).get("execution_profile") or "full")
        base["_execution_profile_controls"] = {}
        return base

    profile_name = resolve_execution_profile_name(task_brief, base, requested=requested)
    profile = deepcopy(profiles.get(profile_name) or {})
    controls = dict(profile.pop("session_controls", {}) or {})
    profile.pop("description", None)
    retrieval_policy = dict(profile.pop("retrieval_policy", {}) or {})

    merged = _deep_merge(base, profile)
    if retrieval_policy:
        merged = _deep_merge(merged, retrieval_policy)

    merged["_execution_profile"] = profile_name
    merged["_execution_profile_controls"] = controls
    return merged


def infer_task_type(task_description: str) -> str:
    text = (task_description or "").lower()
    mapping = [
        ("analizar", ("analiza", "analizar", "review", "revisar", "diagnost", "inspeccion")),
        ("crear", ("crear", "construir", "generar", "nuevo", "build")),
        ("corregir", ("corregir", "fix", "arreglar", "resolver error", "reparar")),
        ("mejorar", ("mejorar", "optimizar", "refactor", "hardening")),
        ("planificar", ("plan", "planificar", "roadmap", "estrategia")),
        ("validar", ("validar", "test", "prueba", "verificar", "compilar")),
        ("automatizar", ("automatizar", "pipeline", "batch", "script", "orquestar")),
    ]
    for task_type, tokens in mapping:
        if any(token in text for token in tokens):
            return task_type
    return "analizar"


def normalize_task_brief(
    project_path: str,
    task_description: str,
    task_brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    incoming = dict(task_brief or {})
    normalized = {
        "project_path": incoming.get("project_path") or project_path,
        "task_type": incoming.get("task_type") or infer_task_type(task_description),
        "objective": incoming.get("objective") or task_description,
        "expected_deliverable": incoming.get("expected_deliverable") or "Resultado validado y registrado en MCUM.",
        "sources_to_review": list(incoming.get("sources_to_review") or []),
        "constraints": list(incoming.get("constraints") or []),
        "success_criteria": incoming.get("success_criteria") or "La tarea queda registrada con outcome claro, validación y artifacts cuando aplique.",
        "execution_mode": incoming.get("execution_mode") or "ejecutar",
        "risk_level": incoming.get("risk_level") or "medio",
        "validation_required": incoming.get("validation_required") or "validación mínima obligatoria",
        "confirmed": bool(incoming.get("confirmed", False)),
        "brief_source": incoming.get("brief_source") or ("user" if task_brief else "inferred"),
    }
    for key, value in incoming.items():
        if key not in normalized and value not in (None, ""):
            normalized[key] = value

    # Detectar campos requeridos inferidos (no confirmados por el usuario)
    # Compara incoming vs defaults: si el usuario no proveyo explicitamente el campo,
    # se marca como inferido. Solo se infiere si incoming es None/vacio.
    # Campos que el usuario proveyó (aunque sean enum invalido) NO se marcan como inferidos.
    if task_brief is None:
        # Sin task_brief → todos los campos vienen de inferencia
        normalized["_intake_inferred_fields"] = list(DEFAULT_INTAKE_POLICY["required_fields"])
        normalized["_intake_warnings"] = [
            f"campo requerido '{f}' inferido automaticamente" for f in DEFAULT_INTAKE_POLICY["required_fields"]
        ]
    else:
        # Verificar campo por campo: fue proveido explicitamente en task_brief?
        incoming_keys = set(k for k in incoming.keys() if incoming.get(k) not in (None, "", [], {}))
        required = set(DEFAULT_INTAKE_POLICY["required_fields"])
        inferred = required - incoming_keys
        if inferred:
            normalized["_intake_inferred_fields"] = sorted(inferred)
            normalized["_intake_warnings"] = [
                f"campo requerido '{f}' inferido, no confirmado por usuario" for f in sorted(inferred)
            ]

    return normalized


def missing_required_fields(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field_name in intake_policy.get("required_fields", []):
        value = task_brief.get(field_name)
        if value in (None, "", [], {}):
            missing.append(field_name)
    return missing


def validate_task_brief(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> list[str]:
    issues = missing_required_fields(task_brief, intake_policy)

    allowed_task_types = set(intake_policy.get("allowed_task_types", []))
    task_type = task_brief.get("task_type")
    if task_type and allowed_task_types and task_type not in allowed_task_types:
        issues.append(f"task_type:{task_type}")

    allowed_execution_modes = set(intake_policy.get("allowed_execution_modes", []))
    execution_mode = task_brief.get("execution_mode")
    if execution_mode and allowed_execution_modes and execution_mode not in allowed_execution_modes:
        issues.append(f"execution_mode:{execution_mode}")

    return issues


def task_brief_metrics(task_brief: dict[str, Any], intake_policy: dict[str, Any]) -> dict[str, Any]:
    required_fields = list(intake_policy.get("required_fields", []))
    optional_fields = list(intake_policy.get("optional_fields", []))

    required_completed = sum(
        1 for field_name in required_fields if task_brief.get(field_name) not in (None, "", [], {})
    )
    optional_completed = sum(
        1 for field_name in optional_fields if task_brief.get(field_name) not in (None, "", [], {})
    )
    total_fields = len(required_fields) + len(optional_fields)
    completed_fields = required_completed + optional_completed

    completeness = round(completed_fields / total_fields, 2) if total_fields else 1.0
    return {
        "required_completed": required_completed,
        "required_total": len(required_fields),
        "optional_completed": optional_completed,
        "optional_total": len(optional_fields),
        "completeness_score": completeness,
    }
