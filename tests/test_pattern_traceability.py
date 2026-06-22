from __future__ import annotations

from types import SimpleNamespace

from MCUM.core.session_manager import OrchestratorSession, TaskResult
from MCUM.db import project_registry


def _pattern_row(pattern_ids_used: list[str] | None = None) -> dict:
    return {
        "skill_used": "mcum-orchestrator",
        "pattern_ids_used": pattern_ids_used or [],
        "outcome": "success",
        "context_tokens_in": 1000,
        "task_wall_clock_ms": 5000,
        "retrieval_latency_ms": 200,
        "log_metadata": {
            "task_brief": {
                "task_type": "corregir",
                "execution_mode": "ejecutar",
            },
            "context_effectiveness": {
                "items": [
                    {
                        "id": "item-1",
                        "section": "failure_patterns",
                        "selected": True,
                        "effectiveness": "high",
                        "support_score": 0.9,
                        "utility_reasons": ["pattern"],
                    }
                ],
                "summary": {
                    "selected_items": 1,
                    "high_value_selected": 1,
                    "missed_opportunities": 0,
                    "items_evaluated": 1,
                },
            },
            "compiled_context": {
                "selected_items_summary": {
                    "failure_patterns": [{"token_cost": 10}],
                }
            },
        },
    }


def test_close_records_pattern_ids_used_for_task_and_session_logs(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(
        session_id="session-1",
        project_id="project-1",
        project_name="Project",
        task_description="fix wrapper output",
        skill_selected="mcum-orchestrator",
        dispatch_result=SimpleNamespace(match_method="semantic", triggered_by="test"),
        auto_dispatch_result=None,
        retrieved_experiences=[],
        failure_patterns=[],
        conflict_cases=[],
        active_patterns=[{"id": "pat-1"}, {"id": "pat-2"}],
        feedback_signals={},
        retrieval_mode="semantic",
        session_start_ts=0.0,
        log_id="log-1",
        retrieval_run_id="run-1",
        warnings=[],
        task_brief={"execution_mode": "ejecutar"},
        project_scope="same_project",
        playbooks=[],
        playbook_scope="none",
        retrieval_latency_ms=0,
        skill_status="active",
        compiled_state=SimpleNamespace(
            estimated_tokens=0,
            to_metadata=lambda: {"mode": "stub"},
        ),
        retrieval_scope_learning=None,
        dispatch_learning_profile=None,
    )
    session._start_ts = 0.0
    session._closed = False
    session.session_id = "session-1"
    session.task_description = "fix wrapper output"
    session.task_brief = {"execution_mode": "ejecutar"}
    session.project_path = "C:/tmp/project"
    session.execution_policy = {"strict_mode": False}
    session.auto_improve = False
    session.force_skill = None
    session.verbose = False
    session._apply_result_policy = lambda result: result  # type: ignore[method-assign]
    session._build_orchestrated_skills = lambda result: []  # type: ignore[method-assign]
    session._build_skill_correction_metadata = lambda result, orchestrated_skills: {  # type: ignore[method-assign]
        "delegated_skills": []
    }
    session._dispatch_result_payload = lambda result: None  # type: ignore[method-assign]
    session._save_session_playbook = lambda result, log_id: None  # type: ignore[method-assign]
    session._reinforce_retrieval_confidence = lambda outcome, context_effectiveness=None: None  # type: ignore[method-assign]
    session._run_autonomous_improvement = lambda skill_name: None  # type: ignore[method-assign]
    session._run_skill_factory_cycle = lambda: None  # type: ignore[method-assign]
    session._log = lambda message: None  # type: ignore[method-assign]

    task_logs: list[dict] = []
    session_ends: list[dict] = []
    monkeypatch.setattr(
        "MCUM.core.session_manager.finalize_retrieval_run",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.log_entry",
        lambda **kwargs: task_logs.append(kwargs) or "task-log-1",
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.log_session_end",
        lambda **kwargs: session_ends.append(kwargs) or "session-end-1",
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.mark_skill_used",
        lambda skill_name: True,
    )
    pattern_usage_calls: list[dict] = []
    monkeypatch.setattr(
        "MCUM.core.session_manager.record_pattern_usage_events",
        lambda **kwargs: pattern_usage_calls.append(kwargs) or {"status": "success", "events_recorded": 2},
    )

    close_result = OrchestratorSession.close(
        session,
        TaskResult(
            task_description="fix wrapper output",
            skill_used="mcum-orchestrator",
            outcome="success",
            confidence_score=0.95,
            output_summary="done",
            user_feedback=1,
        ),
    )

    assert close_result["log_id"] == "task-log-1"
    assert close_result["record_status"] == "recorded"
    assert task_logs[0]["pattern_ids_used"] == ["pat-1", "pat-2"]
    assert task_logs[0]["log_metadata"]["pattern_ids_used"] == ["pat-1", "pat-2"]
    assert session_ends[0]["pattern_ids_used"] == ["pat-1", "pat-2"]
    assert session_ends[0]["extra_metadata"]["pattern_ids_used"] == ["pat-1", "pat-2"]
    assert pattern_usage_calls[0]["pattern_ids"] == ["pat-1", "pat-2"]
    assert session_ends[0]["extra_metadata"]["pattern_usage"]["events_recorded"] == 2


def test_context_effectiveness_profile_uses_pattern_signal() -> None:
    plain_rows = [_pattern_row() for _ in range(3)]
    pattern_rows = [_pattern_row(["pat-1", "pat-2"]) for _ in range(3)]

    plain_profile = project_registry.derive_context_effectiveness_profile(
        plain_rows,
        skill_name="mcum-orchestrator",
        task_type="corregir",
        execution_mode="ejecutar",
        min_samples=3,
    )
    pattern_profile = project_registry.derive_context_effectiveness_profile(
        pattern_rows,
        skill_name="mcum-orchestrator",
        task_type="corregir",
        execution_mode="ejecutar",
        min_samples=3,
    )

    assert pattern_profile["pattern_usage_summary"]["usage_rate"] == 1.0
    assert pattern_profile["pattern_usage_summary"]["mean_pattern_ids_used"] == 2.0
    assert pattern_profile["efficiency_adjustments"]["failure_patterns"] > plain_profile["efficiency_adjustments"]["failure_patterns"]
