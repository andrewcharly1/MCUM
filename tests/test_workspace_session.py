from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from MCUM import workspace_session
from MCUM.core.worker_runner import resolve_worker_runner


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
        "skip_runtime_artifact": False,
        "worker_runner": "auto",
        "model_aware_workers": False,
        "no_model_aware_workers": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_resolve_task_brief_attaches_and_persists_spec_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(
        tmp_path,
        task="Crear un flujo de login",
        task_type="crear",
        objective="Crear login con validaciones",
        expected_deliverable="Spec persistida y brief enriquecido",
        success_criteria="Brief contiene spec_contract",
        validation_required="Contrato guardado en PostgreSQL",
    )
    persisted: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_session,
        "load_execution_policy",
        lambda: {
            "spec_contract": {
                "enabled": True,
                "persist": True,
                "always_attach": True,
                "block_on_persist_failure": True,
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )

    def fake_upsert(**kwargs):
        persisted.update(kwargs)
        return {"id": "spec-1"}

    monkeypatch.setattr(workspace_session, "upsert_spec_contract", fake_upsert)

    brief = workspace_session._resolve_task_brief(args)

    assert brief["spec_contract"]["id"] == "spec-1"
    assert brief["spec_contract"]["mode"] == "full"
    assert brief["spec_contract"]["project_id"] == "project-1"
    assert "Spec Contract:" in " ".join(brief["constraints"])
    assert persisted["task_id"] == brief["spec_contract"]["task_id"]
    assert persisted["contract"]["mode"] == "full"


def test_opportunistic_daily_guard_schedules_when_missing_today(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(tmp_path, skip_daily_guard=False)
    popen_calls: list[dict[str, object]] = []
    maintenance_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_session,
        "load_execution_policy",
        lambda: {
            "opportunistic_daily_guard": {
                "enabled": True,
                "maintenance_name": "daily_guard",
                "modes": ["record"],
                "allow_during_tests": True,
                "record_queued_run": True,
                "log_dir_name": "daily_guard_test",
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "queued-1",
    )

    class _FakePopen:
        def __init__(self, command, **kwargs) -> None:
            popen_calls.append({"command": command, "kwargs": kwargs})

    monkeypatch.setattr(workspace_session.subprocess, "Popen", _FakePopen)

    result = workspace_session._maybe_launch_opportunistic_daily_guard(
        args,
        {"project_path": str(tmp_path), "task_id": "task-1"},
        mode="record",
        task_log_id="log-1",
        outcome="success",
    )

    assert result["status"] == "scheduled"
    assert result["queued_id"] == "queued-1"
    assert maintenance_calls[0]["status"] == "queued"
    assert maintenance_calls[0]["trigger_reason"] == "opportunistic_after_record"
    assert popen_calls
    command = popen_calls[0]["command"]
    assert "maintenance-cycle" in command
    assert "--force" in command
    assert "--skip-daily-guard" in command
    assert command[command.index("--queued-run-id") + 1] == "queued-1"


def test_opportunistic_daily_guard_child_is_fully_detached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Regression: the daily-guard child shared the parent's console/process
    # group and was torn down before recording its result, leaving queued runs
    # to be reaped as failures. The child must be spawned fully detached.
    import os
    import subprocess as _subprocess

    args = _base_args(tmp_path, skip_daily_guard=False)
    popen_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_session,
        "load_execution_policy",
        lambda: {
            "opportunistic_daily_guard": {
                "enabled": True,
                "maintenance_name": "daily_guard",
                "modes": ["record"],
                "allow_during_tests": True,
                "record_queued_run": True,
                "log_dir_name": "daily_guard_test",
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(workspace_session, "record_maintenance_run", lambda **kwargs: "queued-1")

    class _FakePopen:
        def __init__(self, command, **kwargs) -> None:
            popen_calls.append({"command": command, "kwargs": kwargs})

    monkeypatch.setattr(workspace_session.subprocess, "Popen", _FakePopen)

    workspace_session._maybe_launch_opportunistic_daily_guard(
        args,
        {"project_path": str(tmp_path), "task_id": "task-1"},
        mode="record",
        task_log_id="log-1",
        outcome="success",
    )

    assert popen_calls
    kwargs = popen_calls[0]["kwargs"]
    if os.name == "nt":
        flags = kwargs.get("creationflags", 0)
        assert flags & getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        assert flags & getattr(_subprocess, "DETACHED_PROCESS", 0)
    else:
        assert kwargs.get("start_new_session") is True


def test_maintenance_cycle_updates_existing_queued_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(tmp_path, queued_run_id="queued-1")
    updates: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_session,
        "update_maintenance_run",
        lambda **kwargs: updates.append(kwargs) or True,
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should update queued run, not insert")),
    )

    run_id = workspace_session._record_or_update_maintenance_run(
        args,
        project_id="project-1",
        maintenance_name="daily_guard",
        scope="project",
        status="success",
        trigger_reason="forced_run",
        finished_at=datetime.now(timezone.utc),
        metrics_snapshot={"ok": True},
        findings={"done": True},
        actions_applied=[],
        tokens_estimated=10,
        notes="done",
    )

    assert run_id == "queued-1"
    assert updates[0]["maintenance_run_id"] == "queued-1"
    assert updates[0]["status"] == "success"


def test_model_aware_workers_are_policy_default() -> None:
    runner = resolve_worker_runner(
        requested_runner="auto",
        model_aware_workers=False,
        no_model_aware_workers=False,
        execution_policy={"worker_runner": {"model_aware_workers_default": True}},
    )

    assert runner == "minimax_sdk"


def test_opportunistic_daily_guard_skips_when_latest_run_is_today(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(tmp_path, skip_daily_guard=False)
    monkeypatch.setattr(
        workspace_session,
        "load_execution_policy",
        lambda: {
            "opportunistic_daily_guard": {
                "enabled": True,
                "maintenance_name": "daily_guard",
                "modes": ["record"],
                "allow_during_tests": True,
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {"id": "run-1", "status": "success", "finished_at": datetime.now(timezone.utc)},
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not queue daily guard twice")),
    )

    result = workspace_session._maybe_launch_opportunistic_daily_guard(
        args,
        {"project_path": str(tmp_path), "task_id": "task-1"},
        mode="record",
        task_log_id="log-1",
        outcome="success",
    )

    assert result["status"] == "already_ran_today"


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


def test_run_command_blocks_local_playwright_script_when_preflight_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    (tmp_path / "render_previews.js").write_text(
        "const { chromium } = require('playwright');\n",
        encoding="utf-8",
    )
    args = _base_args(
        tmp_path,
        command="node render_previews.js",
        workdir=None,
        timeout=30,
        summary=None,
        confidence_success=0.93,
        confidence_failure=0.25,
    )
    run_called = {"value": False}
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(
        workspace_session,
        "preflight_playwright_environment",
        lambda workdir: {
            "status": "needs_browser_install",
            "missing": ["local_playwright_package", "playwright_browser"],
            "recommendations": ["npm install -D playwright", "npx playwright install chromium"],
        },
    )

    def fake_run(command, **kwargs):
        run_called["value"] = True
        return SimpleNamespace(returncode=0, stdout="should not run", stderr="")

    monkeypatch.setattr(workspace_session.subprocess, "run", fake_run)

    exit_code = workspace_session._run_command(args)
    session = _FakeSession.instances[-1]

    assert exit_code == 1
    assert run_called["value"] is False
    assert session.closed_result.outcome == "failure"
    assert "Playwright preflight blocked command" in session.closed_result.validation_summary
    assert session.closed_result.extra_metadata["preflight_blocked"] is True
    assert session.closed_result.extra_metadata["playwright_preflight"]["missing"] == [
        "local_playwright_package",
        "playwright_browser",
    ]


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


def test_record_only_defaults_auto_execution_profile_to_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        execution_profile="auto",
        summary="Manual validation stored in MCUM.",
        outcome="partial",
        confidence=0.61,
    )
    captured: dict[str, str] = {}

    def fake_resolve(current_args, command=None):
        captured["execution_profile"] = current_args.execution_profile
        return {
            "project_path": current_args.project_path,
            "confirmed": True,
            "execution_profile": current_args.execution_profile,
        }

    monkeypatch.setattr(workspace_session, "_resolve_task_brief", fake_resolve)
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    assert workspace_session._record_only(args) == 0
    assert captured["execution_profile"] == "fast"
    assert _FakeSession.instances[-1].kwargs["task_brief"]["execution_profile"] == "fast"


def test_record_only_preserves_explicit_full_execution_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        execution_profile="full",
        summary="Manual validation stored in MCUM.",
        outcome="partial",
        confidence=0.61,
    )
    captured: dict[str, str] = {}

    def fake_resolve(current_args, command=None):
        captured["execution_profile"] = current_args.execution_profile
        return {"project_path": current_args.project_path, "confirmed": True}

    monkeypatch.setattr(workspace_session, "_resolve_task_brief", fake_resolve)
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    assert workspace_session._record_only(args) == 0
    assert captured["execution_profile"] == "full"


def test_intake_only_rejects_non_interactive_input_before_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(tmp_path, task=None, project_path=None)
    monkeypatch.setattr(workspace_session.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        workspace_session,
        "_prompt_text",
        lambda *args, **kwargs: pytest.fail("prompt must not run without a TTY"),
    )

    with pytest.raises(RuntimeError, match="requires a TTY"):
        workspace_session._intake_only(args)


def test_prompt_text_translates_eof_to_clear_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    with pytest.raises(RuntimeError, match="Interactive intake input is unavailable"):
        workspace_session._prompt_text("Task", required=True)


def test_fast_execution_profile_disables_graph_work() -> None:
    policy = workspace_session.apply_execution_profile(
        workspace_session.load_execution_policy(),
        {"task_type": "validar", "risk_level": "bajo"},
        requested="fast",
    )

    assert policy["code_graph"]["enabled"] is False
    assert policy["code_graph"]["auto_sync"] is False
    assert policy["graph_intelligence"]["enabled"] is False


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


def test_record_only_rejects_invalid_experience_category_before_session_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        save_experience=True,
        experience_category="architecture_upgrade",
        summary="Should fail before opening a session.",
        outcome="success",
        confidence=0.9,
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    with pytest.raises(ValueError, match="Invalid experience category"):
        workspace_session._record_only(args)

    assert _FakeSession.instances == []


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


def test_run_command_auto_generates_runtime_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        artifact=[],
        command="Write-Output ok",
        workdir=None,
        timeout=30,
        summary=None,
        confidence_success=0.93,
        confidence_failure=0.2,
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(
        workspace_session.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="wrapper ok", stderr=""),
    )

    exit_code = workspace_session._run_command(args)
    session = _FakeSession.instances[-1]

    assert exit_code == 0
    assert session.closed_result.outcome == "success"
    runtime_artifacts = [
        artifact["path"]
        for artifact in session.closed_result.artifacts
        if "MCUM_RESULT_" in artifact["path"]
    ]
    assert len(runtime_artifacts) == 1
    assert Path(runtime_artifacts[0]).exists()


def test_run_command_attaches_multi_agent_plan_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        command="Write-Output ok",
        workdir=None,
        timeout=30,
        summary=None,
        confidence_success=0.93,
        confidence_failure=0.2,
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        max_workers=3,
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "corregir",
            "objective": "Fix a complex issue safely.",
            "expected_deliverable": "Validated fix.",
            "success_criteria": "The issue is fixed and validated.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "supervised_multi_agent": True,
            "task_id": "task-123",
            "editable_scope": current_args.project_path,
            "read_only_scope": current_args.project_path,
        },
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(
        workspace_session.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="wrapper ok", stderr=""),
    )

    exit_code = workspace_session._run_command(args)
    session = _FakeSession.instances[-1]

    assert exit_code == 0
    plan = session.closed_result.extra_metadata["multi_agent_plan"]
    assert plan["mode"] == "supervised"
    assert len(plan["workers"]) >= 2
    assert any(worker["mode"] == "write" for worker in plan["workers"])


def test_run_command_auto_promotes_to_multi_run_when_complex_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(
        tmp_path,
        command="Write-Output implementer",
        auto_multi_run=True,
        worker_command=["validator=Write-Output validator"],
        workdir=None,
        timeout=30,
    )
    recorded: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "corregir",
            "objective": "Fix a complex issue safely.",
            "expected_deliverable": "Validated fix.",
            "success_criteria": "Fix lands with validator confirmation.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "task_id": "task-auto-run",
            "editable_scope": current_args.project_path,
            "read_only_scope": current_args.project_path,
        },
    )

    def fake_multi_run(current_args, **kwargs):
        recorded["args"] = current_args
        recorded["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(workspace_session, "_run_multi_execution", fake_multi_run)

    exit_code = workspace_session._run_command(args)

    assert exit_code == 0
    assert recorded["kwargs"]["auto_promoted_from"] == "run"
    commands = recorded["kwargs"]["precomputed_worker_commands"]
    assert commands["implementer"] == "Write-Output implementer"
    assert commands["validator"] == "Write-Output validator"


def test_run_command_auto_promotes_to_multi_run_when_anti_loop_requires_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _base_args(
        tmp_path,
        command="Write-Output implementer",
        worker_command=["validator=Write-Output validator"],
        workdir=None,
        timeout=30,
    )
    recorded: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "corregir",
            "objective": "Retry safely after repeated failures.",
            "expected_deliverable": "Validated alternate path.",
            "success_criteria": "A different strategy is validated.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "task_id": "task-anti-loop-auto-run",
            "editable_scope": current_args.project_path,
            "read_only_scope": current_args.project_path,
        },
    )

    def fake_preflight(current_args, task_brief):
        task_brief["supervised_multi_agent"] = True
        task_brief["anti_loop_force_multi_run"] = True
        task_brief["preferred_write_skill_hint"] = "validator-specialist"
        task_brief["anti_loop_preflight"] = {
            "enabled": True,
            "loop_risk": 0.82,
            "recommendation": "switch_strategy_before_retry",
        }
        return task_brief["anti_loop_preflight"]

    def fake_multi_run(current_args, **kwargs):
        recorded["args"] = current_args
        recorded["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(workspace_session, "_apply_run_anti_loop_preflight", fake_preflight)
    monkeypatch.setattr(workspace_session, "_run_multi_execution", fake_multi_run)

    exit_code = workspace_session._run_command(args)

    assert exit_code == 0
    assert recorded["kwargs"]["auto_promoted_from"] == "run"
    plan = recorded["kwargs"]["precomputed_plan"]
    assert plan["anti_loop_recommended"] is True
    assert plan["preferred_write_skill_hint"] == "validator-specialist"
    commands = recorded["kwargs"]["precomputed_worker_commands"]
    assert commands["implementer"] == "Write-Output implementer"
    assert commands["validator"] == "Write-Output validator"


def test_multi_plan_generates_supervised_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _base_args(
        tmp_path,
        task="Investigate and fix a cross-cutting workspace issue.",
        task_type="corregir",
        objective="Generate a supervised multi-agent plan.",
        expected_deliverable="Plan with workers and merge policy.",
        success_criteria="The plan defines coordinator, workers, budgets, and guardrails.",
        execution_mode="ejecutar",
        risk_level="alto",
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        force_skill="mcum-orchestrator",
        max_workers=3,
    )

    exit_code = workspace_session._run_multi_plan(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"mode": "supervised"' in output
    assert '"workers"' in output


def test_build_multi_run_phases_groups_contiguous_read_only_and_write() -> None:
    workers = [
        {"role": "context_analyst", "mode": "read_only"},
        {"role": "implementer", "mode": "write"},
        {"role": "validator", "mode": "read_only"},
    ]
    worker_lookup = {
        "context_analyst": (workers[0], {"worker_role": "context_analyst"}),
        "implementer": (workers[1], {"worker_role": "implementer"}),
        "validator": (workers[2], {"worker_role": "validator"}),
    }
    phases, skipped = workspace_session._build_multi_run_phases(
        workers,
        worker_lookup,
        {
            "context_analyst": "Write-Output a",
            "implementer": "Write-Output b",
            "validator": "Write-Output c",
        },
    )

    assert skipped == []
    assert [phase["kind"] for phase in phases] == ["read_only", "write", "read_only"]
    assert [entry["role"] for entry in phases[0]["entries"]] == ["context_analyst"]
    assert [entry["role"] for entry in phases[1]["entries"]] == ["implementer"]
    assert [entry["role"] for entry in phases[2]["entries"]] == ["validator"]


def test_multi_run_parallelizes_read_only_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task="Analyze a broad issue with two read-only workers",
        task_type="analizar",
        objective="Collect and validate findings.",
        expected_deliverable="Coordinated analysis.",
        success_criteria="Both read-only workers complete under supervision.",
        execution_mode="analizar",
        risk_level="medio",
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        max_workers=2,
        workdir=None,
        timeout=30,
        worker_command=[
            "context_analyst=Write-Output analyst",
            "validator=Write-Output validator",
        ],
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "analizar",
            "objective": current_args.objective,
            "expected_deliverable": current_args.expected_deliverable,
            "success_criteria": current_args.success_criteria,
            "execution_mode": current_args.execution_mode,
            "risk_level": current_args.risk_level,
            "validation_required": "Findings and cross-check complete.",
            "confirmed": True,
            "supervised_multi_agent": True,
            "task_id": "task-parallel",
            "read_only_scope": current_args.project_path,
        },
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    def fake_worker(*args, **kwargs):
        role = kwargs["worker"]["role"]
        time.sleep(0.35)
        return {
            "role": role,
            "mode": kwargs["worker"]["mode"],
            "status": "success",
            "command": kwargs["command"],
            "task_id": f"task-{role}",
            "mcum_log_id": f"log-{role}",
            "mcum_session_id": f"session-{role}",
            "exit_code": 0,
            "summary": f"{role} ok",
            "validation_summary": f"{role} validated",
            "artifacts": [],
        }

    monkeypatch.setattr(workspace_session, "_execute_supervised_worker", fake_worker)

    started = time.perf_counter()
    exit_code = workspace_session._run_multi_execution(args)
    elapsed = time.perf_counter() - started
    coordinator = _FakeSession.instances[0]

    assert exit_code == 0
    assert elapsed < 0.6
    runs = coordinator.closed_result.extra_metadata["worker_runs"]
    phase_reports = coordinator.closed_result.extra_metadata["phase_reports"]
    merge_summary = coordinator.closed_result.extra_metadata["merge_summary"]
    assert len(runs) == 2
    assert {run["phase_kind"] for run in runs} == {"read_only"}
    assert phase_reports[0]["parallelized"] is True
    assert phase_reports[0]["phase_kind"] == "read_only"
    assert merge_summary["compact_context_tokens_estimate"] > 0
    assert len(merge_summary["highlights"]) == 2
    assert coordinator.closed_result.experience_data["category"] == "testing_strategy"
    assert coordinator.closed_result.playbook_data["output_summary"] == merge_summary["compact_context"]


def test_multi_run_executes_supervised_workers_and_closes_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task="Fix a cross-cutting orchestrator issue safely",
        task_type="corregir",
        objective="Coordinate analysis, implementation, and validation.",
        expected_deliverable="Validated supervised execution.",
        success_criteria="Workers run under guardrails and validator confirms the write path.",
        execution_mode="ejecutar",
        risk_level="alto",
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        max_workers=3,
        workdir=None,
        timeout=30,
        worker_command=[
            "context_analyst=Write-Output analyst",
            "implementer=Write-Output implementer",
            "validator=Write-Output validator",
        ],
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "corregir",
            "objective": current_args.objective,
            "expected_deliverable": current_args.expected_deliverable,
            "success_criteria": current_args.success_criteria,
            "execution_mode": current_args.execution_mode,
            "risk_level": current_args.risk_level,
            "validation_required": "All worker steps must validate cleanly.",
            "confirmed": True,
            "supervised_multi_agent": True,
            "task_id": "task-456",
            "editable_scope": current_args.project_path,
            "read_only_scope": current_args.project_path,
        },
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(
        workspace_session.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"ok:{command[-1]}",
            stderr="",
        ),
    )

    exit_code = workspace_session._run_multi_execution(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert len(_FakeSession.instances) == 4
    coordinator = _FakeSession.instances[0]
    analyst = _FakeSession.instances[1]
    implementer = _FakeSession.instances[2]
    validator = _FakeSession.instances[3]

    assert coordinator.closed_result.outcome == "success"
    assert coordinator.closed_result.extra_metadata["multi_agent_plan"]["mode"] == "supervised"
    assert len(coordinator.closed_result.extra_metadata["worker_runs"]) == 3
    assert coordinator.closed_result.extra_metadata["merge_summary"]["successful_roles"] == [
        "context_analyst",
        "implementer",
        "validator",
    ]
    assert coordinator.closed_result.experience_data["category"] == "implementation_recipe"
    assert coordinator.closed_result.playbook_data["output_summary"] == coordinator.closed_result.extra_metadata["merge_summary"]["compact_context"]
    assert analyst.kwargs["task_brief"]["orchestration_role"] == "worker"
    assert analyst.kwargs["task_brief"]["suppress_autonomy_hooks"] is True
    assert analyst.kwargs["task_brief"]["allow_worker_learning_writes"] is False
    assert analyst.kwargs["task_brief"]["parent_session_id"] == "session-123"
    assert implementer.kwargs["task_brief"]["editable_scope"] == str(tmp_path)
    assert validator.kwargs["task_brief"]["worker_role"] == "validator"
    assert '"status": "success"' in output
    assert "mcum_log_id=log-123" in output


def test_model_aware_worker_uses_codex_exec_recommended_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task="Collect compact context with a low-cost worker",
        workdir=None,
        timeout=30,
        model_aware_workers=True,
        worker_runner="codex-exec",
    )
    worker = {
        "role": "context_analyst",
        "mode": "read_only",
        "agent_profile": "context-retriever",
        "recommended_model": "gpt-5.4-mini",
        "model_route": {
            "agent_profile": "context-retriever",
            "recommended_model": "gpt-5.4-mini",
            "reasoning_effort": "low",
            "token_budget": {"context_in": 900, "output": 260, "total": 1160},
        },
    }
    worker_brief = {
        "project_path": str(tmp_path),
        "worker_role": "context_analyst",
        "objective": "Inspect context cheaply.",
        "expected_deliverable": "Compact evidence.",
        "success_criteria": "Evidence is concise.",
        "validation_required": "Worker returns a concise summary.",
        "read_only_scope": str(tmp_path),
    }
    recorded_run: dict[str, object] = {}
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    def fake_run(command, **kwargs):
        recorded_run["command"] = command
        recorded_run["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="codex worker ok", stderr="")

    monkeypatch.setattr(workspace_session.subprocess, "run", fake_run)

    result = workspace_session._execute_supervised_worker(
        args,
        coordinator_session_id="coordinator-session",
        coordinator_task_id="task-cost",
        coordinator_skill="mcum-orchestrator",
        worker=worker,
        worker_brief=worker_brief,
        command="Analiza los archivos relevantes y devuelve solo hallazgos compactos.",
    )

    command = recorded_run["command"]
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-5.4-mini"
    assert 'model_reasoning_effort="low"' in command
    assert recorded_run["kwargs"]["input"].startswith("Eres un worker supervisado por MCUM")
    assert "Mantente conciso para ahorrar tokens" in recorded_run["kwargs"]["input"]
    assert result["status"] == "success"
    assert result["runner"] == "codex_exec"
    assert result["runner_metadata"]["model_aware"] is True
    assert result["runner_metadata"]["recommended_model"] == "gpt-5.4-mini"
    worker_session = _FakeSession.instances[-1]
    assert worker_session.closed_result.extra_metadata["runner"] == "codex_exec"


def test_minimax_worker_runner_records_usage_from_json_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task="Delegate compact analysis to MiniMax",
        workdir=None,
        timeout=30,
        worker_runner="minimax-sdk",
        entrypoint_agent="codex",
    )
    worker = {
        "role": "context_analyst",
        "mode": "read_only",
        "agent_profile": "context-retriever",
        "model_route": {
            "agent_profile": "context-retriever",
            "recommended_model": "gpt-5.4-mini",
            "token_budget": {"context_in": 900, "output": 260, "total": 1160},
        },
    }
    worker_brief = {
        "project_path": str(tmp_path),
        "worker_role": "context_analyst",
        "objective": "Inspect context cheaply.",
        "expected_deliverable": "Compact evidence.",
        "success_criteria": "Evidence is concise.",
        "validation_required": "Worker returns a concise summary.",
        "read_only_scope": str(tmp_path),
    }
    recorded_run: dict[str, object] = {}
    stdout_payload = {
        "status": "success",
        "summary": "MiniMax returned compact evidence.",
        "provider": "minimax",
        "protocol": "openai",
        "model": "MiniMax-M3",
        "source": "test_env",
        "available": True,
        "usage": {"input_tokens": 222, "output_tokens": 111, "total_tokens": 333},
    }
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    def fake_run(command, **kwargs):
        recorded_run["command"] = command
        recorded_run["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(stdout_payload), stderr="")

    monkeypatch.setattr(workspace_session.subprocess, "run", fake_run)

    result = workspace_session._execute_supervised_worker(
        args,
        coordinator_session_id="coordinator-session",
        coordinator_task_id="task-cost",
        coordinator_skill="mcum-orchestrator",
        worker=worker,
        worker_brief=worker_brief,
        command="Analiza los archivos relevantes y devuelve solo hallazgos compactos.",
    )

    command = recorded_run["command"]
    assert command[1].endswith("minimax_worker.py")
    payload = json.loads(recorded_run["kwargs"]["input"])
    assert payload["runner"] == "minimax_sdk"
    assert payload["model"] == "MiniMax-M3"
    assert payload["worker_brief"]["entrypoint_agent"] == "codex"
    assert result["runner"] == "minimax_sdk"
    assert result["runner_metadata"]["usage"] == {"input_tokens": 222, "output_tokens": 111, "total_tokens": 333}
    assert result["runner_metadata"]["recommended_model"] == "MiniMax-M3"
    worker_session = _FakeSession.instances[-1]
    assert worker_session.closed_result.context_tokens_out == 111
    assert worker_session.closed_result.extra_metadata["runner_payload_status"]["summary"] == "MiniMax returned compact evidence."


def test_unknown_worker_request_falls_back_to_powershell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task="Validate unsupported worker fallback.",
        workdir=None,
        timeout=30,
        worker_runner="legacy-cli",
    )
    worker = {
        "role": "validator",
        "mode": "read_only",
        "agent_profile": "validator-qa",
        "recommended_model": "gpt-5.3-codex",
        "model_route": {"recommended_model": "gpt-5.3-codex"},
    }
    worker_brief = {
        "project_path": str(tmp_path),
        "worker_role": "validator",
        "objective": "Verify unsupported runners do not execute as workers.",
        "expected_deliverable": "Fallback result.",
        "success_criteria": "The result exposes a safe runner.",
        "validation_required": "Fallback is recorded.",
        "read_only_scope": str(tmp_path),
    }
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)

    result = workspace_session._execute_supervised_worker(
        args,
        coordinator_session_id="coordinator-session",
        coordinator_task_id="task-timeout",
        coordinator_skill="mcum-orchestrator",
        worker=worker,
        worker_brief=worker_brief,
        command="Write-Output 'fallback ok'",
    )

    assert result["status"] == "success"
    assert result["lifecycle_status"] == "completed"
    assert result["runner"] == "powershell"
    assert result["runner_metadata"]["model_aware"] is False


