from __future__ import annotations

from types import SimpleNamespace

from MCUM.core.multi_agent import build_multi_agent_plan, resolve_orchestration_context
from MCUM.core.session_manager import OrchestratorSession, TaskResult
from MCUM.policy import load_execution_policy, normalize_task_brief


def test_normalize_task_brief_preserves_multi_agent_fields() -> None:
    brief = normalize_task_brief(
        "C:/repo",
        "Fix a complex issue",
        task_brief={
            "task_type": "corregir",
            "objective": "Fix the issue safely.",
            "expected_deliverable": "Validated fix",
            "success_criteria": "The issue is fixed and tested.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "parent_task_id": "parent-1",
            "orchestration_role": "worker",
            "worker_role": "validator",
        },
    )

    assert brief["parent_task_id"] == "parent-1"
    assert brief["orchestration_role"] == "worker"
    assert brief["worker_role"] == "validator"


def test_build_multi_agent_plan_recommends_supervised_workers_for_complex_task() -> None:
    execution_policy = load_execution_policy()
    brief = normalize_task_brief(
        "C:/repo",
        "Fix a cross-cutting issue safely",
        task_brief={
            "task_type": "corregir",
            "objective": "Fix the issue safely.",
            "expected_deliverable": "Validated fix",
            "success_criteria": "The issue is fixed and tested.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "task_id": "task-1",
            "editable_scope": "src",
            "read_only_scope": "tests",
            "supervised_multi_agent": True,
        },
    )

    plan = build_multi_agent_plan(
        brief,
        execution_policy,
        selected_skill="mcum-orchestrator",
    )

    assert plan["mode"] == "supervised"
    assert len(plan["workers"]) == 3
    assert plan["worker_policy"]["max_write_workers"] == 1
    assert sum(1 for worker in plan["workers"] if worker["mode"] == "write") == 1
    assert plan["worker_briefs"][0]["parent_task_id"] == "task-1"


def test_build_multi_agent_plan_prefers_anti_loop_write_skill_hint() -> None:
    execution_policy = load_execution_policy()
    brief = normalize_task_brief(
        "C:/repo",
        "Retry with an alternate validated strategy",
        task_brief={
            "task_type": "corregir",
            "objective": "Switch away from a repeated failing path.",
            "expected_deliverable": "Validated alternate path",
            "success_criteria": "A different write strategy is used and validated.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "task_id": "task-anti-loop",
            "editable_scope": "src",
            "read_only_scope": "tests",
            "supervised_multi_agent": True,
            "selected_skill_hint": "mcum-orchestrator",
            "preferred_write_skill_hint": "validator-specialist",
            "anti_loop_force_multi_run": True,
        },
    )

    plan = build_multi_agent_plan(
        brief,
        execution_policy,
        selected_skill="mcum-orchestrator",
    )

    write_workers = [worker for worker in plan["workers"] if worker["mode"] == "write"]
    assert plan["anti_loop_recommended"] is True
    assert plan["preferred_write_skill_hint"] == "validator-specialist"
    assert len(write_workers) == 1
    assert write_workers[0]["skill_hint"] == "validator-specialist"


def test_build_multi_agent_plan_assigns_cost_aware_models() -> None:
    execution_policy = load_execution_policy()
    brief = normalize_task_brief(
        "C:/repo",
        "Improve the local MCUM worker routing without changing the cloud project",
        task_brief={
            "task_type": "mejorar",
            "objective": "Add model-aware routing to supervised workers.",
            "expected_deliverable": "Validated local MCUM model routing.",
            "success_criteria": "Workers receive model recommendations and cost estimates.",
            "execution_mode": "ejecutar",
            "risk_level": "medio",
            "confirmed": True,
            "task_id": "task-model-routing",
            "editable_scope": "core",
            "read_only_scope": "tests",
            "supervised_multi_agent": True,
        },
    )

    plan = build_multi_agent_plan(
        brief,
        execution_policy,
        selected_skill="mcum-orchestrator",
    )

    workers_by_role = {worker["role"]: worker for worker in plan["workers"]}
    assert plan["model_routing"]["enabled"] is True
    assert plan["coordinator"]["recommended_model"] in {"gpt-5.3-codex", "gpt-5.5"}
    assert workers_by_role["context_analyst"]["agent_profile"] == "context-retriever"
    assert workers_by_role["context_analyst"]["recommended_model"] == "gpt-5.4-mini"
    assert workers_by_role["implementer"]["agent_profile"] == "builder-codex"
    assert workers_by_role["implementer"]["recommended_model"] == "gpt-5.3-codex"
    assert workers_by_role["validator"]["agent_profile"] == "validator-qa"
    assert plan["worker_briefs"][1]["recommended_model"] == "gpt-5.3-codex"
    assert plan["model_routing"]["summary"]["estimated_savings_ratio"] > 0


