"""
OpenClaw <-> MCUM bridge.

This utility is meant to be called from OpenClaw running inside WSL.
It keeps PostgreSQL behind MCUM and exposes a small, explicit command
surface for context retrieval, task recording, and MCUM-managed command
execution.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


BRIDGE_ROOT = Path(__file__).resolve().parent
MCUM_ROOT = BRIDGE_ROOT.parents[1]
SKILL_PARENT = MCUM_ROOT.parent
REPO_ROOT = BRIDGE_ROOT.parents[4]
WORKSPACE_SESSION = MCUM_ROOT / "workspace_session.py"

if str(SKILL_PARENT) not in sys.path:
    sys.path.insert(0, str(SKILL_PARENT))

from MCUM.core import dispatch  # noqa: E402
from MCUM.core.state_compiler import compile_state  # noqa: E402
from MCUM.db.experience_store import retrieve_for_task  # noqa: E402
from MCUM.db.project_registry import (  # noqa: E402
    get_context_effectiveness_profile,
    get_dispatch_performance_profile,
    get_or_create_project,
    get_retrieval_scope_profile,
)
from MCUM.db.session_playbooks import retrieve_session_playbooks  # noqa: E402
from MCUM.db.skill_catalog import get_skill_record, sync_skill_catalog  # noqa: E402
from MCUM.policy import (  # noqa: E402
    load_execution_policy,
    load_intake_policy,
    normalize_task_brief,
    task_brief_metrics,
    validate_task_brief,
)


TASK_TYPES = ["analizar", "crear", "corregir", "mejorar", "planificar", "validar", "automatizar"]
EXECUTION_MODES = ["analizar", "proponer", "ejecutar"]
RISK_LEVELS = ["bajo", "medio", "alto"]


def _strip_nuls(value: str) -> str:
    return value.replace("\x00", "")


def _wsl_to_windows_path(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("/mnt/") and len(normalized) > 6 and normalized[5].isalpha() and normalized[6] == "/":
        drive = normalized[5].upper()
        suffix = normalized[7:].replace("/", "\\")
        return f"{drive}:\\{suffix}"
    return normalized


def _normalize_path_like(value: str | None) -> str | None:
    if not value:
        return value
    stripped = value.strip()
    if stripped.startswith("/mnt/"):
        return _wsl_to_windows_path(stripped)
    return str(Path(stripped))


def _normalize_source_items(items: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in items or []:
        item = str(raw or "").strip()
        if not item:
            continue
        if item.startswith("http://") or item.startswith("https://"):
            normalized.append(item)
        else:
            normalized.append(_normalize_path_like(item) or item)
    return normalized


def _project_path_from_args(args: argparse.Namespace) -> str:
    raw = getattr(args, "project_path", None)
    if not raw:
        return str(REPO_ROOT)
    return _normalize_path_like(raw) or str(REPO_ROOT)


def _project_name_from_path(project_path: str) -> str:
    return Path(project_path).name or "the workspace"


def _task_brief_from_args(args: argparse.Namespace, project_path: str) -> dict[str, Any]:
    task_brief = normalize_task_brief(
        project_path,
        args.task,
        task_brief={
            "project_path": project_path,
            "task_type": args.task_type,
            "objective": args.objective or args.task,
            "expected_deliverable": args.expected_deliverable or "Deliver a clear, validated result.",
            "sources_to_review": _normalize_source_items(getattr(args, "source_to_review", [])),
            "constraints": list(getattr(args, "constraint", []) or []),
            "success_criteria": args.success_criteria or "Return a validated result and keep MCUM in sync.",
            "execution_mode": args.execution_mode,
            "risk_level": args.risk_level,
            "validation_required": args.validation_required,
            "task_id": getattr(args, "task_id", None),
            "primary_metric": getattr(args, "primary_metric", None),
            "baseline": getattr(args, "metric_baseline", None),
            "target": getattr(args, "metric_target", None),
            "editable_scope": getattr(args, "editable_scope", None),
            "read_only_scope": getattr(args, "read_only_scope", None),
            "protected_scope": getattr(args, "protected_scope", None),
            "iteration_budget": getattr(args, "iteration_budget", None),
            "decision_rule": getattr(args, "decision_rule", None),
            "confirmed": True,
            "brief_source": "openclaw_bridge",
        },
    )
    intake_policy = load_intake_policy()
    task_brief["metrics"] = task_brief_metrics(task_brief, intake_policy)
    issues = validate_task_brief(task_brief, intake_policy)
    if issues:
        raise ValueError("Invalid MCUM task brief: " + ", ".join(issues))
    return task_brief


def _dispatch_payload(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "skill_name": result.skill_name,
        "confidence": result.confidence,
        "match_method": result.match_method,
        "triggered_by": result.triggered_by,
        "semantic_score": result.semantic_score,
        "alternatives": result.alternatives[:3],
        "warnings": result.warnings[:3],
    }


def _build_context_preview(args: argparse.Namespace) -> dict[str, Any]:
    project_path = _project_path_from_args(args)
    project_name = args.project_name or _project_name_from_path(project_path)
    task_brief = _task_brief_from_args(args, project_path)
    execution_policy = load_execution_policy()

    sync_skill_catalog()
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    project_id = project["id"]

    dispatch_learning_profile = get_dispatch_performance_profile(
        project_id=project_id,
        task_type=str(task_brief.get("task_type") or ""),
        execution_mode=str(task_brief.get("execution_mode") or ""),
        limit=80,
        min_samples=3,
        allow_cross_project=bool(execution_policy.get("allow_cross_project_fallback", True)),
    )

    auto_dispatch_result = None
    if args.force_skill:
        auto_dispatch_result = dispatch(
            task_description=args.task,
            project_context=project,
            force_skill=None,
            dispatch_learning_profile=dispatch_learning_profile,
        )

    dispatch_result = dispatch(
        task_description=args.task,
        project_context=project,
        force_skill=args.force_skill,
        dispatch_learning_profile=dispatch_learning_profile,
    )

    skill_record = get_skill_record(dispatch_result.skill_name) or {}
    skill_status = str(skill_record.get("status") or "unknown")

    retrieval_scope_profile = get_retrieval_scope_profile(
        project_id=project_id,
        skill_name=dispatch_result.skill_name,
        task_type=str(task_brief.get("task_type") or ""),
        execution_mode=str(task_brief.get("execution_mode") or ""),
        limit=40,
        min_samples=3,
        allow_cross_project=bool(execution_policy.get("allow_cross_project_fallback", True)),
    )

    retrieval_result = retrieve_for_task(
        args.task,
        skill_context=dispatch_result.skill_name,
        project_id=project_id,
        policy=execution_policy,
        scope_learning_profile=retrieval_scope_profile,
    )
    playbook_result = retrieve_session_playbooks(
        args.task,
        skill_name=dispatch_result.skill_name,
        project_id=project_id,
        limit=int(execution_policy.get("max_playbooks", 3) or 3),
        min_similarity=float(execution_policy.get("min_playbook_similarity", 0.28) or 0.28),
        allow_cross_project=bool(execution_policy.get("allow_cross_project_fallback", True)),
    )
    warnings = list(dispatch_result.warnings)
    warnings.extend(retrieval_result.get("warnings", []))
    warnings.extend(playbook_result.get("warnings", []))
    if retrieval_result.get("total_retrieved", 0) == 0:
        warnings.insert(0, "Cold start: no previous experience was retrieved.")

    context_effectiveness = get_context_effectiveness_profile(
        project_id=project_id,
        skill_name=dispatch_result.skill_name,
        task_type=str(task_brief.get("task_type") or ""),
        execution_mode=str(task_brief.get("execution_mode") or ""),
        limit=int((execution_policy.get("state_compiler") or {}).get("effectiveness_history_limit", 60) or 60),
        min_samples=int((execution_policy.get("state_compiler") or {}).get("min_effectiveness_samples", 3) or 3),
        allow_cross_project=bool(execution_policy.get("allow_cross_project_fallback", True)),
    )

    compiled_state = compile_state(
        session_id=f"openclaw-preview-{uuid.uuid4()}",
        project_name=project_name,
        project_id=project_id,
        project_scope=retrieval_result.get("project_scope", "unknown"),
        task_description=args.task,
        task_brief=task_brief,
        skill_selected=dispatch_result.skill_name,
        skill_status=skill_status,
        dispatch_confidence=dispatch_result.confidence,
        dispatch_method=dispatch_result.match_method,
        auto_dispatch_result=_dispatch_payload(auto_dispatch_result),
        retrieval_mode=retrieval_result.get("retrieval_mode", "unknown"),
        retrieval_latency_ms=0,
        experiences=retrieval_result.get("experiences", []),
        failure_patterns=retrieval_result.get("failure_patterns", []),
        conflict_cases=retrieval_result.get("conflict_cases", []),
        playbooks=playbook_result.get("playbooks", []),
        warnings=warnings,
        execution_policy=execution_policy,
        effectiveness_profile=context_effectiveness,
    )

    return {
        "project_path": project_path,
        "project_name": project_name,
        "selected_skill": dispatch_result.skill_name,
        "skill_status": skill_status,
        "dispatch_confidence": dispatch_result.confidence,
        "dispatch_method": dispatch_result.match_method,
        "retrieval_mode": retrieval_result.get("retrieval_mode", "unknown"),
        "project_scope": retrieval_result.get("project_scope", "unknown"),
        "warnings": warnings,
        "context_block": compiled_state.to_context_block(),
    }


def _common_workspace_session_args(args: argparse.Namespace, project_path: str) -> list[str]:
    project_name = args.project_name or _project_name_from_path(project_path)
    cmd = [
        sys.executable,
        str(WORKSPACE_SESSION),
        "--project-path",
        project_path,
        "--project-name",
        project_name,
        "--task",
        args.task,
        "--task-type",
        args.task_type,
        "--objective",
        args.objective or args.task,
        "--expected-deliverable",
        args.expected_deliverable or "Deliver a clear, validated result.",
        "--success-criteria",
        args.success_criteria or "Return a validated result and keep MCUM in sync.",
        "--execution-mode",
        args.execution_mode,
        "--risk-level",
        args.risk_level,
    ]

    force_skill = args.force_skill or "mcum-orchestrator"
    cmd.extend(["--force-skill", force_skill])

    for source in _normalize_source_items(getattr(args, "source_to_review", [])):
        cmd.extend(["--source-to-review", source])
    for constraint in list(getattr(args, "constraint", []) or []):
        if str(constraint).strip():
            cmd.extend(["--constraint", str(constraint)])

    if getattr(args, "validation_required", None):
        cmd.extend(["--validation-required", args.validation_required])
    if getattr(args, "task_id", None):
        cmd.extend(["--task-id", args.task_id])
    if getattr(args, "primary_metric", None):
        cmd.extend(["--primary-metric", args.primary_metric])
    if getattr(args, "metric_baseline", None):
        cmd.extend(["--metric-baseline", args.metric_baseline])
    if getattr(args, "metric_target", None):
        cmd.extend(["--metric-target", args.metric_target])
    if getattr(args, "metric_after", None):
        cmd.extend(["--metric-after", args.metric_after])
    if getattr(args, "editable_scope", None):
        cmd.extend(["--editable-scope", args.editable_scope])
    if getattr(args, "read_only_scope", None):
        cmd.extend(["--read-only-scope", args.read_only_scope])
    if getattr(args, "protected_scope", None):
        cmd.extend(["--protected-scope", args.protected_scope])
    if getattr(args, "decision_rule", None):
        cmd.extend(["--decision-rule", args.decision_rule])
    if getattr(args, "decision", None):
        cmd.extend(["--decision", args.decision])
    if getattr(args, "validation_command", None):
        cmd.extend(["--validation-command", args.validation_command])
    if getattr(args, "iteration_budget", None) is not None:
        cmd.extend(["--iteration-budget", str(args.iteration_budget)])
    if getattr(args, "max_runtime_minutes", None) is not None:
        cmd.extend(["--max-runtime-minutes", str(args.max_runtime_minutes)])
    if getattr(args, "emit_program_file", False):
        cmd.append("--emit-program-file")
    if getattr(args, "final_skill", None):
        cmd.extend(["--final-skill", args.final_skill])
    for delegated in list(getattr(args, "delegated_skill", []) or []):
        if str(delegated).strip():
            cmd.extend(["--delegated-skill", str(delegated)])

    if not getattr(args, "verbose_mcum", False):
        cmd.append("--quiet")
    if not getattr(args, "auto_improve", False):
        cmd.append("--no-auto-improve")

    return cmd


def _run_workspace_session(mode: str, args: argparse.Namespace) -> int:
    project_path = _project_path_from_args(args)
    cmd = _common_workspace_session_args(args, project_path)
    cmd.insert(2, mode)

    if mode == "record":
        cmd.extend(["--summary", args.summary, "--outcome", args.outcome, "--confidence", str(args.confidence)])
        if args.error_description:
            cmd.extend(["--error-description", args.error_description])
        if args.validation_summary:
            cmd.extend(["--validation-summary", args.validation_summary])
        if args.save_experience:
            cmd.append("--save-experience")
        if args.experience_title:
            cmd.extend(["--experience-title", args.experience_title])
    elif mode == "run":
        cmd.extend(["--command", args.command])
        if args.workdir:
            cmd.extend(["--workdir", _normalize_path_like(args.workdir) or args.workdir])
        if args.timeout is not None:
            cmd.extend(["--timeout", str(args.timeout)])
        if args.summary:
            cmd.extend(["--summary", args.summary])
        cmd.extend(
            [
                "--confidence-success",
                str(args.confidence_success),
                "--confidence-failure",
                str(args.confidence_failure),
            ]
        )
        if args.allow_exec is not True:
            raise ValueError("Refusing to execute without --allow-exec.")
    else:
        raise ValueError(f"Unsupported workspace_session mode: {mode}")

    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.stdout:
        print(_strip_nuls(completed.stdout).rstrip())
    if completed.stderr:
        print(_strip_nuls(completed.stderr).rstrip(), file=sys.stderr)
    return int(completed.returncode)


def _add_common_bridge_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-path", default=str(REPO_ROOT), help="Workspace project path.")
    parser.add_argument("--project-name", help="Optional project name override.")
    parser.add_argument("--task", required=True, help="Human-readable task description.")
    parser.add_argument("--task-type", choices=TASK_TYPES, default="analizar")
    parser.add_argument("--objective", help="Structured task objective.")
    parser.add_argument("--expected-deliverable", help="Expected deliverable.")
    parser.add_argument("--source-to-review", action="append", default=[], help="Source path or URL to review.")
    parser.add_argument("--constraint", action="append", default=[], help="Execution constraint.")
    parser.add_argument("--success-criteria", help="Concrete success criteria.")
    parser.add_argument("--execution-mode", choices=EXECUTION_MODES, default="analizar")
    parser.add_argument("--risk-level", choices=RISK_LEVELS, default="medio")
    parser.add_argument("--validation-required", help="Validation note for MCUM.")
    parser.add_argument("--task-id", help="Stable task identifier for traceability.")
    parser.add_argument("--primary-metric", help="Primary metric for the task.")
    parser.add_argument("--metric-baseline", help="Metric baseline before execution.")
    parser.add_argument("--metric-target", help="Desired target for the metric.")
    parser.add_argument("--metric-after", help="Observed metric after execution.")
    parser.add_argument("--editable-scope", help="Editable scope for the task.")
    parser.add_argument("--read-only-scope", help="Read-only scope for the task.")
    parser.add_argument("--protected-scope", help="Protected scope for the task.")
    parser.add_argument("--iteration-budget", type=int, default=5, help="Intended iteration budget.")
    parser.add_argument("--decision-rule", help="Decision rule for keep/discard/crash.")
    parser.add_argument("--decision", choices=["keep", "discard", "crash", "partial"], help="Decision outcome.")
    parser.add_argument("--validation-command", help="Validation command for the task program.")
    parser.add_argument("--max-runtime-minutes", type=int, default=30, help="Expected max runtime minutes.")
    parser.add_argument("--emit-program-file", action="store_true", help="Emit PROGRAM_<task_id>.md via workspace_session.")
    parser.add_argument("--force-skill", default="mcum-orchestrator", help="Optional forced skill.")
    parser.add_argument("--final-skill", help="Final skill override for record/run.")
    parser.add_argument("--delegated-skill", action="append", default=[], help="Delegated downstream skill.")
    parser.add_argument("--verbose-mcum", action="store_true", help="Show MCUM lifecycle logs.")
    parser.add_argument("--auto-improve", action="store_true", help="Allow SISL/skill-factory after record/run.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge OpenClaw tasks into MCUM.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    context_parser = subparsers.add_parser("context", help="Preview MCUM context for a task without logging a session.")
    _add_common_bridge_args(context_parser)
    context_parser.add_argument("--format", choices=["text", "json"], default="text")

    record_parser = subparsers.add_parser("record", help="Record a completed task in MCUM.")
    _add_common_bridge_args(record_parser)
    record_parser.add_argument("--summary", required=True, help="Task outcome summary.")
    record_parser.add_argument("--outcome", choices=["success", "partial", "failure"], default="success")
    record_parser.add_argument("--confidence", type=float, default=0.85)
    record_parser.add_argument("--error-description", help="Optional failure details.")
    record_parser.add_argument("--validation-summary", help="Optional validation note.")
    record_parser.add_argument("--save-experience", action="store_true", help="Persist the result as an MCUM experience.")
    record_parser.add_argument("--experience-title", help="Optional experience title.")

    run_parser = subparsers.add_parser("run", help="Execute a PowerShell command through MCUM.")
    _add_common_bridge_args(run_parser)
    run_parser.add_argument("--command", required=True, help="PowerShell command to execute.")
    run_parser.add_argument("--workdir", help="Working directory for the command.")
    run_parser.add_argument("--timeout", type=int, help="Timeout in seconds.")
    run_parser.add_argument("--summary", help="Optional summary override.")
    run_parser.add_argument("--confidence-success", type=float, default=0.9)
    run_parser.add_argument("--confidence-failure", type=float, default=0.25)
    run_parser.add_argument("--allow-exec", action="store_true", help="Required safety flag for command execution.")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.mode == "context":
        preview = _build_context_preview(args)
        if args.format == "json":
            print(json.dumps(preview, ensure_ascii=False, indent=2))
        else:
            print(preview["context_block"])
        return 0

    if args.mode == "record":
        return _run_workspace_session("record", args)

    if args.mode == "run":
        return _run_workspace_session("run", args)

    parser.error(f"Unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