def test_multi_run_requires_validator_when_write_worker_is_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        task_type="corregir",
        execution_mode="ejecutar",
        risk_level="alto",
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        max_workers=3,
        workdir=None,
        timeout=30,
        worker_command=["implementer=Write-Output implementer"],
    )

    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {
            "project_path": current_args.project_path,
            "task_type": "corregir",
            "objective": "Fix safely.",
            "expected_deliverable": "Validated fix.",
            "success_criteria": "No regressions.",
            "execution_mode": "ejecutar",
            "risk_level": "alto",
            "confirmed": True,
            "supervised_multi_agent": True,
            "task_id": "task-789",
            "editable_scope": current_args.project_path,
            "read_only_scope": current_args.project_path,
        },
    )
    monkeypatch.setattr(workspace_session, "OrchestratorSession", _FakeSession)
    monkeypatch.setattr(workspace_session, "_abort_active_session", lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match="validator worker command"):
        workspace_session._run_multi_execution(args)


def test_maintenance_cycle_skips_when_no_delta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=None,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    recorded: dict[str, object] = {}

    monkeypatch.setattr(workspace_session, "load_maintenance_policy", lambda: {"maintenance_name": "daily_guard"})
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": False,
            "reasons": [],
            "recommended_actions": [],
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: recorded.update(kwargs) or "maintenance-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert _FakeSession.instances == []
    assert recorded["status"] == "skipped"
    assert "maintenance-1" in output
    assert '"status": "skipped"' in output


