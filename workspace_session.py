"""
Workspace-level MCUM wrapper for commands and manual task records.

Use this CLI to ensure project work is executed under an MCUM-managed session.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent
SKILL_PARENT = SKILL_ROOT.parent
if str(SKILL_PARENT) not in sys.path:
    sys.path.insert(0, str(SKILL_PARENT))

from MCUM.core import OrchestratorSession, TaskResult
from MCUM.core.skill_factory import run_skill_factory_cycle
from MCUM.db.project_registry import get_or_create_project, log_entry
from MCUM.logging_utils import configure_logging, get_logger
from MCUM.policy import (
    load_intake_policy,
    normalize_task_brief,
    task_brief_metrics,
    validate_task_brief,
)
from MCUM.sisl import run_sisl_cycle
from MCUM.sisl.skill_bootstrap import bootstrap_skill_from_doc
from MCUM.sisl.autonomous_loop import get_current_skill_version

LOGGER = get_logger("workspace_session")


def _clip(text: str | None, limit: int = 1400) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _artifact_payload(paths: list[str], base_dir: str | None = None) -> list[dict]:
    artifacts: list[dict] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute() and base_dir:
            path = Path(base_dir) / path
        path = path.resolve()
        entry = {
            "path": str(path),
            "exists": path.exists(),
            "type": "file" if path.is_file() else "directory" if path.is_dir() else "missing",
        }
        if path.exists() and path.is_file():
            entry["size_bytes"] = path.stat().st_size
        artifacts.append(entry)
    return artifacts


def _existing_paths(artifacts: list[dict]) -> list[str]:
    return [artifact["path"] for artifact in artifacts if artifact.get("exists")]


def _task_skill_payload(args: argparse.Namespace, selected_skill: str | None) -> tuple[str, list[str], str | None]:
    final_skill = str(getattr(args, "final_skill", None) or selected_skill or "mcum-orchestrator").strip()
    delegated_skills: list[str] = []
    for skill_name in list(getattr(args, "delegated_skill", []) or []):
        cleaned = str(skill_name or "").strip()
        if cleaned and cleaned not in delegated_skills and cleaned != final_skill:
            delegated_skills.append(cleaned)

    correction_source = None
    if selected_skill and final_skill != selected_skill:
        correction_source = "workspace_session_final_skill_override"
    elif delegated_skills:
        correction_source = "workspace_session_delegated_execution"

    return final_skill, delegated_skills, correction_source


def _experience_data(args: argparse.Namespace, summary: str, command: str | None = None) -> dict | None:
    if not getattr(args, "save_experience", False):
        return None

    conclusion = args.conclusion or summary or "Task completed under MCUM-managed workflow."
    context = args.context or command or args.task

    return {
        "category": args.experience_category,
        "title": args.experience_title or args.task[:120],
        "content": {
            "conclusion": conclusion,
            "context": context,
        },
        "applicability": {
            "when": f"Use for MCUM-managed tasks in {args.project_path}",
        },
        "not_applicable_cases": {
            "when_not": "The task did not run under an MCUM-managed session or was not validated.",
        },
    }


def _task_brief(args: argparse.Namespace, command: str | None = None) -> dict:
    sources = list(getattr(args, "source_to_review", []) or [])
    constraints = list(getattr(args, "constraint", []) or [])
    validation_required = getattr(args, "validation_required", None)
    if command and not validation_required:
        validation_required = f"Command must complete successfully: {command}"

    return {
        "project_path": args.project_path,
        "task_type": getattr(args, "task_type", None),
        "objective": getattr(args, "objective", None) or args.task,
        "expected_deliverable": getattr(args, "expected_deliverable", None),
        "sources_to_review": sources,
        "constraints": constraints,
        "success_criteria": getattr(args, "success_criteria", None),
        "execution_mode": getattr(args, "execution_mode", None),
        "risk_level": getattr(args, "risk_level", None),
        "validation_required": validation_required,
        "confirmed": True,
        "brief_source": "workspace_session_cli",
    }


def _prompt_text(label: str, default: str | None = None, required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default not in (None, ""):
            return str(default)
        if not required:
            return ""
        print("Este campo es obligatorio.")


def _prompt_choice(label: str, options: list[str], default: str | None = None) -> str:
    options_text = "/".join(options)
    while True:
        value = _prompt_text(f"{label} ({options_text})", default=default, required=True).lower()
        if value in options:
            return value
        print(f"Opción inválida. Debe ser una de: {options_text}")


def _prompt_list(label: str, current: list[str] | None = None) -> list[str]:
    default = ", ".join(current or [])
    raw = _prompt_text(label, default=default, required=False)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_intake_intro() -> None:
    print("MCUM orchestrates the task from intake to retrieval, execution, validation, and logging.")
    print("Answer the intake questions to build a structured brief before any project work begins.\n")


def _print_brief_summary(brief: dict) -> None:
    metrics = brief.get("metrics", {})
    summary = {
        "project_path": brief.get("project_path"),
        "task_type": brief.get("task_type"),
        "objective": brief.get("objective"),
        "expected_deliverable": brief.get("expected_deliverable"),
        "sources_to_review": brief.get("sources_to_review"),
        "constraints": brief.get("constraints"),
        "success_criteria": brief.get("success_criteria"),
        "execution_mode": brief.get("execution_mode"),
        "risk_level": brief.get("risk_level"),
        "validation_required": brief.get("validation_required"),
        "metrics": metrics,
    }
    print("\nMCUM Intake Summary")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _interactive_intake(args: argparse.Namespace, command: str | None = None) -> dict:
    policy = load_intake_policy()
    existing = _task_brief(args, command=command)

    _print_intake_intro()

    project_path = _prompt_text("1. Ruta del proyecto", default=args.project_path, required=True)
    task_type = _prompt_choice("2. Tipo de tarea", policy.get("allowed_task_types", []), default=existing.get("task_type"))
    objective = _prompt_text("3. Objetivo exacto", default=existing.get("objective"), required=True)
    expected_deliverable = _prompt_text(
        "4. Entregable esperado",
        default=existing.get("expected_deliverable"),
        required=True,
    )
    sources_to_review = _prompt_list("5. Archivos / documentos / endpoints a revisar (coma separados)", existing.get("sources_to_review"))
    constraints = _prompt_list("6. Restricciones (coma separadas)", existing.get("constraints"))
    success_criteria = _prompt_text(
        "7. Criterio de éxito",
        default=existing.get("success_criteria"),
        required=True,
    )
    execution_mode = _prompt_choice(
        "8. Modo de trabajo",
        policy.get("allowed_execution_modes", []),
        default=existing.get("execution_mode"),
    )
    risk_level = _prompt_choice("9. Riesgo", ["bajo", "medio", "alto"], default=existing.get("risk_level", "medio"))
    validation_required = _prompt_text(
        "10. Validación requerida",
        default=existing.get("validation_required"),
        required=False,
    )

    candidate = normalize_task_brief(
        project_path,
        args.task,
        task_brief={
            "project_path": project_path,
            "task_type": task_type,
            "objective": objective,
            "expected_deliverable": expected_deliverable,
            "sources_to_review": sources_to_review,
            "constraints": constraints,
            "success_criteria": success_criteria,
            "execution_mode": execution_mode,
            "risk_level": risk_level,
            "validation_required": validation_required,
            "confirmed": False,
            "brief_source": "interactive_intake",
        },
    )
    candidate["metrics"] = task_brief_metrics(candidate, policy)

    issues = validate_task_brief(candidate, policy)
    if issues:
        raise ValueError("Interactive intake produced invalid brief: " + ", ".join(issues))

    _print_brief_summary(candidate)
    confirm = _prompt_choice("Confirmar brief", ["si", "no"], default="si")
    if confirm != "si":
        raise RuntimeError("Interactive intake canceled by user.")

    candidate["confirmed"] = True
    args.project_path = candidate["project_path"]
    return candidate


def _resolve_task_brief(args: argparse.Namespace, command: str | None = None) -> dict:
    interactive = bool(getattr(args, "interactive_intake", False))
    if interactive:
        if not sys.stdin.isatty():
            raise RuntimeError("Interactive intake requires a TTY.")
        return _interactive_intake(args, command=command)

    if not getattr(args, "project_path", None):
        raise RuntimeError("MCUM strict mode requires --project-path or --interactive-intake.")
    if not getattr(args, "task", None):
        raise RuntimeError("MCUM strict mode requires --task or --interactive-intake.")

    brief = normalize_task_brief(args.project_path, args.task, task_brief=_task_brief(args, command=command))
    brief["metrics"] = task_brief_metrics(brief, load_intake_policy())
    return brief


def _abort_active_session(
    session: OrchestratorSession,
    error_description: str,
    output_summary: str,
    validation_summary: str | None = None,
) -> str | None:
    if session.context is None:
        return None
    try:
        return session.abort(
            error_description=error_description,
            output_summary=output_summary,
            validation_summary=validation_summary,
        )
    except Exception as exc:
        LOGGER.warning("MCUM session abort failed: %s", exc)
        return None


def _run_command(args: argparse.Namespace) -> int:
    task_brief = _resolve_task_brief(args, command=args.command)
    workdir = args.workdir or args.project_path
    session = OrchestratorSession(
        project_path=args.project_path,
        project_name=args.project_name,
        task_description=args.task,
        force_skill=args.force_skill,
        verbose=not args.quiet,
        auto_improve=not args.no_auto_improve,
        task_brief=task_brief,
    )
    session_started = False
    session_closed = False

    try:
        ctx = session.begin()
        session_started = True

        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", args.command],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )

        stdout_tail = _clip(completed.stdout)
        stderr_tail = _clip(completed.stderr)
        outcome = "success" if completed.returncode == 0 else "failure"
        confidence = args.confidence_success if outcome == "success" else args.confidence_failure
        final_skill, delegated_skills, correction_source = _task_skill_payload(args, ctx.skill_selected)

        if args.summary:
            summary = args.summary
        else:
            summary_parts = [f"Command executed under MCUM with exit_code={completed.returncode}."]
            if stdout_tail:
                summary_parts.append(f"stdout_tail: {stdout_tail}")
            if stderr_tail:
                summary_parts.append(f"stderr_tail: {stderr_tail}")
            summary = " ".join(summary_parts)

        artifacts = _artifact_payload(args.artifact, workdir)
        log_id = session.close(
            TaskResult(
                task_description=args.task,
                skill_used=final_skill,
                outcome=outcome,
                confidence_score=confidence,
                output_summary=summary,
                artifacts=artifacts,
                error_description=stderr_tail or (f"exit_code={completed.returncode}" if completed.returncode else None),
                experience_data=_experience_data(args, summary, command=args.command),
                validation_summary=f"Command exit_code={completed.returncode}; workdir={workdir}",
                playbook_data={
                    "title": args.experience_title or args.task[:120],
                    "objective": args.objective or args.task,
                    "commands": [args.command],
                    "files_touched": _existing_paths(artifacts),
                    "reusable_when": args.success_criteria or args.validation_required,
                },
                skills_orchestrated=delegated_skills,
                correction_source=correction_source,
            )
        )
        session_closed = True
    except subprocess.TimeoutExpired as exc:
        stdout_tail = _clip(exc.stdout)
        stderr_tail = _clip(exc.stderr)
        timeout_note = f"Command timed out after {args.timeout} second(s)."
        summary_parts = [timeout_note]
        if stdout_tail:
            summary_parts.append(f"stdout_tail: {stdout_tail}")
        if stderr_tail:
            summary_parts.append(f"stderr_tail: {stderr_tail}")
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=timeout_note,
                output_summary=" ".join(summary_parts),
                validation_summary=f"Command timeout; workdir={workdir}",
            )
        raise
    except KeyboardInterrupt:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description="KeyboardInterrupt",
                output_summary="Command interrupted before completion.",
                validation_summary=f"Command interrupted; workdir={workdir}",
            )
        raise
    except Exception as exc:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=str(exc),
                output_summary="Command aborted before completion.",
                validation_summary=f"Unhandled exception; workdir={workdir}",
            )
        raise

    print(f"mcum_log_id={log_id}")
    print(f"mcum_session_id={ctx.session_id}")
    print(f"exit_code={completed.returncode}")
    if stdout_tail:
        print(f"stdout_tail={stdout_tail}")
    if stderr_tail:
        print(f"stderr_tail={stderr_tail}")
    return completed.returncode


def _record_only(args: argparse.Namespace) -> int:
    task_brief = _resolve_task_brief(args)
    session = OrchestratorSession(
        project_path=args.project_path,
        project_name=args.project_name,
        task_description=args.task,
        force_skill=args.force_skill,
        verbose=not args.quiet,
        auto_improve=not args.no_auto_improve,
        task_brief=task_brief,
    )
    session_started = False
    session_closed = False

    try:
        ctx = session.begin()
        session_started = True
        artifacts = _artifact_payload(args.artifact, args.project_path)
        final_skill, delegated_skills, correction_source = _task_skill_payload(args, ctx.skill_selected)
        log_id = session.close(
            TaskResult(
                task_description=args.task,
                skill_used=final_skill,
                outcome=args.outcome,
                confidence_score=args.confidence,
                output_summary=args.summary,
                artifacts=artifacts,
                error_description=args.error_description,
                experience_data=_experience_data(args, args.summary),
                validation_summary=args.validation_summary,
                playbook_data={
                    "title": args.experience_title or args.task[:120],
                    "objective": args.objective or args.task,
                    "files_touched": _existing_paths(artifacts),
                    "reusable_when": args.success_criteria or args.validation_required,
                },
                skills_orchestrated=delegated_skills,
                correction_source=correction_source,
            )
        )
        session_closed = True
    except KeyboardInterrupt:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description="KeyboardInterrupt",
                output_summary="Manual MCUM record interrupted before completion.",
                validation_summary="Manual record interrupted before successful session close.",
            )
        raise
    except Exception as exc:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=str(exc),
                output_summary="Manual MCUM record aborted before completion.",
                validation_summary="Unhandled exception before successful session close.",
            )
        raise

    print(f"mcum_log_id={log_id}")
    print(f"mcum_session_id={ctx.session_id}")
    return 0


def _intake_only(args: argparse.Namespace) -> int:
    args.interactive_intake = True
    if not getattr(args, "task", None):
        args.task = _prompt_text("Resumen breve de la tarea", required=True)
    if not getattr(args, "project_path", None):
        args.project_path = _prompt_text("Ruta del proyecto", required=True)

    brief = _resolve_task_brief(args)
    print("\nConfirmed Task Brief")
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return 0


def _run_skill_factory(args: argparse.Namespace) -> int:
    project_id = None
    if getattr(args, "project_path", None):
        project = get_or_create_project(
            project_path=args.project_path,
            project_name=getattr(args, "project_name", None),
        )
        project_id = project["id"]

    result = run_skill_factory_cycle(
        project_id=project_id,
        auto_bootstrap=not args.promote_only,
        min_occurrences=getattr(args, "min_occurrences", 2),
        low_confidence_threshold=getattr(args, "low_confidence_threshold", 0.72),
        max_candidates=getattr(args, "max_candidates", 1),
        min_active_tests=getattr(args, "min_active_tests", 8),
        min_successful_uses=getattr(args, "min_successful_uses", 2),
        min_success_rate=getattr(args, "min_success_rate", 0.75),
        min_lifecycle_score=getattr(args, "min_lifecycle_score", 0.78),
        min_testing_uses=getattr(args, "min_testing_uses", 2),
        activation_score=getattr(args, "activation_score", 0.82),
        rollback_score=getattr(args, "rollback_score", 0.55),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_sisl_cycle(args: argparse.Namespace) -> int:
    project_id = None
    project_name = getattr(args, "project_name", None)
    if getattr(args, "project_path", None):
        project = get_or_create_project(
            project_path=args.project_path,
            project_name=project_name,
        )
        project_id = project["id"]
        project_name = project.get("project_name", project_name)

    skill_version = args.skill_version or get_current_skill_version(args.skill_name)
    result = run_sisl_cycle(
        skill_name=args.skill_name,
        skill_version=skill_version,
        target_ckl=args.target_ckl,
        verbose=not args.quiet,
        dry_run=bool(args.dry_run),
        persist_eval=not args.no_persist_eval,
        writeback_mode=args.writeback_mode,
    )

    if project_id:
        description = (
            f"skill={args.skill_name}; baseline_ckl={result.get('baseline_ckl_score', 0):.3f}; "
            f"final_ckl={result.get('ckl_score', 0):.3f}; "
            f"proposals={result.get('proposals_n', 0)}; "
            f"applied={len(result.get('applied', []))}; "
            f"mode={args.writeback_mode}; dry_run={bool(args.dry_run)}"
        )
        log_entry(
            project_id=project_id,
            log_type="improvement",
            title=f"Manual SISL cycle: {args.skill_name}",
            description=description,
            skill_used="mcum-orchestrator",
            skills_orchestrated=[args.skill_name],
            outcome="success",
            confidence_score=float(result.get("ckl_score") or 0.0),
            log_metadata={
                "trigger": "workspace_session_cli",
                "skill_name": args.skill_name,
                "skill_version": skill_version,
                "writeback_mode": args.writeback_mode,
                "dry_run": bool(args.dry_run),
                "project_name": project_name,
                "gate_result": result.get("gate_result"),
                "applied": result.get("applied", []),
                "report_id": result.get("report_id"),
                "eval_record_id": result.get("eval_record_id"),
                "candidate_eval_record_id": result.get("candidate_eval_record_id"),
            },
        )

    payload = {
        "skill_name": args.skill_name,
        "skill_version": skill_version,
        "target_ckl": args.target_ckl,
        "dry_run": bool(args.dry_run),
        "writeback_mode": args.writeback_mode,
        "ckl_score": result.get("ckl_score"),
        "baseline_ckl_score": result.get("baseline_ckl_score"),
        "proposals_n": result.get("proposals_n"),
        "high_conf_n": result.get("high_conf_n"),
        "applied": result.get("applied", []),
        "gate_result": result.get("gate_result"),
        "report_id": result.get("report_id"),
        "report_version": result.get("report_version"),
        "eval_record_id": result.get("eval_record_id"),
        "candidate_eval_record_id": result.get("candidate_eval_record_id"),
        "should_continue": result.get("should_continue"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_skill_bootstrap(args: argparse.Namespace) -> int:
    project_id = None
    project_name = getattr(args, "project_name", None)
    if getattr(args, "project_path", None):
        project = get_or_create_project(
            project_path=args.project_path,
            project_name=project_name,
        )
        project_id = project["id"]
        project_name = project.get("project_name", project_name)

    results: list[dict] = []
    for skill_name in list(args.skill_name or []):
        bootstrap = bootstrap_skill_from_doc(
            skill_name,
            project_id=project_id,
            max_tests=args.max_tests,
        )
        entry = {"skill_name": skill_name, "bootstrap": bootstrap}

        if getattr(args, "run_sisl", False):
            skill_version = bootstrap.get("skill_version") or get_current_skill_version(skill_name)
            cycle = run_sisl_cycle(
                skill_name=skill_name,
                skill_version=skill_version,
                target_ckl=args.target_ckl,
                verbose=not args.quiet,
                dry_run=bool(args.sisl_dry_run),
                persist_eval=not args.no_persist_eval,
                writeback_mode=args.writeback_mode,
            )
            entry["sisl_cycle"] = {
                "ckl_score": cycle.get("ckl_score"),
                "baseline_ckl_score": cycle.get("baseline_ckl_score"),
                "proposals_n": cycle.get("proposals_n"),
                "high_conf_n": cycle.get("high_conf_n"),
                "applied": cycle.get("applied", []),
                "gate_result": cycle.get("gate_result"),
                "report_id": cycle.get("report_id"),
                "report_version": cycle.get("report_version"),
            }

        results.append(entry)

    if project_id:
        log_entry(
            project_id=project_id,
            log_type="improvement",
            title="Skill bootstrap cycle",
            description=f"skills={len(results)}; run_sisl={bool(args.run_sisl)}; writeback={args.writeback_mode}",
            skill_used="mcum-orchestrator",
            skills_orchestrated=[item["skill_name"] for item in results],
            outcome="success",
            confidence_score=0.92,
            log_metadata={
                "trigger": "workspace_session_cli",
                "project_name": project_name,
                "results": results,
            },
        )

    print(json.dumps({"results": results}, ensure_ascii=False, indent=2, default=str))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run workspace tasks under an MCUM-managed session.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-path", help="Absolute project path to register in MCUM.")
    common.add_argument("--project-name", help="Optional project name override.")
    common.add_argument("--task", help="Task description to store in MCUM.")
    common.add_argument("--artifact", action="append", default=[], help="Artifact path to attach to the task log.")
    common.add_argument("--force-skill", help="Optional skill name to force in MCUM dispatch. Omit to let MCUM auto-dispatch.")
    common.add_argument("--final-skill", help="Skill that actually resolved the task if it differs from the initially selected skill.")
    common.add_argument("--delegated-skill", action="append", default=[], help="Additional downstream skills involved in the task outcome.")
    common.add_argument("--save-experience", action="store_true", help="Persist the result as an experience.")
    common.add_argument("--experience-title", help="Optional experience title.")
    common.add_argument(
        "--experience-category",
        default="implementation_recipe",
        help="Experience category when --save-experience is enabled.",
    )
    common.add_argument("--conclusion", help="Experience conclusion when --save-experience is enabled.")
    common.add_argument("--context", help="Experience context when --save-experience is enabled.")
    common.add_argument("--quiet", action="store_true", help="Reduce MCUM console output.")
    common.add_argument("--no-auto-improve", action="store_true", help="Disable autonomous SISL after the session closes.")
    common.add_argument("--interactive-intake", action="store_true", help="Ask the structured intake questions before executing the task.")
    common.add_argument("--task-type", choices=["analizar", "crear", "corregir", "mejorar", "planificar", "validar", "automatizar"])
    common.add_argument("--objective", help="Structured task objective for the MCUM brief.")
    common.add_argument("--expected-deliverable", help="Expected deliverable for the MCUM brief.")
    common.add_argument("--source-to-review", action="append", default=[], help="File, endpoint, or document to review.")
    common.add_argument("--constraint", action="append", default=[], help="Execution constraint for the MCUM brief.")
    common.add_argument("--success-criteria", help="Concrete success criteria for the MCUM brief.")
    common.add_argument("--execution-mode", choices=["analizar", "proponer", "ejecutar"], default="ejecutar")
    common.add_argument("--risk-level", choices=["bajo", "medio", "alto"], default="medio")
    common.add_argument("--validation-required", help="Validation requirement to store in the task brief.")

    run_parser = subparsers.add_parser("run", parents=[common], help="Run a command under MCUM.")
    run_parser.add_argument("--command", required=True, help="PowerShell command to execute.")
    run_parser.add_argument("--workdir", help="Working directory for the command.")
    run_parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds.")
    run_parser.add_argument("--summary", help="Optional summary override for the task log.")
    run_parser.add_argument("--confidence-success", type=float, default=0.9)
    run_parser.add_argument("--confidence-failure", type=float, default=0.25)

    record_parser = subparsers.add_parser("record", parents=[common], help="Record a task result without running a command.")
    record_parser.add_argument("--summary", required=True, help="Summary to store in MCUM.")
    record_parser.add_argument("--outcome", choices=["success", "partial", "failure"], default="success")
    record_parser.add_argument("--confidence", type=float, default=0.85)
    record_parser.add_argument("--error-description", help="Optional error details for failure cases.")
    record_parser.add_argument("--validation-summary", help="Optional validation note for manual records.")

    intake_parser = subparsers.add_parser("intake", help="Run an interactive MCUM intake and print the normalized brief.")
    intake_parser.add_argument("--project-path", help="Project path to seed the intake prompt.")
    intake_parser.add_argument("--project-name", help="Optional project name override.")
    intake_parser.add_argument("--task", help="Short task summary to seed the intake prompt.")
    intake_parser.add_argument("--task-type", choices=["analizar", "crear", "corregir", "mejorar", "planificar", "validar", "automatizar"])
    intake_parser.add_argument("--objective", help="Structured task objective for the MCUM brief.")
    intake_parser.add_argument("--expected-deliverable", help="Expected deliverable for the MCUM brief.")
    intake_parser.add_argument("--source-to-review", action="append", default=[], help="File, endpoint, or document to review.")
    intake_parser.add_argument("--constraint", action="append", default=[], help="Execution constraint for the MCUM brief.")
    intake_parser.add_argument("--success-criteria", help="Concrete success criteria for the MCUM brief.")
    intake_parser.add_argument("--execution-mode", choices=["analizar", "proponer", "ejecutar"], default="ejecutar")
    intake_parser.add_argument("--risk-level", choices=["bajo", "medio", "alto"], default="medio")
    intake_parser.add_argument("--validation-required", help="Validation requirement to store in the task brief.")

    factory_parser = subparsers.add_parser("skill-factory", help="Analyze gaps, bootstrap candidate skills, and promote validated candidates.")
    factory_parser.add_argument("--project-path", help="Optional project path to scope signals and improvement logs.")
    factory_parser.add_argument("--project-name", help="Optional project name override.")
    factory_parser.add_argument("--promote-only", action="store_true", help="Do not bootstrap new candidates; only evaluate existing ones.")
    factory_parser.add_argument("--max-candidates", type=int, default=1, help="Maximum candidates to create in one cycle.")
    factory_parser.add_argument("--min-occurrences", type=int, default=2, help="Minimum repeated gap signals required before creating a candidate.")
    factory_parser.add_argument("--low-confidence-threshold", type=float, default=0.72, help="Maximum average confidence considered a coverage gap.")
    factory_parser.add_argument("--min-active-tests", type=int, default=8, help="Minimum active tests required to promote a candidate.")
    factory_parser.add_argument("--min-successful-uses", type=int, default=2, help="Minimum successful task uses required to promote a candidate.")
    factory_parser.add_argument("--min-success-rate", type=float, default=0.75, help="Minimum success rate required to promote a candidate.")
    factory_parser.add_argument("--min-lifecycle-score", type=float, default=0.78, help="Minimum composite lifecycle score required to promote a catalog candidate.")
    factory_parser.add_argument("--min-testing-uses", type=int, default=2, help="Minimum real task uses required before promoting or rolling back a testing skill version.")
    factory_parser.add_argument("--activation-score", type=float, default=0.82, help="Composite score threshold that promotes a testing skill version to active.")
    factory_parser.add_argument("--rollback-score", type=float, default=0.55, help="Composite score threshold that rolls back a weak testing skill version.")

    sisl_parser = subparsers.add_parser("sisl-cycle", help="Run a targeted SISL cycle for one skill under controlled writeback mode.")
    sisl_parser.add_argument("--project-path", help="Optional project path to attach the improvement log.")
    sisl_parser.add_argument("--project-name", help="Optional project name override.")
    sisl_parser.add_argument("--skill-name", required=True, help="Skill name to evaluate and improve.")
    sisl_parser.add_argument("--skill-version", help="Optional skill version override.")
    sisl_parser.add_argument("--target-ckl", type=float, default=0.85, help="Target CKL score for the cycle.")
    sisl_parser.add_argument("--writeback-mode", choices=["disabled", "candidate", "enabled"], default="candidate")
    sisl_parser.add_argument("--dry-run", action="store_true", help="Evaluate and propose without writing to SKILL.md.")
    sisl_parser.add_argument("--no-persist-eval", action="store_true", help="Skip persisting eval rows to the database.")
    sisl_parser.add_argument("--quiet", action="store_true", help="Reduce SISL console output.")

    bootstrap_parser = subparsers.add_parser("skill-bootstrap", help="Seed cold-start skills from local SKILL.md and optionally run SISL immediately.")
    bootstrap_parser.add_argument("--project-path", help="Optional project path to scope seeded experiences and logs.")
    bootstrap_parser.add_argument("--project-name", help="Optional project name override.")
    bootstrap_parser.add_argument("--skill-name", action="append", required=True, help="Skill name to bootstrap. Repeat for multiple skills.")
    bootstrap_parser.add_argument("--max-tests", type=int, default=8, help="Maximum tests to generate after seeding experiences.")
    bootstrap_parser.add_argument("--run-sisl", action="store_true", help="Run a SISL cycle immediately after bootstrapping each skill.")
    bootstrap_parser.add_argument("--target-ckl", type=float, default=0.85, help="Target CKL when --run-sisl is enabled.")
    bootstrap_parser.add_argument("--writeback-mode", choices=["disabled", "candidate", "enabled"], default="disabled")
    bootstrap_parser.add_argument("--sisl-dry-run", action="store_true", help="When --run-sisl is enabled, evaluate without writing to SKILL.md.")
    bootstrap_parser.add_argument("--no-persist-eval", action="store_true", help="Skip persisting eval rows when --run-sisl is enabled.")
    bootstrap_parser.add_argument("--quiet", action="store_true", help="Reduce bootstrap and SISL console output.")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    quiet = bool(getattr(args, "quiet", False))
    configure_logging(logging.WARNING if quiet else None, force=True)

    if getattr(args, "project_path", None):
        project_path = Path(args.project_path).resolve()
        args.project_path = str(project_path)

    if args.mode == "intake":
        return _intake_only(args)
    if args.mode == "run":
        return _run_command(args)
    if args.mode == "skill-factory":
        return _run_skill_factory(args)
    if args.mode == "sisl-cycle":
        return _run_sisl_cycle(args)
    if args.mode == "skill-bootstrap":
        return _run_skill_bootstrap(args)
    return _record_only(args)


if __name__ == "__main__":
    sys.exit(main())
