from __future__ import annotations

from MCUM.core.dispatcher import DispatchResult
from MCUM.core.session_manager import OrchestratorSession, TaskResult
from MCUM.core import session_manager


def test_session_persists_metrics_and_playbook(monkeypatch) -> None:
    log_calls: list[dict] = []
    session_end_calls: list[dict] = []
    playbook_calls: list[dict] = []
    mark_used_calls: list[str] = []
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
    monkeypatch.setattr(session_manager, "sync_skill_catalog", lambda: {"skills_synced": 2})
    monkeypatch.setattr(session_manager, "get_skill_record", lambda skill_name: {"status": "active"})
    monkeypatch.setattr(session_manager, "get_or_create_project", lambda **kwargs: {"id": "project-1", "project_name": "MCUM"})
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
    monkeypatch.setattr(session_manager, "save_experience", lambda **kwargs: "exp-saved")
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
    log_id = session.close(
        TaskResult(
            task_description="Validate wrapper regression suite",
            skill_used=ctx.skill_selected,
            outcome="success",
            confidence_score=0.94,
            output_summary="Wrapper regression suite passed.",
            validation_summary="pytest .agent/skills/MCUM/tests -q",
            artifacts=[{"path": "C:/repo/report.txt", "exists": True, "type": "file"}],
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
    assert log_id == "task-log"
    assert log_calls[0]["log_type"] == "decision"
    assert log_calls[0]["retrieval_latency_ms"] == 250
    assert log_calls[1]["log_type"] == "task"
    assert log_calls[1]["task_wall_clock_ms"] == 4200
    assert log_calls[1]["retrieval_latency_ms"] == 250
    assert log_calls[1]["context_tokens_in"] > 0
    assert log_calls[1]["context_tokens_out"] > 0
    assert playbook_calls[0]["title"] == "Wrapper regression playbook"
    assert playbook_calls[0]["source_task_log_id"] == "task-log"
    assert session_end_calls[0]["extra_metadata"]["playbook_id"] == "playbook-1"
    assert mark_used_calls == ["mcum-orchestrator"]


def test_forced_session_logs_shadow_auto_dispatch(monkeypatch) -> None:
    session_start_calls: list[dict] = []
    log_calls: list[dict] = []
    dispatch_calls: list[str | None] = []
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
        "get_or_create_project",
        lambda **kwargs: {"id": "project-1", "project_name": "MCUM"},
    )

    def fake_dispatch(**kwargs):
        dispatch_calls.append(kwargs.get("force_skill"))
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
    assert ctx.auto_dispatch_result is not None
    assert ctx.auto_dispatch_result.skill_name == "ui-ux-pro-max"
    assert session_start_calls[0]["extra_metadata"]["auto_dispatch"]["skill_name"] == "ui-ux-pro-max"
    assert log_calls[0]["log_metadata"]["auto_dispatch"]["skill_name"] == "ui-ux-pro-max"


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
    assert task_log["log_metadata"]["final_skill"] == "html-dashboard-expert"
    assert task_log["log_metadata"]["skill_correction"]["implicit"] is True
    assert task_log["log_metadata"]["skill_correction"]["source"] == "workspace_session_final_skill_override"
    assert session_end_calls[0]["extra_metadata"]["skill_correction"]["changed"] is True
    assert mark_used_calls == ["ui-ux-pro-max", "html-dashboard-expert"]
