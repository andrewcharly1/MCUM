from __future__ import annotations

from types import SimpleNamespace

from MCUM.core.dispatcher import DispatchResult
from MCUM.core.session_manager import OrchestratorSession, TaskResult
from MCUM.core import session_manager


def test_session_persists_metrics_and_playbook(monkeypatch) -> None:
    log_calls: list[dict] = []
    session_end_calls: list[dict] = []
    playbook_calls: list[dict] = []
    mark_used_calls: list[str] = []
    saved_experience_calls: list[dict] = []
    adjust_calls: list[dict] = []
    time_values = iter([100.0, 104.2, 104.2])
    perf_values = iter([10.0, 10.25])

    monkeypatch.setattr(
        session_manager,
        "load_intake_policy",
        lambda: {
            "required_fields": [],
            "optional_fields": [],
            "allowed_task_types": ["validar"],
            "allowed_execution_modes": ["ejecutar"],
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
            "max_playbooks": 3,
            "min_playbook_similarity": 0.28,
        },
    )
    monkeypatch.setattr(
        session_manager,
        "build_source_snapshots",
        lambda paths, project_path=None: [
            {
                "snapshot_type": "source_file",
                "path": "C:/repo/workspace_session.py",
                "exists": True,
            }
        ],
    )
    monkeypatch.setattr(
        session_manager,
        "build_project_structure_snapshots",
        lambda project_path, extra_paths=None: [
            {
                "snapshot_type": "project_structure",
                "path": "C:/repo/package.json",
                "exists": True,
                "role": "package_manifest",
            }
        ],
    )
    monkeypatch.setattr(session_manager, "sync_skill_catalog", lambda: {"skills_synced": 2})
    monkeypatch.setattr(session_manager, "get_skill_record", lambda skill_name: {"status": "active"})
    monkeypatch.setattr(session_manager, "get_or_create_project", lambda **kwargs: {"id": "project-1", "project_name": "MCUM"})
    monkeypatch.setattr(
        session_manager,
        "get_retrieval_scope_profile",
        lambda **kwargs: {
            "sample_count": 3,
            "active": True,
            "eager_cross_project": False,
            "prefer_same_project_only": True,
        },
    )
    monkeypatch.setattr(
        session_manager,
        "get_context_effectiveness_profile",
        lambda **kwargs: {
            "active": True,
            "scope": "same_project",
            "sample_count": 4,
            "section_adjustments": {"playbooks": 0.04},
            "reason_adjustments": {"source_match": 0.03},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "get_dispatch_performance_profile",
        lambda **kwargs: {
            "active": True,
            "sample_count": 5,
            "priority_adjustments": {"mcum-orchestrator": 0.2},
            "score_adjustments": {"mcum-orchestrator": 0.01},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "dispatch",
        lambda **kwargs: DispatchResult(
            skill_name="mcum-orchestrator",
            confidence=0.91,
            match_method="semantic",
            alternatives=[],
            triggered_by="semantic_score=0.91",
        ),
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_for_task",
        lambda *args, **kwargs: {
                "experiences": [
                    {
                        "id": "exp-1",
                        "category": "implementation_recipe",
                        "title": "Use wrapper smoke tests",
                        "content": {"conclusion": "Run pytest on wrapper paths."},
                        "applicability": {"when": "Updating workspace_session"},
                        "not_applicable_cases": {"when_not": "No wrapper involved"},
                        "source_artifacts": [{"path": "workspace_session.py"}],
                        "_similarity": 0.8,
                    }
                ],
            "failure_patterns": [],
            "conflict_cases": [],
            "retrieval_mode": "semantic_project",
            "project_scope": "same_project",
            "warnings": [],
            "total_retrieved": 1,
            "tokens_used_estimate": 88,
            "scope_learning_profile": {"sample_count": 3, "active": True},
            "memory_governance": {
                "enabled": True,
                "mode": "assist",
                "sections": {
                    "experiences": {
                        "states": {"hot": 1, "warm": 0, "cold": 0, "quarantined": 0},
                        "filtered_count": 0,
                    }
                },
            },
        },
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_session_playbooks",
        lambda *args, **kwargs: {
            "playbooks": [
                {
                    "id": "pb-0",
                    "title": "Previous wrapper fix",
                    "objective": "Keep wrapper stable",
                    "output_summary": "Validated run/record flows.",
                    "commands": ["pytest -q"],
                    "files_touched": ["workspace_session.py"],
                    "reusable_when": "Updating wrapper lifecycle",
                    "_similarity": 0.73,
                }
            ],
            "search_scope": "same_project",
            "warnings": [],
            "memory_governance": {
                "enabled": True,
                "mode": "assist",
                "states": {"hot": 1, "warm": 0, "cold": 0, "quarantined": 0},
                "filtered_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        session_manager,
        "analyze_problem_loop",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:abc",
            "problem_signature": "validate wrapper regression",
            "loop_risk": 0.28,
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
            "problem_fingerprint": "problem:abc",
            "problem_signature": "validate wrapper regression",
            "strategy_fingerprint": "strategy:def",
            "strategy_signature": "mcum semantic project",
            "loop_risk": 0.31,
            "risk_level": "low",
            "recommendation": "observe_only",
            "warnings": [],
        },
    )
    monkeypatch.setattr(session_manager, "log_session_start", lambda **kwargs: {"log_id": "session-start"})
    monkeypatch.setattr(session_manager, "record_retrieval_run", lambda **kwargs: "retrieval-run-1")

    def fake_log_entry(**kwargs):
        log_calls.append(kwargs)
        if kwargs["log_type"] == "decision":
            return "decision-log"
        return "task-log"

    monkeypatch.setattr(session_manager, "log_entry", fake_log_entry)
    monkeypatch.setattr(session_manager, "finalize_retrieval_run", lambda **kwargs: True)
    monkeypatch.setattr(session_manager, "adjust_confidence", lambda **kwargs: adjust_calls.append(kwargs) or True)
    monkeypatch.setattr(
        session_manager,
        "save_experience",
        lambda **kwargs: saved_experience_calls.append(kwargs) or "exp-saved",
    )
    monkeypatch.setattr(
        session_manager,
        "save_session_playbook",
        lambda **kwargs: playbook_calls.append(kwargs) or "playbook-1",
    )
    monkeypatch.setattr(
        session_manager,
        "log_session_end",
        lambda **kwargs: session_end_calls.append(kwargs) or "session-end-log",
    )
    monkeypatch.setattr(
        session_manager,
        "finalize_loop_state",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:abc",
            "strategy_fingerprint": "strategy:def",
            "loop_risk": 0.31,
            "risk_level": "low",
            "recommendation": "observe_only",
            "repeated_error_failures": 0,
            "warnings": [],
        },
    )
    monkeypatch.setattr(session_manager, "mark_skill_used", lambda skill_name: mark_used_calls.append(skill_name) or True)
    monkeypatch.setattr(session_manager, "run_autonomous_improvement", lambda **kwargs: {"skipped": True, "reason": "disabled"})
    monkeypatch.setattr(session_manager.time, "time", lambda: next(time_values))
    monkeypatch.setattr(session_manager.time, "perf_counter", lambda: next(perf_values))

    session = OrchestratorSession(
        project_path="C:/repo",
        project_name="MCUM",
        task_description="Validate wrapper regression suite",
        task_brief={"confirmed": True},
        verbose=False,
        auto_improve=False,
    )

    ctx = session.begin()
    close_result = session.close(
        TaskResult(
            task_description="Validate wrapper regression suite",
            skill_used=ctx.skill_selected,
            outcome="success",
            confidence_score=0.94,
            output_summary="Wrapper regression suite passed.",
            validation_summary="pytest .agent/skills/MCUM/tests -q",
            artifacts=[{"path": "C:/repo/report.txt", "exists": True, "type": "file"}],
            experience_data={
                "category": "testing_strategy",
                "title": "Wrapper regression validation",
                "content": {
                    "conclusion": "Run wrapper regression tests after lifecycle changes.",
                    "context": "workspace_session lifecycle paths changed",
                },
            },
            playbook_data={
                "title": "Wrapper regression playbook",
                "commands": ["pytest .agent/skills/MCUM/tests -q"],
                "files_touched": ["workspace_session.py", "tests/test_workspace_session.py"],
                "reusable_when": "Changing wrapper lifecycle or logging",
            },
        )
    )

    assert ctx.skill_status == "active"
    assert ctx.playbook_scope == "same_project"
    assert ctx.retrieval_latency_ms == 250
    assert ctx.compiled_state is not None
    assert ctx.compiled_state.selected_counts["experiences"] == 1
    assert ctx.compiled_state.selected_counts["playbooks"] == 1
    assert close_result["log_id"] == "task-log"
    assert close_result["record_status"] == "recorded"
    assert log_calls[0]["log_type"] == "decision"
    assert log_calls[0]["retrieval_latency_ms"] == 250
    assert log_calls[0]["log_metadata"]["compiled_context"]["selected_counts"]["playbooks"] == 1
    assert log_calls[0]["log_metadata"]["compiled_context"]["learning_profile_summary"]["sample_count"] == 4
    assert log_calls[0]["log_metadata"]["dispatch_learning"]["sample_count"] == 5
    assert log_calls[0]["log_metadata"]["retrieval_scope_learning"]["sample_count"] == 3
    assert log_calls[0]["log_metadata"]["memory_governance"]["sections"]["experiences"]["states"]["hot"] == 1
    assert log_calls[0]["log_metadata"]["playbook_memory_governance"]["states"]["hot"] == 1
    assert log_calls[0]["log_metadata"]["anti_loop"]["problem_fingerprint"] == "problem:abc"
    assert log_calls[1]["log_type"] == "task"
    assert log_calls[1]["task_wall_clock_ms"] == 4200
    assert log_calls[1]["retrieval_latency_ms"] == 250
    assert log_calls[1]["context_tokens_in"] > 0
    assert log_calls[1]["context_tokens_out"] > 0
    assert log_calls[1]["log_metadata"]["compiled_context"]["selected_counts"]["experiences"] == 1
    assert log_calls[1]["log_metadata"]["context_effectiveness"]["summary"]["items_adjusted"] >= 1
    assert log_calls[1]["log_metadata"]["memory_governance"]["sections"]["experiences"]["states"]["hot"] == 1
    assert log_calls[1]["log_metadata"]["playbook_memory_governance"]["states"]["hot"] == 1
    assert log_calls[1]["log_metadata"]["anti_loop"]["strategy_fingerprint"] == "strategy:def"
    assert saved_experience_calls[0]["source_artifacts"][0]["snapshot_type"] == "source_file"
    assert saved_experience_calls[0]["source_artifacts"][1]["snapshot_type"] == "project_structure"
    assert playbook_calls[0]["title"] == "Wrapper regression playbook"
    assert playbook_calls[0]["artifacts"][-1]["snapshot_type"] == "project_structure"
    assert playbook_calls[0]["source_task_log_id"] == "task-log"
    assert session_end_calls[0]["extra_metadata"]["playbook_id"] == "playbook-1"
    assert session_end_calls[0]["extra_metadata"]["dispatch_learning"]["sample_count"] == 5
    assert session_end_calls[0]["extra_metadata"]["compiled_context"]["selected_counts"]["warnings"] == 0
    assert session_end_calls[0]["extra_metadata"]["context_effectiveness"]["summary"]["items_adjusted"] >= 1
    assert session_end_calls[0]["extra_metadata"]["memory_governance"]["sections"]["experiences"]["states"]["hot"] == 1
    assert session_end_calls[0]["extra_metadata"]["playbook_memory_governance"]["states"]["hot"] == 1
    assert session_end_calls[0]["extra_metadata"]["anti_loop"]["recommendation"] == "observe_only"
    assert mark_used_calls == ["mcum-orchestrator"]
    assert any(call["experience_id"] == "exp-1" and call["delta"] > 0 for call in adjust_calls)


def test_forced_session_logs_shadow_auto_dispatch(monkeypatch) -> None:
    session_start_calls: list[dict] = []
    log_calls: list[dict] = []
    dispatch_calls: list[str | None] = []
    dispatch_hints_seen: list[dict | None] = []
    perf_values = iter([20.0, 20.1])

    monkeypatch.setattr(
        session_manager,
        "load_intake_policy",
        lambda: {
            "required_fields": [],
            "optional_fields": [],
            "allowed_task_types": ["validar"],
            "allowed_execution_modes": ["ejecutar"],
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
            "max_playbooks": 3,
            "min_playbook_similarity": 0.28,
        },
    )
    monkeypatch.setattr(session_manager, "sync_skill_catalog", lambda: {"skills_synced": 2})
    monkeypatch.setattr(session_manager, "get_skill_record", lambda skill_name: {"status": "active"})
    monkeypatch.setattr(
        session_manager,
        "get_dispatch_performance_profile",
        lambda **kwargs: {"active": True, "sample_count": 4},
    )
    monkeypatch.setattr(session_manager, "get_retrieval_scope_profile", lambda **kwargs: {})
    monkeypatch.setattr(session_manager, "get_context_effectiveness_profile", lambda **kwargs: {})
    monkeypatch.setattr(
        session_manager,
        "get_or_create_project",
        lambda **kwargs: {"id": "project-1", "project_name": "MCUM"},
    )

    def fake_dispatch(**kwargs):
        dispatch_calls.append(kwargs.get("force_skill"))
        dispatch_hints_seen.append(kwargs.get("dispatch_hints"))
        if kwargs.get("force_skill"):
            return DispatchResult(
                skill_name="html-dashboard-expert",
                confidence=1.0,
                match_method="forced_by_user",
                alternatives=[],
                triggered_by="user_override",
            )
        return DispatchResult(
            skill_name="ui-ux-pro-max",
            confidence=0.82,
            match_method="semantic",
            alternatives=[{"name": "html-dashboard-expert", "score": 0.74, "priority": 8}],
            triggered_by="semantic_score=0.82",
        )

    monkeypatch.setattr(session_manager, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        session_manager,
        "retrieve_for_task",
        lambda *args, **kwargs: {
            "experiences": [],
            "failure_patterns": [],
            "conflict_cases": [],
            "retrieval_mode": "semantic_project",
            "project_scope": "same_project",
            "warnings": [],
            "total_retrieved": 0,
            "tokens_used_estimate": 10,
            "scope_learning_profile": {},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_session_playbooks",
        lambda *args, **kwargs: {
            "playbooks": [],
            "search_scope": "same_project",
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "analyze_problem_loop",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:loop",
            "problem_signature": "dashboard retry",
            "loop_risk": 0.62,
            "risk_level": "high",
            "recommendation": "increase_validation_and_diverge",
            "success_escape_skills": ["validator-skill"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "enrich_loop_state_with_strategy",
        lambda **kwargs: {
            "enabled": True,
            "problem_fingerprint": "problem:loop",
            "strategy_fingerprint": "strategy:loop",
            "loop_risk": 0.71,
            "risk_level": "high",
            "recommendation": "switch_strategy_before_retry",
            "alternate_success_skills": ["validator-skill"],
            "warnings": ["Anti-loop: this same strategy has failed repeatedly on a similar task."],
        },
    )
    monkeypatch.setattr(
        session_manager,
        "log_session_start",
        lambda **kwargs: session_start_calls.append(kwargs) or {"log_id": "session-start"},
    )
    monkeypatch.setattr(session_manager, "record_retrieval_run", lambda **kwargs: "retrieval-run-1")
    monkeypatch.setattr(
        session_manager,
        "log_entry",
        lambda **kwargs: log_calls.append(kwargs) or "decision-log",
    )
    monkeypatch.setattr(session_manager.time, "perf_counter", lambda: next(perf_values))

    session = OrchestratorSession(
        project_path="C:/repo",
        project_name="MCUM",
        task_description="Crear dashboard ejecutivo de flota minera",
        force_skill="html-dashboard-expert",
        task_brief={"confirmed": True},
        verbose=False,
        auto_improve=False,
    )

    ctx = session.begin()

    assert dispatch_calls == [None, "html-dashboard-expert"]
    assert dispatch_hints_seen[0]["preferred_skills"] == ["validator-skill"]
    assert dispatch_hints_seen[1]["preferred_skills"] == ["validator-skill"]
    assert ctx.auto_dispatch_result is not None
    assert ctx.auto_dispatch_result.skill_name == "ui-ux-pro-max"
    assert ctx.anti_loop["validation_escalated"] is True
    assert "divergence_requested" in ctx.anti_loop["actions_applied"]
    assert "alternate_skill_hint" in ctx.anti_loop["actions_applied"]
    assert "Anti-loop: do not retry the same strategy without a material change." in session.task_brief["constraints"]
    assert any("validator-skill" in item for item in session.task_brief["sources_to_review"])
    assert session_start_calls[0]["extra_metadata"]["dispatch_learning"]["sample_count"] == 4
    assert session_start_calls[0]["extra_metadata"]["dispatch_hints"]["preferred_skills"] == ["validator-skill"]
    assert session_start_calls[0]["extra_metadata"]["auto_dispatch"]["skill_name"] == "ui-ux-pro-max"
    assert session_start_calls[0]["extra_metadata"]["anti_loop"]["recommendation"] == "switch_strategy_before_retry"
    assert log_calls[0]["log_metadata"]["dispatch_learning"]["sample_count"] == 4
    assert log_calls[0]["log_metadata"]["dispatch_hints"]["preferred_skills"] == ["validator-skill"]
    assert log_calls[0]["log_metadata"]["auto_dispatch"]["skill_name"] == "ui-ux-pro-max"
    assert "Anti-loop control active" in ctx.warnings[-1]


def test_apply_result_policy_requires_explicit_validation_when_anti_loop_escalates(monkeypatch) -> None:
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
            "strict_mode": True,
            "require_task_brief": False,
            "require_validation_before_success": True,
            "require_artifacts_for_success": False,
            "allow_cross_project_fallback": True,
            "block_on_policy_violation": True,
            "anti_loop": {"enabled": True},
        },
    )

    session = OrchestratorSession(
        project_path="C:/repo",
        task_description="Retry wrapper analysis",
        task_brief={"confirmed": True, "execution_mode": "analizar"},
        verbose=False,
        auto_improve=False,
    )
    session._ctx = SimpleNamespace(anti_loop={"enabled": True, "validation_escalated": True})

    adjusted = session._apply_result_policy(
        TaskResult(
            task_description="Retry wrapper analysis",
            skill_used="mcum-orchestrator",
            outcome="success",
            confidence_score=0.92,
            output_summary="The issue is likely fixed.",
            validation_summary=None,
        )
    )

    assert adjusted.outcome == "partial"
    assert "anti_loop_validation_required" in (adjusted.error_description or "")


def test_session_logs_implicit_skill_correction_and_delegation(monkeypatch) -> None:
    log_calls: list[dict] = []
    session_end_calls: list[dict] = []
    mark_used_calls: list[str] = []
    time_values = iter([300.0, 306.0, 306.0])
    perf_values = iter([40.0, 40.05])

    monkeypatch.setattr(
        session_manager,
        "load_intake_policy",
        lambda: {
            "required_fields": [],
            "optional_fields": [],
            "allowed_task_types": ["validar"],
            "allowed_execution_modes": ["ejecutar"],
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
            "max_playbooks": 3,
            "min_playbook_similarity": 0.28,
        },
    )
    monkeypatch.setattr(session_manager, "sync_skill_catalog", lambda: {"skills_synced": 2})
    monkeypatch.setattr(session_manager, "get_skill_record", lambda skill_name: {"status": "active"})
    monkeypatch.setattr(
        session_manager,
        "get_dispatch_performance_profile",
        lambda **kwargs: {"active": True, "sample_count": 6},
    )
    monkeypatch.setattr(session_manager, "get_retrieval_scope_profile", lambda **kwargs: {})
    monkeypatch.setattr(session_manager, "get_context_effectiveness_profile", lambda **kwargs: {})
    monkeypatch.setattr(
        session_manager,
        "get_or_create_project",
        lambda **kwargs: {"id": "project-1", "project_name": "MCUM"},
    )
    monkeypatch.setattr(
        session_manager,
        "dispatch",
        lambda **kwargs: DispatchResult(
            skill_name="ui-ux-pro-max",
            confidence=0.78,
            match_method="semantic",
            alternatives=[{"name": "html-dashboard-expert", "score": 0.71, "priority": 8}],
            triggered_by="semantic_score=0.78",
        ),
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_for_task",
        lambda *args, **kwargs: {
            "experiences": [],
            "failure_patterns": [],
            "conflict_cases": [],
            "retrieval_mode": "semantic_project",
            "project_scope": "same_project",
            "warnings": [],
            "total_retrieved": 0,
            "tokens_used_estimate": 22,
            "scope_learning_profile": {},
        },
    )
    monkeypatch.setattr(
        session_manager,
        "retrieve_session_playbooks",
        lambda *args, **kwargs: {
            "playbooks": [],
            "search_scope": "same_project",
            "warnings": [],
        },
    )
    monkeypatch.setattr(session_manager, "log_session_start", lambda **kwargs: {"log_id": "session-start"})
    monkeypatch.setattr(session_manager, "record_retrieval_run", lambda **kwargs: "retrieval-run-1")

    def fake_log_entry(**kwargs):
        log_calls.append(kwargs)
        if kwargs["log_type"] == "decision":
            return "decision-log"
        return "task-log"

    monkeypatch.setattr(session_manager, "log_entry", fake_log_entry)
    monkeypatch.setattr(session_manager, "finalize_retrieval_run", lambda **kwargs: True)
    monkeypatch.setattr(session_manager, "adjust_confidence", lambda **kwargs: True)
    monkeypatch.setattr(session_manager, "save_session_playbook", lambda **kwargs: "playbook-1")
    monkeypatch.setattr(
        session_manager,
        "log_session_end",
        lambda **kwargs: session_end_calls.append(kwargs) or "session-end-log",
    )
    monkeypatch.setattr(session_manager, "mark_skill_used", lambda skill_name: mark_used_calls.append(skill_name) or True)
    monkeypatch.setattr(session_manager, "run_autonomous_improvement", lambda **kwargs: {"skipped": True, "reason": "disabled"})
    monkeypatch.setattr(session_manager.time, "time", lambda: next(time_values))
    monkeypatch.setattr(session_manager.time, "perf_counter", lambda: next(perf_values))

    session = OrchestratorSession(
        project_path="C:/repo",
        project_name="MCUM",
        task_description="Crear dashboard ejecutivo con glassmorphism para flota minera",
        task_brief={"confirmed": True},
        verbose=False,
        auto_improve=False,
    )

    session.begin()
    session.close(
        TaskResult(
            task_description="Crear dashboard ejecutivo con glassmorphism para flota minera",
            skill_used="html-dashboard-expert",
            outcome="success",
            confidence_score=0.9,
            output_summary="Se resolvio delegando la ejecucion final al especialista HTML.",
            validation_summary="Smoke test manual del dashboard",
            skills_orchestrated=["ui-ux-pro-max", "html-dashboard-expert"],
            correction_source="workspace_session_final_skill_override",
        )
    )

    task_log = log_calls[-1]
    assert task_log["skills_orchestrated"] == ["ui-ux-pro-max", "html-dashboard-expert"]
    assert task_log["log_metadata"]["selected_skill"] == "ui-ux-pro-max"
    assert task_log["log_metadata"]["dispatch_learning"]["sample_count"] == 6
    assert task_log["log_metadata"]["final_skill"] == "html-dashboard-expert"
    assert task_log["log_metadata"]["skill_correction"]["implicit"] is True
    assert task_log["log_metadata"]["skill_correction"]["source"] == "workspace_session_final_skill_override"
    assert session_end_calls[0]["extra_metadata"]["dispatch_learning"]["sample_count"] == 6
    assert session_end_calls[0]["extra_metadata"]["skill_correction"]["changed"] is True
    assert mark_used_calls == ["ui-ux-pro-max", "html-dashboard-expert"]