def test_build_multi_agent_plan_attaches_same_project_context_trace_to_workers() -> None:
    execution_policy = load_execution_policy()
    brief = normalize_task_brief(
        "C:/repo",
        "Implement and validate a bounded graph change",
        task_brief={
            "task_type": "mejorar",
            "objective": "Implement and validate the graph change.",
            "expected_deliverable": "Validated implementation.",
            "success_criteria": "Workers receive project context.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "supervised_multi_agent": True,
            "project_context_envelope": {
                "version": "1.0",
                "envelope_hash": "envelope-1",
                "context_pack_id": "pack-1",
                "snapshot": {"snapshot_id": "snapshot-1"},
                "project": {"id": "project-1", "name": "Demo", "path": "C:/repo"},
                "query_plan": {"primary_intent": "change"},
                "task_contract": {"objective": "Implement and validate the graph change."},
                "selected_skill": {"name": "mcum-orchestrator", "status": "active"},
                "graph_context": {"code_locations": [], "primary_entities": []},
                "operational_memory": {"experiences": [], "patterns": [], "failures": []},
            },
        },
    )

    plan = build_multi_agent_plan(brief, execution_policy, selected_skill="mcum-orchestrator")

    assert plan["worker_briefs"]
    for worker_brief in plan["worker_briefs"]:
        context_slice = worker_brief["worker_context_slice"]
        assert context_slice["context_pack_id"] == "pack-1"
        assert context_slice["graph_snapshot_id"] == "snapshot-1"
        assert context_slice["project"]["path"] == "C:/repo"
        assert context_slice["worker"]["writeback"] == "coordinator_only"


def test_high_risk_architecture_routes_coordinator_to_deep_model() -> None:
    execution_policy = load_execution_policy()
    brief = normalize_task_brief(
        "C:/repo",
        "Design a multitenant SaaS architecture with security review",
        task_brief={
            "task_type": "planificar",
            "objective": "Design critical architecture and security boundaries.",
            "expected_deliverable": "Architecture decision with risks.",
            "success_criteria": "Security and multitenancy risks are explicit.",
            "execution_mode": "proponer",
            "risk_level": "alto",
            "confirmed": True,
            "supervised_multi_agent": True,
        },
    )

    plan = build_multi_agent_plan(brief, execution_policy, selected_skill="mcum-orchestrator")

    assert plan["coordinator"]["agent_profile"] == "orchestrator-master"
    assert plan["coordinator"]["recommended_model"] == "gpt-5.5"
    assert plan["model_routing"]["coordinator"]["decision"] == "deep_reasoning"


def test_resolve_orchestration_context_defaults_worker_to_coordinator_only_writeback() -> None:
    execution_policy = load_execution_policy()
    context = resolve_orchestration_context(
        {
            "project_path": "C:/repo",
            "task_type": "corregir",
            "objective": "Fix safely.",
            "expected_deliverable": "Validated fix",
            "success_criteria": "Fixed",
            "execution_mode": "ejecutar",
            "confirmed": True,
            "orchestration_role": "worker",
            "worker_role": "validator",
            "parent_task_id": "parent-1",
        },
        execution_policy,
    )

    assert context["role"] == "worker"
    assert context["suppress_autonomy_hooks"] is True
    assert context["allow_learning_writes"] is False
    assert context["writeback_mode"] == "coordinator_only"


