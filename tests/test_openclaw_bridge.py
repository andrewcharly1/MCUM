from __future__ import annotations

import argparse
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from MCUM.integrations.openclaw import openclaw_bridge


def _args(**overrides) -> argparse.Namespace:
    values = {
        "project_path": str(openclaw_bridge.REPO_ROOT),
        "project_name": None,
        "task": "Analyze the project structure",
        "task_type": "analizar",
        "objective": None,
        "expected_deliverable": None,
        "source_to_review": [],
        "constraint": [],
        "success_criteria": None,
        "execution_mode": "analizar",
        "risk_level": "medio",
        "validation_required": None,
        "force_skill": None,
        "final_skill": None,
        "delegated_skill": [],
        "verbose_mcum": False,
        "auto_improve": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_project_path_converts_wsl_mounts() -> None:
    args = _args(project_path="/mnt/c/Users/dev/workspace")
    assert openclaw_bridge._project_path_from_args(args) == r"C:\Users\dev\workspace"


def test_context_preview_returns_compiled_block(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(source_to_review=["/mnt/c/Users/dev/file.txt"])

    monkeypatch.setattr(openclaw_bridge, "sync_skill_catalog", lambda: None)
    monkeypatch.setattr(
        openclaw_bridge,
        "get_or_create_project",
        lambda **kwargs: {"id": "project-1", "project_name": "the workspace"},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "get_dispatch_performance_profile",
        lambda **kwargs: {"active": False},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "dispatch",
        lambda **kwargs: SimpleNamespace(
            skill_name="mcum-orchestrator",
            confidence=0.91,
            match_method="forced_by_user" if kwargs.get("force_skill") else "semantic",
            triggered_by="unit-test",
            semantic_score=0.9,
            alternatives=["kaizen"],
            warnings=[],
        ),
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "get_retrieval_scope_profile",
        lambda **kwargs: {"active": False},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "retrieve_for_task",
        lambda *args, **kwargs: {
            "retrieval_mode": "semantic_project",
            "project_scope": "same_project",
            "warnings": [],
            "total_retrieved": 1,
            "experiences": [{"id": "exp-1", "title": "Use MCUM", "category": "implementation_recipe"}],
            "failure_patterns": [],
            "conflict_cases": [],
        },
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "retrieve_session_playbooks",
        lambda *args, **kwargs: {"warnings": [], "playbooks": []},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "get_context_effectiveness_profile",
        lambda **kwargs: {"active": False},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "get_skill_record",
        lambda skill_name: {"status": "active"},
    )
    monkeypatch.setattr(
        openclaw_bridge,
        "compile_state",
        lambda **kwargs: SimpleNamespace(to_context_block=lambda: "compiled context block"),
    )

    preview = openclaw_bridge._build_context_preview(args)

    assert preview["project_name"] == "the workspace"
    assert preview["selected_skill"] == "mcum-orchestrator"
    assert preview["context_block"] == "compiled context block"


def test_run_workspace_session_builds_record_command(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}
    args = _args(
        summary="Finished review",
        outcome="success",
        confidence=0.88,
        error_description=None,
        validation_summary="manual check",
        save_experience=True,
        experience_title="Bridge validation",
        force_skill=None,
    )

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(openclaw_bridge.subprocess, "run", fake_run)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = openclaw_bridge._run_workspace_session("record", args)

    cmd = recorded["command"]
    assert exit_code == 0
    assert cmd[0] == openclaw_bridge.sys.executable
    assert "record" in cmd
    assert "--force-skill" in cmd
    assert "mcum-orchestrator" in cmd
    assert "--quiet" in cmd
    assert "--no-auto-improve" in cmd
    assert "--save-experience" in cmd
    assert "ok" in stdout.getvalue()


def test_run_workspace_session_requires_allow_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(command="Write-Output ok", workdir=None, timeout=None, summary=None, confidence_success=0.9, confidence_failure=0.2, allow_exec=False)
    with pytest.raises(ValueError, match="--allow-exec"):
        openclaw_bridge._run_workspace_session("run", args)
