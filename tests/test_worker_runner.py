from __future__ import annotations

import json

from MCUM.core.worker_runner import build_worker_runner_invocation, resolve_worker_runner


def _worker() -> dict:
    return {
        "role": "implementer",
        "mode": "write",
        "editable_scope": "src/frontend",
        "protected_scope": "src/backend",
        "model_route": {"recommended_model": "gpt-5.3-codex"},
    }


def _brief() -> dict:
    return {
        "worker_role": "implementer",
        "objective": "Build the bounded frontend slice.",
        "expected_deliverable": "Frontend implementation.",
        "success_criteria": "Scope stays inside src/frontend.",
        "validation_required": "Run a focused build check.",
        "editable_scope": "src/frontend",
        "protected_scope": "src/backend",
    }


def test_resolve_worker_runner_supports_explicit_gemini_cli() -> None:
    runner = resolve_worker_runner(
        requested_runner="gemini-cli",
        model_aware_workers=False,
        no_model_aware_workers=False,
        execution_policy={},
    )

    assert runner == "gemini_cli"


def test_resolve_worker_runner_rejects_unknown_explicit_runner() -> None:
    runner = resolve_worker_runner(
        requested_runner="legacy-cli",
        model_aware_workers=False,
        no_model_aware_workers=False,
        execution_policy={},
    )

    assert runner == "powershell"


def test_resolve_worker_runner_supports_explicit_spreadsheet_extractor() -> None:
    runner = resolve_worker_runner(
        requested_runner="spreadsheet-extractor",
        model_aware_workers=False,
        no_model_aware_workers=False,
        execution_policy={},
    )

    assert runner == "spreadsheet_extractor"


def test_resolve_worker_runner_supports_explicit_minimax_sdk() -> None:
    runner = resolve_worker_runner(
        requested_runner="minimax-sdk",
        model_aware_workers=False,
        no_model_aware_workers=False,
        execution_policy={},
    )

    assert runner == "minimax_sdk"


def test_minimax_worker_runner_builds_sdk_payload_without_secrets() -> None:
    invocation = build_worker_runner_invocation(
        runner="minimax_sdk",
        command="Analiza el modulo y devuelve hallazgos compactos.",
        workdir=r"C:\repo",
        project_path=r"C:\repo",
        project_name="demo",
        worker=_worker(),
        worker_brief={**_brief(), "entrypoint_agent": "codex"},
        execution_policy={
            "worker_runner": {
                "minimax_sdk": {
                    "default_model": "MiniMax-M3",
                    "max_prompt_chars": 5000,
                    "max_output_tokens": 900,
                }
            }
        },
    )

    payload = json.loads(invocation["stdin"])
    assert invocation["runner"] == "minimax_sdk"
    assert invocation["args"][1].endswith("minimax_worker.py")
    assert payload["runner"] == "minimax_sdk"
    assert payload["model"] == "MiniMax-M3"
    assert payload["worker_brief"]["entrypoint_agent"] == "codex"
    assert "api_key" not in invocation["stdin"].lower()
    assert invocation["metadata"]["provider"] == "minimax"
    assert invocation["metadata"]["model_aware"] is True


def test_gemini_worker_runner_uses_gemini_policy_and_stdin_brief() -> None:
    invocation = build_worker_runner_invocation(
        runner="gemini_cli",
        command="Implement the initial frontend table.",
        workdir=r"C:\repo",
        project_path=r"C:\repo",
        project_name="demo",
        worker=_worker(),
        worker_brief=_brief(),
        execution_policy={
            "worker_runner": {
                "gemini_cli": {
                    "binary": "gemini.cmd",
                    "default_model": "gemini-2.5-flash",
                    "approval_mode": "yolo",
                    "output_format": "json",
                    "include_project_path": True,
                    "skip_trust": True,
                }
            }
        },
    )

    assert invocation["runner"] == "gemini_cli"
    assert invocation["args"][0] == "gemini.cmd"
    assert ["--model", "gemini-2.5-flash"] == invocation["args"][
        invocation["args"].index("--model") : invocation["args"].index("--model") + 2
    ]
    assert "--skip-trust" in invocation["args"]
    assert "--output-format" in invocation["args"]
    assert "--approval-mode" in invocation["args"]
    assert "--include-directories" in invocation["args"]
    assert invocation["metadata"]["recommended_model"] == "gemini-2.5-flash"
    assert invocation["metadata"]["model_aware"] is True
    assert "agente entrypoint" in invocation["stdin"]
    assert "src/frontend" in invocation["stdin"]
    assert "src/backend" in invocation["stdin"]


def test_unknown_worker_runner_falls_back_to_powershell() -> None:
    invocation = build_worker_runner_invocation(
        runner="legacy_cli",
        command="Implement the bounded frontend slice.",
        workdir=r"C:\repo",
        project_path=r"C:\repo",
        project_name="demo",
        worker=_worker(),
        worker_brief=_brief(),
        worker_timeout_seconds=60,
        execution_policy={},
    )

    assert invocation["runner"] == "powershell"
    assert invocation["args"][:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert invocation["args"][3] == "Implement the bounded frontend slice."
    assert invocation["stdin"] is None
    assert invocation["metadata"]["model_aware"] is False


def test_spreadsheet_extractor_runner_builds_bounded_json_command(tmp_path) -> None:
    workbook_path = tmp_path / "source.xlsx"
    workbook_path.write_bytes(b"fake workbook bytes")

    invocation = build_worker_runner_invocation(
        runner="spreadsheet_extractor",
        command=f"Extract workbook metadata from {workbook_path}",
        workdir=str(tmp_path),
        project_path=str(tmp_path),
        project_name="demo",
        worker=_worker(),
        worker_brief=_brief(),
        execution_policy={
            "worker_runner": {
                "spreadsheet_extractor": {
                    "binary": "python.exe",
                    "max_sheets": 3,
                    "max_rows": 5,
                    "max_cols": 7,
                    "max_scan_rows": 11,
                }
            }
        },
    )

    assert invocation["runner"] == "spreadsheet_extractor"
    assert invocation["args"][0] == "python.exe"
    assert "spreadsheet_extractor.py" in invocation["args"][1]
    assert str(workbook_path.resolve()) in invocation["args"]
    assert "--output" in invocation["args"]
    assert "--max-sheets" in invocation["args"]
    assert "3" in invocation["args"]
    assert invocation["stdin"] is None
    assert invocation["metadata"]["recommended_model"] == "local_openpyxl"
    assert invocation["metadata"]["source_path"] == str(workbook_path.resolve())
    assert invocation["metadata"]["output_path"].endswith(".json")


def test_gemini_worker_runner_falls_back_when_disabled() -> None:
    invocation = build_worker_runner_invocation(
        runner="gemini_cli",
        command="Write-Output legacy",
        workdir=r"C:\repo",
        project_path=r"C:\repo",
        project_name="demo",
        worker=_worker(),
        worker_brief=_brief(),
        execution_policy={"worker_runner": {"gemini_cli": {"enabled": False}}},
    )

    assert invocation["runner"] == "powershell"
    assert invocation["metadata"]["fallback_reason"] == "gemini_cli_disabled"
