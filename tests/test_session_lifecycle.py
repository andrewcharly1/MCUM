from __future__ import annotations

from types import SimpleNamespace

from MCUM.core.session_manager import OrchestratorSession
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
