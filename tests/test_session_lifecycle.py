from __future__ import annotations

from types import SimpleNamespace

from MCUM.core.session_manager import OrchestratorSession
from MCUM.core.session_manager import TaskResult
from MCUM.workspace_session import _abort_active_session


def test_abort_builds_failure_task_result() -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(skill_selected="mcum-orchestrator")
    session.task_description = "sample task"

    captured: dict[str, object] = {}

    def fake_close(result):
        captured["result"] = result
        return "log-123"

    session.close = fake_close  # type: ignore[method-assign]

    log_id = OrchestratorSession.abort(
        session,
        error_description="boom",
        output_summary="aborted",
        validation_summary="validation note",
        confidence_score=0.2,
    )

    result = captured["result"]
    assert log_id == "log-123"
    assert result.outcome == "failure"
    assert result.skill_used == "mcum-orchestrator"
    assert result.task_description == "sample task"
    assert result.error_description == "boom"
    assert result.output_summary == "aborted"
    assert result.validation_summary == "validation note"
    assert result.confidence_score == 0.2


def test_abort_active_session_returns_none_without_context() -> None:
    session = SimpleNamespace(context=None)
    assert _abort_active_session(session, "boom", "summary") is None


def test_abort_active_session_delegates_to_session_abort() -> None:
    captured: dict[str, object] = {}

    class FakeSession:
        context = object()

        def abort(self, **kwargs):
            captured.update(kwargs)
            return "log-456"

    log_id = _abort_active_session(
        FakeSession(),
        error_description="boom",
        output_summary="summary",
        validation_summary="validation",
    )

    assert log_id == "log-456"
    assert captured == {
        "error_description": "boom",
        "output_summary": "summary",
        "validation_summary": "validation",
    }


def test_abort_active_session_logs_warning_when_abort_fails(caplog) -> None:
    class FakeSession:
        context = object()

        def abort(self, **kwargs):
            raise RuntimeError("abort failed")

    with caplog.at_level("WARNING"):
        result = _abort_active_session(FakeSession(), "boom", "summary")

    assert result is None
    assert "abort failed" in caplog.text


def _task_result() -> TaskResult:
    return TaskResult(
        task_description="update graph",
        skill_used="mcum-orchestrator",
        outcome="success",
        confidence_score=0.95,
    )


def test_close_always_finalizes_task_graph_after_success() -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(skill_selected="mcum-orchestrator")
    session._close_impl = lambda result: {"log_id": "log-1"}  # type: ignore[method-assign]
    session._finalize_task_graph = lambda **kwargs: {"status": "success"}  # type: ignore[method-assign]

    result = OrchestratorSession.close(session, _task_result())

    assert result["graph_finalization"] == {"status": "success"}


def test_close_finalizes_task_graph_even_when_close_impl_fails() -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(skill_selected="mcum-orchestrator")
    finalized = {"value": False}

    def fail_close(result):
        raise RuntimeError("close failed")

    session._close_impl = fail_close  # type: ignore[method-assign]
    session._finalize_task_graph = lambda **kwargs: finalized.update(value=True) or {}  # type: ignore[method-assign]

    try:
        OrchestratorSession.close(session, _task_result())
    except RuntimeError as exc:
        assert str(exc) == "close failed"
    else:
        raise AssertionError("close should propagate the original failure")

    assert finalized["value"] is True


def test_task_graph_finalization_uses_explicit_project_path(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session.execution_policy = {
        "code_graph": {"enabled": True, "auto_sync": True, "coordinator_only_sync": True},
        "graph_intelligence": {"enabled": True, "coordinator_only_sync": True},
    }
    session.orchestration_context = {"role": "coordinator"}
    session._project = {"id": "11111111-1111-1111-1111-111111111111"}
    session.project_path = "C:/workspace/Project-A"
    session.project_name = "Project-A"
    session.session_id = "session-1"
    session._close_code_graph_sync = {}
    session._log = lambda message: None  # type: ignore[method-assign]
    calls: dict[str, dict] = {}

    def fake_code_sync(**kwargs):
        calls["code_graph"] = kwargs
        return {"status": "no_changes", "trigger": "task_end", "delta": {"has_changes": False}}

    def fake_unified_sync(**kwargs):
        calls["unified_graph"] = kwargs
        return {"status": "success"}

    monkeypatch.setattr("MCUM.core.session_manager.sync_project_code_graph", fake_code_sync)
    monkeypatch.setattr("MCUM.core.session_manager.sync_unified_project_graph", fake_unified_sync)

    OrchestratorSession._finalize_task_graph(session, selected_skill="mcum-orchestrator")

    assert calls["code_graph"]["project_path"] == "C:/workspace/Project-A"
    assert calls["code_graph"]["trigger"] == "task_end"
    assert calls["unified_graph"]["project_id"] == "11111111-1111-1111-1111-111111111111"
    assert calls["unified_graph"]["trigger"] == "task_end"
    assert calls["unified_graph"]["code_graph_sync"]["trigger"] == "task_end"


def test_task_graph_finalization_reuses_close_code_graph_sync(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session.execution_policy = {
        "code_graph": {"enabled": True, "auto_sync": True, "coordinator_only_sync": True},
        "graph_intelligence": {"enabled": True, "coordinator_only_sync": True},
    }
    session.orchestration_context = {"role": "coordinator"}
    session._project = {"id": "11111111-1111-1111-1111-111111111111"}
    session.project_path = "C:/workspace/Project-A"
    session.project_name = "Project-A"
    session.session_id = "session-1"
    session._close_code_graph_sync = {
        "status": "success",
        "trigger": "session_close",
        "delta": {"has_changes": True, "changed_paths": ["src/app.py"]},
    }
    session._log = lambda message: None  # type: ignore[method-assign]
    calls: dict[str, dict] = {}

    monkeypatch.setattr(
        "MCUM.core.session_manager.sync_project_code_graph",
        lambda **kwargs: pytest.fail("task_end must reuse the session_close code graph sync"),
    )

    def fake_unified_sync(**kwargs):
        calls["unified_graph"] = kwargs
        return {"status": "success"}

    monkeypatch.setattr("MCUM.core.session_manager.sync_unified_project_graph", fake_unified_sync)

    result = OrchestratorSession._finalize_task_graph(session, selected_skill="mcum-orchestrator")

    assert result["code_graph"]["reused_for_trigger"] == "task_end"
    assert calls["unified_graph"]["code_graph_sync"]["trigger"] == "session_close"