def test_maintenance_cycle_runs_safe_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "catalog_targets": {"max_candidate_active_ratio": 3.0},
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
                "adaptive_actions": ["run_skill_factory"],
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
                "min_active_tests": 8,
                "min_successful_uses": 2,
                "min_success_rate": 0.75,
                "min_lifecycle_score": 0.78,
                "min_testing_uses": 2,
                "activation_score": 0.82,
                "rollback_score": 0.55,
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "findings": {"delta": {"reasons": ["metrics_stale"]}},
            "metrics_snapshot": {"reasons": ["metrics_stale"]},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["new_logs", "metrics_stale"],
            "recommended_actions": [
                "refresh_daily_metrics",
                "snapshot_project_kpis",
                "run_skill_factory",
            ],
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "refresh_daily_metrics",
        lambda project_id=None: {"rows_refreshed": 12, "latest_day": "2026-03-27"},
    )
    monkeypatch.setattr(
        workspace_session,
        "snapshot_project_kpis",
        lambda **kwargs: [{"project_id": "project-1", "snapshot_date": "2026-03-27"}],
    )
    monkeypatch.setattr(
        workspace_session,
        "run_skill_factory_cycle",
        lambda **kwargs: {
            "signals": [{"signal": "gap"}],
            "created": [{"skill_name": "candidate-skill"}],
            "promoted": [],
            "pending": [],
            "pending_total": 0,
            "pending_truncated": 0,
            "applied_hints": [{"skill_name": "candidate-skill"}],
            "catalog_pressure": {"auto_bootstrap_applied": True},
            "testing_reviews": {"activated": [], "rolled_back": [], "monitoring_total": 0, "monitoring_truncated": 0},
        },
    )
    maintenance_task_calls: list[dict] = []
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-123", "session-123"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-2",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert _FakeSession.instances == []
    assert maintenance_task_calls[0]["confidence_score"] == 0.92
    assert maintenance_calls[-1]["status"] == "success"
    assert len(maintenance_calls[-1]["actions_applied"]) == 3
    runtime_artifacts = [
        artifact["path"]
        for artifact in maintenance_task_calls[0]["artifacts"]
        if "MCUM_RESULT_" in artifact["path"]
    ]
    assert len(runtime_artifacts) == 1
    assert Path(runtime_artifacts[0]).exists()
    assert "maintenance-2" in output
    assert '"status": "success"' in output