def test_worker_session_defers_learning_and_skips_autonomy_hooks(monkeypatch) -> None:
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
        active_patterns=[],
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
        orchestration={"role": "worker"},
    )
    session._start_ts = 0.0
    session._closed = False
    session.session_id = "session-1"
    session.task_description = "fix wrapper output"
    session.task_brief = {"execution_mode": "ejecutar"}
    session.project_path = "C:/tmp/project"
    session.execution_policy = {"strict_mode": False}
    session.orchestration_context = {
        "role": "worker",
        "suppress_autonomy_hooks": True,
        "allow_learning_writes": False,
    }
    session.auto_improve = False
    session.force_skill = None
    session.verbose = False
    session._apply_result_policy = lambda result: result  # type: ignore[method-assign]
    session._build_orchestrated_skills = lambda result: []  # type: ignore[method-assign]
    session._build_skill_correction_metadata = lambda result, orchestrated_skills: {  # type: ignore[method-assign]
        "delegated_skills": []
    }
    session._dispatch_result_payload = lambda result: None  # type: ignore[method-assign]
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
    monkeypatch.setattr(
        "MCUM.core.session_manager.save_experience",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("save_experience should not run for worker sessions")),
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.save_session_playbook",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("save_session_playbook should not run for worker sessions")),
    )
    reinforce_called = {"value": False}
    autonomy_called = {"value": False}
    factory_called = {"value": False}
    session._reinforce_retrieval_confidence = lambda outcome, context_effectiveness=None: reinforce_called.update(value=True)  # type: ignore[method-assign]
    session._run_autonomous_improvement = lambda skill_name: autonomy_called.update(value=True)  # type: ignore[method-assign]
    session._run_skill_factory_cycle = lambda: factory_called.update(value=True)  # type: ignore[method-assign]

    close_result = OrchestratorSession.close(
        session,
        TaskResult(
            task_description="fix wrapper output",
            skill_used="mcum-orchestrator",
            outcome="success",
            confidence_score=0.95,
            output_summary="done",
            experience_data={
                "category": "implementation_recipe",
                "content": {"conclusion": "done"},
            },
            playbook_data={"files_touched": ["wrapper.py"]},
        ),
    )

    assert close_result["log_id"] == "task-log-1"
    assert close_result["record_status"] == "recorded"
    assert task_logs[0]["log_metadata"]["learning_writeback_deferred"] is True
    assert session_ends[0]["extra_metadata"]["learning_writeback_deferred"] is True
    assert reinforce_called["value"] is False
    assert autonomy_called["value"] is False
    assert factory_called["value"] is False


def test_save_session_playbook_prefers_compact_overrides(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(project_id="project-1")
    session.project_path = "C:/tmp/project"
    session.session_id = "session-1"
    session.task_brief = {}
    captured: dict[str, object] = {}
    session._log = lambda message: None  # type: ignore[method-assign]

    monkeypatch.setattr(
        "MCUM.core.session_manager.build_project_structure_snapshots",
        lambda project_path, extra_paths=None: [],
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.save_session_playbook",
        lambda **kwargs: captured.update(kwargs) or "playbook-1",
    )

    playbook_id = OrchestratorSession._save_session_playbook(
        session,
        TaskResult(
            task_description="Coordinate a supervised multi-run",
            skill_used="mcum-orchestrator",
            outcome="success",
            confidence_score=0.95,
            output_summary="verbose coordinator summary",
            validation_summary="verbose validation",
            artifacts=[],
            playbook_data={
                "title": "Coordinated multi-run",
                "objective": "Compact reusable path",
                "output_summary": "compact context summary",
                "validation_summary": "compact validation summary",
                "commands": ["implementer: Write-Output ok"],
                "files_touched": [],
            },
        ),
        "task-log-1",
    )

    assert playbook_id == "playbook-1"
    assert captured["output_summary"] == "compact context summary"
    assert captured["validation_summary"] == "compact validation summary"
