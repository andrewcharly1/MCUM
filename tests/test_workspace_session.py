from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from MCUM import workspace_session


class _FakeSession:
    instances: list["_FakeSession"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.context = None
        self.closed_result = None
        _FakeSession.instances.append(self)

    def begin(self) -> SimpleNamespace:
        self.context = SimpleNamespace(
            skill_selected=self.kwargs.get("force_skill") or "mcum-orchestrator",
            session_id="session-123",
        )
        return self.context

    def close(self, result):
        self.closed_result = result
        return "log-123"

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()


def _base_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "project_path": str(tmp_path),
        "project_name": "mcum-orchestrator",
        "task": "Validate MCUM wrapper flows",
        "artifact": [],
        "force_skill": None,
        "final_skill": None,
        "delegated_skill": [],
        "save_experience": False,
        "experience_title": None,
        "experience_category": "implementation_recipe",
        "conclusion": None,
        "context": None,
        "quiet": True,
        "no_auto_improve": True,
        "interactive_intake": False,
        "task_type": "validar",
        "objective": "Validate wrapper execution",
        "expected_deliverable": "Passing smoke tests",
        "source_to_review": [],
        "constraint": [],
        "success_criteria": "The wrapper records the session outcome",
        "execution_mode": "ejecutar",
        "risk_level": "medio",
        "validation_required": None,
        "error_description": None,
        "validation_summary": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_run_command_records_successful_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ok", encoding="utf-8")
    args = _base_args(
        tmp_path,
        artifact=["artifact.txt"],
        command="Write-Output ok",
        workdir=None,
        timeout=30,
        summary=None,
        confidence_success=0.93,
        confidence_failure=0.2,
    )
    recorded_run: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    def fake_run(command, **kwargs):
        recorded_run["command"] = command
        recorded_run["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="wrapper ok", stderr="")

    monkeypatch.setattr(workspace_session.subprocess, "run", fake_run)

    exit_code = workspace_session._run_command(args)
    captured = capsys.readouterr().out
    session = _FakeSession.instances[-1]

    assert exit_code == 0
    assert recorded_run["command"] == ["powershell.exe", "-NoProfile", "-Command", "Write-Output ok"]
    assert recorded_run["kwargs"]["cwd"] == str(tmp_path)
    assert recorded_run["kwargs"]["timeout"] == 30
    assert session.kwargs["auto_improve"] is False
    assert session.kwargs["task_brief"]["confirmed"] is True
    assert session.closed_result.outcome == "success"
    assert session.closed_result.confidence_score == 0.93
    assert session.closed_result.validation_summary == f"Command exit_code=0; workdir={tmp_path}"
    assert session.closed_result.artifacts[0]["path"] == str(artifact.resolve())
    assert session.closed_result.artifacts[0]["exists"] is True
    assert session.closed_result.playbook_data["commands"] == ["Write-Output ok"]
    assert session.closed_result.playbook_data["files_touched"] == [str(artifact.resolve())]
    assert "mcum_log_id=log-123" in captured
    assert "mcum_session_id=session-123" in captured
    assert "exit_code=0" in captured
    assert "stdout_tail=wrapper ok" in captured


def test_run_command_aborts_active_session_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        command="Start-Sleep -Seconds 99",
        workdir=None,
        timeout=5,
        summary=None,
        confidence_success=0.9,
        confidence_failure=0.2,
    )
    aborted: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(
        workspace_session,
        "_abort_active_session",
        lambda session, error_description, output_summary, validation_summary=None: aborted.update(
            {
                "session": session,
                "error_description": error_description,
                "output_summary": output_summary,
                "validation_summary": validation_summary,
            }
        )
        or "log-abort",
    )

    def fake_run(command, **kwargs):
        exc = subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial", stderr="boom")
        exc.stdout = "partial"
        exc.stderr = "boom"
        raise exc

    monkeypatch.setattr(workspace_session.subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        workspace_session._run_command(args)

    assert isinstance(aborted["session"], _FakeSession)
    assert aborted["error_description"] == "Command timed out after 5 second(s)."
    assert "stdout_tail: partial" in aborted["output_summary"]
    assert "stderr_tail: boom" in aborted["output_summary"]
    assert aborted["validation_summary"] == f"Command timeout; workdir={tmp_path}"


def test_record_only_closes_manual_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    args = _base_args(
        tmp_path,
        artifact=[artifact.name],
        save_experience=True,
        experience_title="Wrapper smoke validation",
        conclusion="Manual record completed",
        context="workspace_session record",
        summary="Manual validation stored in MCUM.",
        outcome="partial",
        confidence=0.61,
        error_description="non-blocking warning",
        validation_summary="pytest smoke record",
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    exit_code = workspace_session._record_only(args)
    captured = capsys.readouterr().out
    session = _FakeSession.instances[-1]

    assert exit_code == 0
    assert session.kwargs["auto_improve"] is False
    assert session.closed_result.outcome == "partial"
    assert session.closed_result.confidence_score == 0.61
    assert session.closed_result.error_description == "non-blocking warning"
    assert session.closed_result.validation_summary == "pytest smoke record"
    assert session.closed_result.artifacts[0]["path"] == str(artifact.resolve())
    assert session.closed_result.playbook_data["files_touched"] == [str(artifact.resolve())]
    assert session.closed_result.experience_data == {
        "category": "implementation_recipe",
        "title": "Wrapper smoke validation",
        "content": {
            "conclusion": "Manual record completed",
            "context": "workspace_session record",
        },
        "applicability": {
            "when": f"Use for MCUM-managed tasks in {tmp_path}",
        },
        "not_applicable_cases": {
            "when_not": "The task did not run under an MCUM-managed session or was not validated.",
        },
    }
    assert "mcum_log_id=log-123" in captured
    assert "mcum_session_id=session-123" in captured


def test_record_only_supports_final_skill_override_and_delegation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        summary="Delegated through a specialist worker.",
        outcome="success",
        confidence=0.88,
        final_skill="html-dashboard-expert",
        delegated_skill=["ui-ux-pro-max"],
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    exit_code = workspace_session._record_only(args)
    session = _FakeSession.instances[-1]

    assert exit_code == 0
    assert session.closed_result.skill_used == "html-dashboard-expert"
    assert session.closed_result.skills_orchestrated == ["ui-ux-pro-max"]
    assert session.closed_result.correction_source == "workspace_session_final_skill_override"


def test_run_sisl_cycle_cli_logs_manual_improvement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_calls: list[dict] = []
    args = argparse.Namespace(
        project_path=str(tmp_path),
        project_name="MCUM",
        skill_name="html-dashboard-expert",
        skill_version=None,
        target_ckl=0.85,
        writeback_mode="candidate",
        dry_run=False,
        no_persist_eval=False,
        quiet=True,
    )

    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_current_skill_version", lambda skill_name: "2.5.0")
    monkeypatch.setattr(
        workspace_session,
        "run_sisl_cycle",
        lambda **kwargs: {
            "ckl_score": 0.88,
            "baseline_ckl_score": 0.79,
            "proposals_n": 2,
            "high_conf_n": 1,
            "applied": [{"type": "add_failure_warning", "applied": True}],
            "gate_result": {"accepted": True},
            "report_id": "report-1",
            "report_version": "2.5.1",
            "eval_record_id": "eval-1",
            "candidate_eval_record_id": "eval-2",
            "should_continue": False,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "log_entry",
        lambda **kwargs: log_calls.append(kwargs) or "log-1",
    )

    exit_code = workspace_session._run_sisl_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"skill_name": "html-dashboard-expert"' in output
    assert '"writeback_mode": "candidate"' in output
    assert log_calls[0]["title"] == "Manual SISL cycle: html-dashboard-expert"
    assert log_calls[0]["log_metadata"]["gate_result"] == {"accepted": True}