def test_maintenance_cycle_tunes_anti_loop_dispatch_bias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []
    update_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "anti_loop_dispatch_tuning": {
                "enabled": True,
                "lookback_days": 21,
            },
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics"],
                "adaptive_actions": ["tune_anti_loop_dispatch_bias"],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "findings": {"delta": {"reasons": ["anti_loop_dispatch_tuning_needed"]}},
            "metrics_snapshot": {"reasons": ["anti_loop_dispatch_tuning_needed"]},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["anti_loop_dispatch_tuning_needed"],
            "recommended_actions": ["tune_anti_loop_dispatch_bias"],
            "last_activity_at": None,
            "anti_loop_dispatch_audit": {
                "recommended_action": "tune_anti_loop_dispatch_bias",
                "recommendation": "increase_bias",
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "analyze_anti_loop_dispatch_effectiveness",
        lambda **kwargs: {
            "project_id": "project-1",
            "recommended_action": "tune_anti_loop_dispatch_bias",
            "recommendation": "increase_bias",
            "current_score_boost": 0.08,
            "current_priority_boost": 0.5,
            "suggested_score_boost": 0.09,
            "suggested_priority_boost": 0.6,
            "metrics": {"hinted_tasks": 9},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "update_execution_policy",
        lambda patch: update_calls.append(patch) or {
            "anti_loop": {
                "dispatch_preference_score_boost": 0.09,
                "dispatch_preference_priority_boost": 0.6,
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-tune-1", "session-tune-1"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-tune-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert update_calls == [
        {
            "anti_loop": {
                "dispatch_preference_score_boost": 0.09,
                "dispatch_preference_priority_boost": 0.6,
            }
        }
    ]
    assert maintenance_calls[-1]["status"] == "success"
    assert maintenance_calls[-1]["actions_applied"][0]["action"] == "tune_anti_loop_dispatch_bias"
    assert maintenance_calls[-1]["findings"]["action_results"][0]["result"]["policy_updated"] is True
    assert maintenance_task_calls[0]["outcome"] == "success"
    assert "maintenance-tune-1" in output
    assert '"status": "success"' in output


def test_maintenance_cycle_tunes_memory_governor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []
    update_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "memory_targets": {},
            "memory_governor_tuning": {
                "enabled": True,
                "lookback_days": 21,
            },
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics"],
                "adaptive_actions": ["tune_memory_governor"],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "findings": {"delta": {"reasons": ["memory_governor_tuning_needed"]}},
            "metrics_snapshot": {"reasons": ["memory_governor_tuning_needed"]},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["memory_governor_tuning_needed"],
            "recommended_actions": ["tune_memory_governor"],
            "last_activity_at": None,
            "operational_summary": {"success_rate": 0.86, "token_efficiency_per_1k": 0.88, "anti_loop_hinted_rate": 0.4},
            "memory_governor_audit": {
                "recommended_action": "tune_memory_governor",
                "recommendation": "tighten",
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "analyze_memory_governor_effectiveness",
        lambda **kwargs: {
            "project_id": "project-1",
            "recommended_action": "tune_memory_governor",
            "recommendation": "tighten",
            "current_assist_penalty_weight": 0.12,
            "current_cross_project_risk": 0.06,
            "current_verbosity_soft_cap": 720,
            "suggested_assist_penalty_weight": 0.13,
            "suggested_cross_project_risk": 0.07,
            "suggested_verbosity_soft_cap": 680,
            "metrics": {"contamination_score": 0.49},
            "stability_guard": {"reason": None},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "update_execution_policy",
        lambda patch: update_calls.append(patch) or {
            "memory_governor": {
                "assist_penalty_weight": 0.13,
                "cross_project_risk": 0.07,
                "verbosity_soft_cap": 680,
            }
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-memory-tune-1", "session-memory-tune-1"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-memory-tune-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert update_calls == [
        {
            "memory_governor": {
                "assist_penalty_weight": 0.13,
                "cross_project_risk": 0.07,
                "verbosity_soft_cap": 680,
            }
        }
    ]
    assert maintenance_calls[-1]["status"] == "success"
    assert maintenance_calls[-1]["actions_applied"][0]["action"] == "tune_memory_governor"
    assert maintenance_calls[-1]["findings"]["action_results"][0]["result"]["policy_updated"] is True
    assert "maintenance-memory-tune-1" in output
    assert '"status": "success"' in output


def test_maintenance_cycle_runs_memory_audit_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "memory_targets": {},
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["audit_memory_governance"],
                "adaptive_actions": [],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["memory_duplicates_high"],
            "recommended_actions": ["audit_memory_governance"],
            "last_activity_at": None,
            "memory_audit": {
                "severity": "medium",
                "contamination_score": 0.44,
                "reasons": ["memory_duplicates_high"],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "audit_memory_governance",
        lambda **kwargs: {
            "project_id": "project-1",
            "severity": "medium",
            "contamination_score": 0.44,
            "reasons": ["memory_duplicates_high"],
            "experience_metrics": {"duplicate_ratio": 0.31},
            "playbook_metrics": {"never_reused_ratio": 0.52},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-321", "session-321"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-memory-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert maintenance_calls[-1]["status"] == "success"
    assert len(maintenance_calls[-1]["actions_applied"]) == 1
    assert maintenance_calls[-1]["actions_applied"][0]["action"] == "audit_memory_governance"
    assert maintenance_calls[-1]["findings"]["action_results"][0]["result"]["contamination_score"] == 0.44
    assert "maintenance-memory-1" in output
    assert '"status": "success"' in output
    assert maintenance_task_calls[0]["outcome"] == "success"


def test_maintenance_cycle_runs_duplicate_consolidation_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "memory_targets": {},
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["consolidate_duplicate_experiences"],
                "adaptive_actions": [],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["memory_exact_duplicates_found"],
            "recommended_actions": ["consolidate_duplicate_experiences"],
            "last_activity_at": None,
            "memory_audit": {
                "severity": "medium",
                "contamination_score": 0.28,
                "reasons": ["memory_exact_duplicates_found"],
                "experience_metrics": {"exact_duplicate_experiences": 3},
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "consolidate_duplicate_experiences",
        lambda **kwargs: {
            "project_id": "project-1",
            "groups_considered": 1,
            "groups_merged": 1,
            "experiences_superseded": 3,
            "samples": [{"canonical_id": "canon-1", "duplicate_ids": ["dup-1", "dup-2", "dup-3"]}],
            "mode": "exact_match_only",
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-654", "session-654"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-dedupe-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert maintenance_calls[-1]["status"] == "success"
    assert len(maintenance_calls[-1]["actions_applied"]) == 1
    assert maintenance_calls[-1]["actions_applied"][0]["action"] == "consolidate_duplicate_experiences"
    assert maintenance_calls[-1]["findings"]["action_results"][0]["result"]["experiences_superseded"] == 3
    assert "maintenance-dedupe-1" in output
    assert '"status": "success"' in output
    assert maintenance_task_calls[0]["outcome"] == "success"


def test_maintenance_cycle_skips_during_cooldown_without_fresh_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=None,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "min_hours_between_runs": 20,
            "skip_if_no_new_logs": True,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "started_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "finished_at": datetime.now(timezone.utc) - timedelta(hours=1),
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": False,
            "reasons": ["candidate_pressure"],
            "recommended_actions": ["run_skill_factory"],
            "fresh_signal_present": False,
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-3",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert maintenance_calls[-1]["status"] == "skipped"
    assert maintenance_calls[-1]["trigger_reason"] == "cooldown_active"
    assert '"reason": "cooldown_active"' in output


def test_maintenance_cycle_force_executes_safe_actions_without_fresh_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=True,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "catalog_targets": {"max_candidate_active_ratio": 3.0},
            "self_heal": {
                "enabled": True,
                "force_executes_safe_actions": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
                "adaptive_actions": ["run_skill_factory"],
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
                "min_active_tests": 8,
                "min_successful_uses": 2,
                "min_success_rate": 0.75,
                "min_lifecycle_score": 0.78,
                "min_testing_uses": 2,
                "activation_score": 0.82,
                "rollback_score": 0.55,
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": False,
            "reasons": [],
            "recommended_actions": [],
            "fresh_signal_present": False,
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "refresh_daily_metrics",
        lambda project_id=None: {"rows_refreshed": 5, "latest_day": "2026-03-27"},
    )
    monkeypatch.setattr(
        workspace_session,
        "snapshot_project_kpis",
        lambda **kwargs: [{"project_id": "project-1", "snapshot_date": "2026-03-27"}],
    )
    monkeypatch.setattr(
        workspace_session,
        "run_skill_factory_cycle",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("run_skill_factory_cycle should not be called")),
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-123", "session-123"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-force-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert _FakeSession.instances == []
    assert maintenance_task_calls[0]["outcome"] == "success"
    assert maintenance_calls[-1]["status"] == "success"
    assert maintenance_calls[-1]["findings"]["force_requested"] is True
    assert maintenance_calls[-1]["findings"]["audit_summary"]["decision"] == "forced_proceed"
    assert len(maintenance_calls[-1]["actions_applied"]) == 2
    assert "force_requested" in output
    assert '"status": "success"' in output


def test_maintenance_cycle_force_reports_clear_noop_when_no_actions_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=True,
        skip_metrics_refresh=True,
        skip_kpi_snapshot=True,
        skip_skill_factory=True,
    )
    maintenance_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "catalog_targets": {"max_candidate_active_ratio": 3.0},
            "self_heal": {
                "enabled": True,
                "force_executes_safe_actions": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
                "adaptive_actions": ["run_skill_factory"],
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(workspace_session, "get_latest_maintenance_run", lambda **kwargs: None)
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": False,
            "reasons": [],
            "recommended_actions": [],
            "fresh_signal_present": False,
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("_log_maintenance_task should not be called")),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-force-2",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert maintenance_calls[-1]["status"] == "skipped"
    assert maintenance_calls[-1]["findings"]["no_action_reason"] == "forced_safe_actions_blocked_by_runtime_flags"
    assert maintenance_calls[-1]["findings"]["force_requested"] is True
    assert maintenance_calls[-1]["findings"]["audit_summary"]["decision"] == "forced_report_only"
    assert "maintenance-force-2" in output
    assert '"status": "skipped"' in output


def test_maintenance_self_heal_classification_requires_repeated_signal_for_adaptive_actions() -> None:
    policy = {
        "self_heal": {
            "enabled": True,
            "repeat_required_for_adaptive_actions": True,
            "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
            "adaptive_actions": ["run_skill_factory"],
        }
    }
    delta = {
        "recommended_actions": [
            "refresh_daily_metrics",
            "snapshot_project_kpis",
            "run_skill_factory",
        ],
        "reasons": ["candidate_pressure", "metrics_stale"],
    }

    first_seen = workspace_session._classify_maintenance_actions(delta, policy, latest_run=None)
    repeated = workspace_session._classify_maintenance_actions(
        delta,
        policy,
        latest_run={"findings": {"delta": {"reasons": ["candidate_pressure", "metrics_stale"]}}},
    )

    assert first_seen["safe_actions"] == ["refresh_daily_metrics", "snapshot_project_kpis"]
    assert first_seen["blocked_actions"] == ["run_skill_factory"]
    assert first_seen["repeat_patterns"] == []
    assert repeated["safe_actions"] == [
        "refresh_daily_metrics",
        "snapshot_project_kpis",
        "run_skill_factory",
    ]
    assert repeated["repeat_patterns"] == ["stale_metrics_repeat", "catalog_pressure_repeat"]


def test_per_run_cap_never_starves_base_safe_actions() -> None:
    # Regression: with 4 base-safe actions recommended and a cap of 3, the old
    # logic truncated the tail (consolidate_duplicate_experiences) into
    # blocked_actions every cycle, so duplicate consolidation never ran. The cap
    # must apply only to promoted adaptive actions; all base-safe actions run.
    policy = {
        "self_heal": {
            "enabled": True,
            "max_actions_per_run": 3,
            "safe_actions": [
                "refresh_daily_metrics",
                "snapshot_project_kpis",
                "audit_memory_governance",
                "consolidate_duplicate_experiences",
            ],
            "adaptive_actions": ["run_skill_factory"],
        }
    }
    delta = {
        "recommended_actions": [
            "refresh_daily_metrics",
            "snapshot_project_kpis",
            "audit_memory_governance",
            "consolidate_duplicate_experiences",
        ],
        "reasons": ["metrics_stale"],
    }

    result = workspace_session._classify_maintenance_actions(delta, policy, latest_run=None)

    assert result["safe_actions"] == [
        "refresh_daily_metrics",
        "snapshot_project_kpis",
        "audit_memory_governance",
        "consolidate_duplicate_experiences",
    ]
    assert "consolidate_duplicate_experiences" not in result["blocked_actions"]


def test_per_run_cap_still_bounds_promoted_adaptive_actions() -> None:
    # The cap continues to limit adaptive actions once their repeat gate opens.
    policy = {
        "self_heal": {
            "enabled": True,
            "max_actions_per_run": 1,
            "repeat_required_for_adaptive_actions": False,
            "safe_actions": ["refresh_daily_metrics"],
            "adaptive_actions": ["run_skill_factory", "tune_memory_governor"],
        }
    }
    delta = {
        "recommended_actions": [
            "refresh_daily_metrics",
            "run_skill_factory",
            "tune_memory_governor",
        ],
        "reasons": ["metrics_stale"],
    }

    result = workspace_session._classify_maintenance_actions(delta, policy, latest_run=None)

    # base-safe always runs; only the first adaptive fits the cap of 1.
    assert "refresh_daily_metrics" in result["safe_actions"]
    assert "run_skill_factory" in result["safe_actions"]
    assert "tune_memory_governor" in result["blocked_actions"]


def test_maintenance_cycle_records_partial_status_and_rollback_metadata_on_action_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeSession.reset()
    args = _base_args(
        tmp_path,
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
    )
    maintenance_calls: list[dict] = []
    maintenance_task_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "catalog_targets": {"max_candidate_active_ratio": 3.0},
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
                "adaptive_actions": ["run_skill_factory"],
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
                "min_active_tests": 8,
                "min_successful_uses": 2,
                "min_success_rate": 0.75,
                "min_lifecycle_score": 0.78,
                "min_testing_uses": 2,
                "activation_score": 0.82,
                "rollback_score": 0.55,
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "findings": {"delta": {"reasons": ["candidate_pressure"]}},
            "metrics_snapshot": {"reasons": ["candidate_pressure"]},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "reasons": ["candidate_pressure"],
            "recommended_actions": [
                "refresh_daily_metrics",
                "snapshot_project_kpis",
                "run_skill_factory",
            ],
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "refresh_daily_metrics",
        lambda project_id=None: {"rows_refreshed": 8, "latest_day": "2026-03-27"},
    )
    monkeypatch.setattr(
        workspace_session,
        "snapshot_project_kpis",
        lambda **kwargs: [{"project_id": "project-1", "snapshot_date": "2026-03-27"}],
    )
    monkeypatch.setattr(
        workspace_session,
        "run_skill_factory_cycle",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("skill factory failed")),
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: maintenance_task_calls.append(kwargs) or ("log-123", "session-123"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: maintenance_calls.append(kwargs) or "maintenance-4",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert _FakeSession.instances == []
    assert maintenance_task_calls[0]["outcome"] == "partial"
    assert maintenance_task_calls[0]["confidence_score"] == 0.78
    assert maintenance_calls[-1]["status"] == "partial"
    assert maintenance_calls[-1]["findings"]["maintenance_status"] == "partial"
    assert maintenance_calls[-1]["findings"]["audit_summary"]["decision"] == "proceed"
    assert maintenance_calls[-1]["findings"]["audit_summary"]["rolled_back_actions"] == ["run_skill_factory"]
    assert len(maintenance_calls[-1]["actions_applied"]) == 3
    failed_actions = [
        action for action in maintenance_calls[-1]["findings"]["action_results"] if action.get("status") == "failure"
    ]
    assert len(failed_actions) == 1
    assert failed_actions[0]["rollback"]["required"] is True
    assert failed_actions[0]["audit"]["manual_review_required"] is True
    assert failed_actions[0]["rollback"]["status"] == "manual_review_required"
    assert "maintenance-4" in output
    assert '"status": "partial"' in output
