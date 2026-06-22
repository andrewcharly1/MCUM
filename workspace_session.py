"""
Workspace-level MCUM wrapper for commands and manual task records.

Use this CLI to ensure project work is executed under an MCUM-managed session.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent
SKILL_PARENT = SKILL_ROOT.parent
if str(SKILL_PARENT) not in sys.path:
    sys.path.insert(0, str(SKILL_PARENT))

# Make `from MCUM...` resolve even when the install folder is not named "MCUM"
# (e.g. an npm/npx cache dir named "mcum-orchestrator"). Register an alias
# package pointing at this directory.
if SKILL_ROOT.name != "MCUM" and "MCUM" not in sys.modules:
    import types as _types

    _mcum_alias = _types.ModuleType("MCUM")
    _mcum_alias.__path__ = [str(SKILL_ROOT)]
    sys.modules["MCUM"] = _mcum_alias

from MCUM.core import OrchestratorSession, TaskResult
from MCUM.core.frontend_qa import build_frontend_qa_plan, preflight_playwright_environment, write_frontend_qa_config
from MCUM.core.graph_analytics import analyze_graph
from MCUM.core.graph_compare import compare_graphs
from MCUM.core.graph_exports import export_graph
from MCUM.core.graph_extractors import ArtifactBudget, ArtifactExtractor, ArtifactPolicy
from MCUM.core.graph_impact import analyze_impact
from MCUM.core.graph_policy import load_graph_policy
from MCUM.core.multi_agent import build_multi_agent_plan
from MCUM.core.spec_contract import build_spec_contract, normalize_spec_policy, spec_guardrails
from MCUM.core.worker_runner import build_worker_runner_invocation, resolve_worker_runner
from MCUM.core.code_graph_indexer import scan_project_code_graph
from MCUM.core.code_graph_sync import sync_project_code_graph
from MCUM.core.code_graph_autoindex import ensure_code_graph
from MCUM.core.pattern_discovery import auto_promote_ready_candidates, run_pattern_discovery
from MCUM.core.skill_factory import run_skill_factory_cycle
from MCUM.anti_loop import analyze_problem_loop, enrich_loop_state_with_strategy, sanitize_loop_state
from MCUM.db.experience_store import VALID_CATEGORIES, consolidate_duplicate_experiences
from MCUM.db.project_registry import (
    analyze_anti_loop_dispatch_effectiveness,
    analyze_memory_governor_effectiveness,
    audit_memory_governance,
    detect_maintenance_delta,
    estimate_tokens,
    get_latest_maintenance_run,
    get_or_create_project,
    log_entry,
    log_session_end,
    log_session_start,
    record_agent_invocation,
    record_maintenance_run,
    reap_stale_maintenance_runs,
    refresh_daily_metrics,
    snapshot_project_kpis,
    update_maintenance_run,
)
from MCUM.db.code_graph_store import persist_index_result, retrieve_code_graph_context
from MCUM.db.graph_intelligence_store import (
    get_graph_query_service,
    load_project_graph,
    persist_analytics_result,
    persist_artifact_result,
    persist_comparison_result,
    persist_impact_result,
)
from MCUM.db.unified_graph_store import (
    find_unified_graph_path,
    get_unified_graph_health,
    query_unified_graph,
    sync_unified_project_graph,
)
from MCUM.db.connection import get_db, get_cursor
from MCUM.db.pattern_store import (
    activate_pattern,
    get_activation_backlog,
    get_pattern_health,
    list_review_ready_candidates,
    materialize_candidate_to_draft,
)
from MCUM.db.spec_store import mark_spec_contract_result, upsert_spec_contract
from MCUM.logging_utils import configure_logging, get_logger
from MCUM.policy import (
    apply_execution_profile,
    load_execution_policy,
    load_intake_policy,
    load_maintenance_policy,
    load_pattern_policy,
    normalize_task_brief,
    task_brief_metrics,
    update_execution_policy,
    validate_task_brief,
)
from MCUM.sisl import run_sisl_cycle
from MCUM.sisl.skill_bootstrap import bootstrap_skill_from_doc
from MCUM.sisl.autonomous_loop import get_current_skill_version

LOGGER = get_logger("workspace_session")


def _configure_console_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            encoding = getattr(stream, "encoding", None)
            if encoding:
                stream.reconfigure(encoding=encoding, errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            continue


def _safe_print(*parts: object, sep: str = " ", end: str = "\n") -> None:
    text = sep.join("" if part is None else str(part) for part in parts)
    try:
        print(text, end=end)
        return
    except UnicodeEncodeError:
        pass

    stream = getattr(sys, "stdout", None) or sys.__stdout__
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")

    try:
        stream.write(safe_text + end)
        stream.flush()
        return
    except Exception:
        fallback_text = text.encode("ascii", errors="backslashreplace").decode("ascii")
        fallback_stream = getattr(sys, "__stdout__", None) or stream
        fallback_stream.write(fallback_text + end)
        fallback_stream.flush()


def _runtime_dir(project_path: str) -> Path:
    return Path(project_path).resolve().parents[0] / ".agent" / "runtime"


def _runtime_results_path(project_path: str) -> Path:
    return _runtime_dir(project_path) / "results.tsv"


def _runtime_runs_path(project_path: str) -> Path:
    return _runtime_dir(project_path) / "runs.jsonl"


def _runtime_artifacts_dir(project_path: str) -> Path:
    return _runtime_dir(project_path) / "artifacts"


def _runtime_artifact_path(project_path: str, task_id: str) -> Path:
    return _runtime_artifacts_dir(project_path) / f"MCUM_RESULT_{task_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "task"


def _trace_task_id(args: argparse.Namespace) -> str:
    explicit = str(getattr(args, "task_id", "") or "").strip()
    if explicit:
        return explicit
    project_name = str(getattr(args, "project_name", "") or Path(args.project_path).name)
    generated = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slugify(project_name)}"
    try:
        setattr(args, "task_id", generated)
    except Exception:
        pass
    return generated


def _ensure_runtime_trace_files(project_path: str) -> None:
    runtime_dir = _runtime_dir(project_path)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    results_path = _runtime_results_path(project_path)
    runs_path = _runtime_runs_path(project_path)
    if not results_path.exists():
        results_path.write_text(
            "timestamp\ttask_id\tproject\tscope\tstatus\tmetric_name\tmetric_before\tmetric_after\tvalidation\tdecision\tsummary\tartifacts\n",
            encoding="utf-8",
        )
    if not runs_path.exists():
        runs_path.write_text("", encoding="utf-8")


def _program_template_path(project_path: str) -> Path:
    return _runtime_dir(project_path) / "PROGRAM_TEMPLATE.md"


def _program_output_path(project_path: str, task_id: str) -> Path:
    return _runtime_dir(project_path) / f"PROGRAM_{task_id}.md"


def _task_brief_extras(args: argparse.Namespace) -> dict:
    return {
        "task_id": _trace_task_id(args),
        "primary_metric": getattr(args, "primary_metric", None),
        "baseline": getattr(args, "metric_baseline", None),
        "target": getattr(args, "metric_target", None),
        "editable_scope": getattr(args, "editable_scope", None),
        "read_only_scope": getattr(args, "read_only_scope", None),
        "protected_scope": getattr(args, "protected_scope", None),
        "iteration_budget": getattr(args, "iteration_budget", None),
        "decision_rule": getattr(args, "decision_rule", None),
        "supervised_multi_agent": bool(getattr(args, "supervised_multi_agent", False)),
        "orchestration_role": getattr(args, "orchestration_role", None),
        "worker_role": getattr(args, "worker_role", None),
        "parent_task_id": getattr(args, "parent_task_id", None),
        "parent_session_id": getattr(args, "parent_session_id", None),
        "worker_index": getattr(args, "worker_index", None),
        "worker_count": getattr(args, "worker_count", None),
        "max_workers": getattr(args, "max_workers", None),
        "entrypoint_agent": getattr(args, "entrypoint_agent", None) or os.environ.get("MCUM_ENTRYPOINT_AGENT"),
        "suppress_autonomy_hooks": bool(getattr(args, "suppress_autonomy_hooks", False))
        if getattr(args, "suppress_autonomy_hooks", False)
        else None,
        "allow_worker_learning_writes": bool(getattr(args, "allow_worker_learning_writes", False))
        if getattr(args, "allow_worker_learning_writes", False)
        else None,
    }


def _write_program_file(args: argparse.Namespace) -> str | None:
    if not getattr(args, "emit_program_file", False):
        return None
    _ensure_runtime_trace_files(args.project_path)
    template_path = _program_template_path(args.project_path)
    if not template_path.exists():
        return None
    task_id = _trace_task_id(args)
    target = _program_output_path(args.project_path, task_id)
    template = template_path.read_text(encoding="utf-8")
    project_name = str(getattr(args, "project_name", "") or Path(args.project_path).name)
    replacements = {
        "<TASK_ID>": task_id,
        "<PROJECT_NAME>": project_name,
        "<ABSOLUTE_PATH>": args.project_path,
        "<ISO_TIMESTAMP>": _now_iso(),
        "<WHAT_SUCCESS_LOOKS_LIKE>": str(getattr(args, "objective", None) or getattr(args, "task", "")),
        "<DELIVERABLE>": str(getattr(args, "expected_deliverable", None) or "unspecified"),
        "<PRIMARY_METRIC>": str(getattr(args, "primary_metric", None) or "unspecified"),
        "<BASELINE_VALUE_OR_STATE>": str(getattr(args, "metric_baseline", None) or "unknown"),
        "<TARGET_VALUE_OR_STATE>": str(getattr(args, "metric_target", None) or "improved"),
        "<VALIDATION_COMMAND>": str(getattr(args, "validation_command", None) or getattr(args, "validation_required", None) or "unspecified"),
        "<TESTS_PASS | BUILD_OK | OUTPUT_CREATED>": str(getattr(args, "validation_required", None) or "validation evidence exists"),
    }
    content = template
    for old, new in replacements.items():
        content = content.replace(old, new)
    content = content.replace("<NUMBER>", str(getattr(args, "iteration_budget", None) or 5), 1)
    content = content.replace("<NUMBER>", str(getattr(args, "max_runtime_minutes", None) or 30), 1)
    content = content.replace("<file_or_directory>", str(getattr(args, "editable_scope", None) or "<fill_me>"), 1)
    content = content.replace("<file_or_directory>", str(getattr(args, "read_only_scope", None) or "<fill_me>"), 1)
    content = content.replace("<file_or_directory>", str(getattr(args, "protected_scope", None) or "<fill_me>"), 1)
    target.write_text(content, encoding="utf-8")
    return str(target)


def _append_runtime_trace(project_path: str, payload: dict) -> None:
    _ensure_runtime_trace_files(project_path)
    results_path = _runtime_results_path(project_path)
    runs_path = _runtime_runs_path(project_path)
    row = [
        str(payload.get("timestamp", "")),
        str(payload.get("task_id", "")),
        str(payload.get("project", "")),
        str(payload.get("scope", "")),
        str(payload.get("status", "")),
        str(payload.get("metric_name", "")),
        str(payload.get("metric_before", "")),
        str(payload.get("metric_after", "")),
        str(payload.get("validation", "")),
        str(payload.get("decision", "")),
        str(payload.get("summary", "")),
        str(payload.get("artifacts", "")),
    ]
    with results_path.open("a", encoding="utf-8") as f:
        f.write("\t".join(value.replace("\t", " ").replace("\n", " ") for value in row) + "\n")
    with runs_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _clip(text: str | None, limit: int = 1400) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _parse_runner_stdout_json(stdout: str | None) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    for candidate in (text, text[text.rfind("{") :] if "{" in text else ""):
        if not candidate:
            continue
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runner_usage_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    normalized: dict[str, int] = {}
    for source_key, target_key in (
        ("input_tokens", "input_tokens"),
        ("prompt_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        parsed = _safe_int(usage.get(source_key))
        if parsed is not None:
            normalized[target_key] = parsed
    if "total_tokens" not in normalized and ("input_tokens" in normalized or "output_tokens" in normalized):
        normalized["total_tokens"] = int(normalized.get("input_tokens") or 0) + int(normalized.get("output_tokens") or 0)
    return normalized


def _record_worker_agent_invocation(
    *,
    project_id: str | None,
    session_id: str | None,
    task_log_id: str | None,
    task_id: str,
    role: str,
    runner_label: str,
    runner_metadata: dict[str, Any],
    runner_payload: dict[str, Any],
    outcome: str,
    exit_code: int | None,
    started_at: str,
    finished_at: str,
    wall_clock_ms: int,
    command: str,
) -> str | None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if not project_id:
        return None
    usage = _runner_usage_from_payload(runner_payload)
    try:
        return record_agent_invocation(
            project_id=project_id,
            session_id=session_id,
            task_log_id=task_log_id,
            task_id=task_id,
            agent_role=role,
            runner=runner_label,
            provider=runner_metadata.get("provider") or runner_payload.get("provider"),
            model=runner_metadata.get("recommended_model") or runner_payload.get("model"),
            protocol=runner_metadata.get("protocol") or runner_payload.get("protocol"),
            credential_source=runner_metadata.get("credential_source") or runner_payload.get("source"),
            outcome=outcome,
            exit_code=exit_code,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            prompt_tokens_estimate=runner_metadata.get("prompt_tokens_estimate"),
            wall_clock_ms=wall_clock_ms,
            started_at=started_at,
            finished_at=finished_at,
            metadata={
                "task_id": task_id,
                "command_preview": _clip(command, 500),
                "runner_metadata": runner_metadata,
                "runner_payload_summary": {
                    key: runner_payload.get(key)
                    for key in ("status", "summary", "available", "base_url", "protocol", "model", "source")
                    if key in runner_payload
                },
            },
        )
    except Exception as exc:
        LOGGER.warning("agent_invocation_record_failed runner=%s role=%s error=%s", runner_label, role, exc)
        return None


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


def _is_runtime_internal_path(project_path: str, path: str | None) -> bool:
    if not path:
        return False
    try:
        candidate = Path(path).resolve()
        runtime_root = _runtime_dir(project_path).resolve()
        candidate.relative_to(runtime_root)
        return True
    except Exception:
        return False


def _playbook_files_touched(project_path: str, artifacts: list[dict]) -> list[str]:
    return [
        artifact["path"]
        for artifact in artifacts
        if artifact.get("exists") and not _is_runtime_internal_path(project_path, artifact.get("path"))
    ]


def _merge_artifact_paths(*groups: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not group:
            continue
        if isinstance(group, (str, Path)):
            candidates = [group]
        else:
            try:
                candidates = list(group)
            except TypeError:
                candidates = [group]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _write_runtime_artifact(
    project_path: str,
    task_id: str,
    payload: dict,
) -> str:
    _ensure_runtime_trace_files(project_path)
    artifacts_dir = _runtime_artifacts_dir(project_path)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = _runtime_artifact_path(project_path, task_id)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(target)


def _auto_artifacts(
    args: argparse.Namespace,
    *,
    base_dir: str | None = None,
    task_id: str | None = None,
    runtime_payload: dict | None = None,
    program_path: str | None = None,
) -> list[dict]:
    task_identifier = task_id or _trace_task_id(args)
    runtime_artifact = None
    if not getattr(args, "skip_runtime_artifact", False):
        runtime_artifact = _write_runtime_artifact(
            args.project_path,
            task_identifier,
            runtime_payload or {},
        )
    artifact_paths = _merge_artifact_paths(
        getattr(args, "artifact", []) or [],
        [runtime_artifact] if runtime_artifact else [],
        [program_path] if program_path else [],
    )
    return _artifact_payload(artifact_paths, base_dir)


def _apply_execution_profile_to_args(args: argparse.Namespace, brief: dict[str, Any]) -> dict[str, Any]:
    requested = str(getattr(args, "execution_profile", None) or "auto").strip().lower()
    if requested:
        brief["execution_profile"] = requested

    execution_policy = apply_execution_profile(load_execution_policy(), brief, requested=requested)
    profile_name = str(execution_policy.get("_execution_profile") or requested or "full")
    controls = dict(execution_policy.get("_execution_profile_controls") or {})
    brief["execution_profile"] = profile_name
    brief["execution_profile_controls"] = {
        key: value
        for key, value in controls.items()
        if key in {"no_auto_improve", "skip_daily_guard", "skip_runtime_artifact", "suppress_autonomy_hooks"}
    }
    setattr(args, "_mcum_execution_profile", profile_name)

    if bool(controls.get("no_auto_improve", False)):
        args.no_auto_improve = True
    if bool(controls.get("skip_daily_guard", False)):
        args.skip_daily_guard = True
    if bool(controls.get("skip_runtime_artifact", False)):
        args.skip_runtime_artifact = True
    if bool(controls.get("suppress_autonomy_hooks", False)):
        args.suppress_autonomy_hooks = True
        brief["suppress_autonomy_hooks"] = True
    return brief


def _attach_spec_contract(args: argparse.Namespace, brief: dict[str, Any]) -> dict[str, Any]:
    execution_policy = apply_execution_profile(load_execution_policy(), brief)
    spec_policy = normalize_spec_policy(execution_policy)
    if not spec_policy.get("enabled", True):
        return brief

    task_id = _trace_task_id(args)
    contract = build_spec_contract(brief, task_id=task_id, execution_policy=execution_policy)
    if not contract.get("enabled"):
        return brief

    if bool(getattr(args, "spec_interactive", False)) and contract.get("clarification_questions"):
        if not sys.stdin.isatty():
            raise RuntimeError("Spec interactive mode requires a TTY.")
        clarifications = []
        questions = list(contract.get("clarification_questions") or [])
        for index, question in enumerate(questions, start=1):
            answer = _prompt_text(
                f"Spec {index}/{len(questions)} {question.get('code')}: {question.get('question')}",
                required=True,
            )
            clarifications.append(
                {
                    "code": question.get("code"),
                    "question": question.get("question"),
                    "answer": answer,
                }
            )
        contract["clarifications"] = clarifications
        contract["status"] = "confirmed"
        contract["summary"]["status"] = "confirmed"

    min_score = float(spec_policy.get("min_score", 0.55) or 0.55)
    score = float(contract.get("confidence_score") or 0.0)
    if bool(spec_policy.get("block_on_low_score", False)) and score < min_score:
        raise RuntimeError(f"Spec Contract score too low: {score:.2f} < {min_score:.2f}")

    summary = dict(contract.get("summary") or {})
    if bool(spec_policy.get("persist", True)):
        try:
            project = get_or_create_project(
                project_path=brief.get("project_path") or args.project_path,
                project_name=getattr(args, "project_name", None),
            )
            row = upsert_spec_contract(
                project_id=str(project["id"]),
                task_id=task_id,
                task_brief=brief,
                contract=contract,
                created_by_skill=getattr(args, "force_skill", None) or "mcum-orchestrator",
            )
            summary["id"] = row.get("id")
            summary["project_id"] = str(project["id"])
        except Exception as exc:
            if bool(spec_policy.get("block_on_persist_failure", True)):
                raise
            summary["persist_error"] = str(exc)

    summary["task_id"] = task_id
    summary["execution_profile"] = execution_policy.get("_execution_profile") or brief.get("execution_profile")
    summary["persisted"] = bool(summary.get("id"))
    summary["guardrails"] = spec_guardrails(contract)
    brief["spec_contract"] = summary
    brief["spec_contract_id"] = summary.get("id")

    constraints = list(brief.get("constraints") or [])
    for guardrail in summary["guardrails"]:
        if guardrail not in constraints:
            constraints.append(guardrail)
    brief["constraints"] = constraints
    return brief


def _mark_spec_contract_from_result(
    task_brief: dict[str, Any],
    *,
    log_id: str | None,
    outcome: str,
    summary: str | None,
    validation_summary: str | None,
    artifacts: list[dict] | None,
) -> None:
    spec_summary = dict(task_brief.get("spec_contract") or {})
    contract_id = spec_summary.get("id") or task_brief.get("spec_contract_id")
    if not contract_id:
        return
    status_by_outcome = {
        "success": "fulfilled",
        "partial": "partial",
        "failure": "failed",
    }
    status = status_by_outcome.get(str(outcome or "").lower(), "partial")
    validation_evidence = []
    if validation_summary:
        validation_evidence.append({"kind": "validation_summary", "value": validation_summary})
    artifact_payload = list(artifacts or [])
    trace_links = [
        {
            "link_kind": str(artifact.get("type") or "artifact"),
            "target_ref": str(artifact.get("path") or ""),
            "metadata": {"exists": bool(artifact.get("exists")), "source": "workspace_session"},
        }
        for artifact in artifact_payload
        if artifact.get("path")
    ]
    try:
        mark_spec_contract_result(
            contract_id=str(contract_id),
            status=status,
            result_summary=summary,
            validation_evidence=validation_evidence,
            artifacts=artifact_payload,
            source_task_log_id=log_id,
            trace_links=trace_links,
        )
    except Exception as exc:
        LOGGER.warning("Spec Contract result update failed: %s", exc)


def _coerce_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _maintenance_run_is_today(run: dict[str, Any] | None) -> bool:
    if not run:
        return False
    marker = _coerce_dt(run.get("finished_at") or run.get("started_at") or run.get("created_at"))
    if marker is None:
        return False
    return marker.astimezone().date() == datetime.now().astimezone().date()


def _skip_opportunistic_daily_guard(args: argparse.Namespace, policy: dict[str, Any], mode: str) -> bool:
    if not bool(policy.get("enabled", True)):
        return True
    if os.getenv(str(policy.get("disable_env") or "MCUM_DISABLE_OPPORTUNISTIC_GUARD")):
        return True
    if os.getenv(str(policy.get("env_guard") or "MCUM_DAILY_GUARD_CHILD")):
        return True
    if os.getenv("PYTEST_CURRENT_TEST") and not bool(policy.get("allow_during_tests", False)):
        return True
    if bool(getattr(args, "skip_daily_guard", False)):
        return True
    if bool(getattr(args, "suppress_autonomy_hooks", False)):
        return True
    if bool(policy.get("skip_worker_sessions", True)) and getattr(args, "orchestration_role", None) == "worker":
        return True
    allowed_modes = {str(item) for item in policy.get("modes") or []}
    return bool(allowed_modes and mode not in allowed_modes)


def _maybe_launch_opportunistic_daily_guard(
    args: argparse.Namespace,
    task_brief: dict[str, Any],
    *,
    mode: str,
    task_log_id: str | None,
    outcome: str | None,
) -> dict[str, Any] | None:
    execution_policy = load_execution_policy()
    policy = dict(execution_policy.get("opportunistic_daily_guard") or {})
    if _skip_opportunistic_daily_guard(args, policy, mode):
        return None

    maintenance_name = str(policy.get("maintenance_name") or "daily_guard")
    project_path = str(getattr(args, "project_path", None) or task_brief.get("project_path") or SKILL_ROOT)
    project_name = str(getattr(args, "project_name", None) or Path(project_path).name)

    try:
        project = get_or_create_project(project_path=project_path, project_name=project_name)
        latest_run = get_latest_maintenance_run(project_id=project["id"], maintenance_name=maintenance_name)
        if _maintenance_run_is_today(latest_run):
            return {"status": "already_ran_today", "maintenance_name": maintenance_name, "project_id": str(project["id"])}

        queued_id = None
        if bool(policy.get("record_queued_run", True)):
            try:
                queued_id = record_maintenance_run(
                    project_id=project["id"],
                    maintenance_name=maintenance_name,
                    scope="project",
                    status="queued",
                    trigger_reason=f"opportunistic_after_{mode}",
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                    last_seen_activity_at=datetime.now(timezone.utc),
                    metrics_snapshot={
                        "source_task_log_id": task_log_id,
                        "source_outcome": outcome,
                        "source_task_id": task_brief.get("task_id") or task_brief.get("spec_contract", {}).get("task_id"),
                    },
                    findings={"reason": "daily_guard_not_seen_today", "latest_run": latest_run},
                    actions_applied=[],
                    tokens_estimated=0,
                    notes="Queued by MCUM opportunistic Daily Guard; child process records the final maintenance result.",
                )
            except Exception as exc:
                LOGGER.warning("Daily Guard queued marker failed; continuing with background launch: %s", exc)

        log_dir = _runtime_dir(project_path) / str(policy.get("log_dir_name") or "daily_guard")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{maintenance_name}.log"
        env = dict(os.environ)
        env[str(policy.get("env_guard") or "MCUM_DAILY_GUARD_CHILD")] = "1"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "maintenance-cycle",
            "--project-path",
            project_path,
            "--project-name",
            project_name,
            "--task",
            f"Ejecutar {maintenance_name} oportunista para {project_name}.",
            "--task-type",
            "automatizar",
            "--execution-mode",
            "ejecutar",
            "--risk-level",
            "medio",
            "--force",
            "--quiet",
            "--skip-daily-guard",
        ]
        if queued_id:
            command.extend(["--queued-run-id", str(queued_id)])
        # Fully detach on Windows so the child survives the short-lived parent
        # (e.g. the MCP bridge's `record` call exits immediately). Without
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP the child shares the
        # parent's console/process group and gets torn down before it can
        # record the maintenance result, leaving the queued run to be reaped.
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        popen_kwargs: dict[str, Any] = {
            "cwd": str(SKILL_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "creationflags": creationflags,
        }
        if os.name != "nt":
            # POSIX: detach from the parent session so it is not killed on exit.
            popen_kwargs["start_new_session"] = True
        with log_path.open("a", encoding="utf-8", errors="replace") as log_handle:
            subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **popen_kwargs,
            )
        return {
            "status": "scheduled",
            "maintenance_name": maintenance_name,
            "project_id": str(project["id"]),
            "queued_id": queued_id,
            "log_path": str(log_path),
        }
    except Exception as exc:
        LOGGER.warning("Opportunistic Daily Guard scheduling failed: %s", exc)
        return {"status": "schedule_failed", "error": str(exc), "maintenance_name": maintenance_name}


def _log_maintenance_task(
    *,
    project_path: str,
    project_name: str,
    task_description: str,
    task_brief: dict,
    maintenance_name: str,
    trigger_reason: str,
    summary: str,
    validation_summary: str,
    artifacts: list[dict],
    confidence_score: float,
    outcome: str = "success",
    started_at_perf: float,
    findings: dict,
    actions: list[dict],
) -> tuple[str, str]:
    session_id = str(uuid.uuid4())
    wall_clock_ms = int((time.perf_counter() - started_at_perf) * 1000)
    duration_sec = max(0, wall_clock_ms // 1000)
    context_tokens_out = estimate_tokens(
        {
            "summary": summary,
            "validation_summary": validation_summary,
            "actions": actions,
        }
    )
    start_info = log_session_start(
        project_path=project_path,
        skill_used="mcum-orchestrator",
        task_description=task_description,
        extra_metadata={
            "session_id": session_id,
            "task_brief": task_brief,
            "maintenance_mode": True,
            "maintenance_name": maintenance_name,
            "trigger_reason": trigger_reason,
        },
    )
    task_log_id = log_entry(
        project_id=start_info["project"]["id"],
        log_type="task",
        title=task_description[:200],
        description=summary,
        skill_used="mcum-orchestrator",
        outcome=outcome,
        artifacts_generated=artifacts,
        session_duration_sec=duration_sec,
        confidence_score=confidence_score,
        context_tokens_in=0,
        context_tokens_out=context_tokens_out,
        task_wall_clock_ms=wall_clock_ms,
        retrieval_latency_ms=0,
        log_metadata={
            "session_id": session_id,
            "task_brief": task_brief,
            "maintenance_mode": True,
            "maintenance_name": maintenance_name,
            "trigger_reason": trigger_reason,
            "validation_summary": validation_summary,
            "maintenance_findings": findings,
            "maintenance_actions": actions,
        },
    )
    log_session_end(
        project_id=start_info["project"]["id"],
        session_duration_sec=duration_sec,
        tasks_completed=1,
        skill_used="mcum-orchestrator",
        outcome=outcome,
        context_tokens_in=0,
        context_tokens_out=context_tokens_out,
        task_wall_clock_ms=wall_clock_ms,
        retrieval_latency_ms=0,
        extra_metadata={
            "session_id": session_id,
            "task_log_id": task_log_id,
            "maintenance_mode": True,
            "maintenance_name": maintenance_name,
            "trigger_reason": trigger_reason,
        },
    )
    return task_log_id, session_id


def _maintenance_self_heal_policy(maintenance_policy: dict) -> dict:
    self_heal_policy = dict(maintenance_policy.get("self_heal") or {})
    safe_actions = list(
        self_heal_policy.get("safe_actions")
        or [
            "refresh_daily_metrics",
            "snapshot_project_kpis",
            "audit_memory_governance",
            "consolidate_duplicate_experiences",
        ]
    )
    adaptive_actions = list(
        self_heal_policy.get("adaptive_actions") or ["run_skill_factory", "tune_anti_loop_dispatch_bias", "tune_memory_governor"]
    )
    manual_review_triggers = list(
        self_heal_policy.get("manual_review_triggers")
        or ["critical_error", "schema_drift", "unexpected_exception", "policy_violation"]
    )
    return {
        "enabled": bool(self_heal_policy.get("enabled", True)),
        "max_actions_per_run": max(0, int(self_heal_policy.get("max_actions_per_run", 3) or 3)),
        "safe_actions": safe_actions,
        "force_executes_safe_actions": bool(self_heal_policy.get("force_executes_safe_actions", True)),
        "adaptive_actions": adaptive_actions,
        "repeat_required_for_adaptive_actions": bool(
            self_heal_policy.get("repeat_required_for_adaptive_actions", True)
        ),
        "manual_review_triggers": manual_review_triggers,
        "continue_on_action_error": bool(self_heal_policy.get("continue_on_action_error", True)),
        "halt_on_manual_review": bool(self_heal_policy.get("halt_on_manual_review", True)),
        "rollback_on_action_failure": bool(self_heal_policy.get("rollback_on_action_failure", True)),
    }


def _classify_maintenance_actions(
    delta: dict,
    maintenance_policy: dict,
    latest_run: dict | None = None,
    force_requested: bool = False,
) -> dict:
    self_heal_policy = _maintenance_self_heal_policy(maintenance_policy)
    recommended_actions: list[str] = []
    for action_name in list(delta.get("recommended_actions") or []):
        cleaned = str(action_name or "").strip()
        if cleaned and cleaned not in recommended_actions:
            recommended_actions.append(cleaned)

    reasons = [str(reason) for reason in list(delta.get("reasons") or []) if str(reason).strip()]
    force_safe_actions = list(self_heal_policy["safe_actions"]) if (force_requested and self_heal_policy["force_executes_safe_actions"]) else []
    previous_reasons: list[str] = []
    if isinstance(latest_run, dict):
        previous_findings = latest_run.get("findings")
        if isinstance(previous_findings, dict):
            previous_delta = previous_findings.get("delta")
            if isinstance(previous_delta, dict):
                previous_reasons = [
                    str(reason)
                    for reason in list(previous_delta.get("reasons") or [])
                    if str(reason).strip()
                ]
            elif previous_findings.get("reasons"):
                previous_reasons = [
                    str(reason)
                    for reason in list(previous_findings.get("reasons") or [])
                    if str(reason).strip()
                ]
        if not previous_reasons and isinstance(latest_run.get("metrics_snapshot"), dict):
            previous_reasons = [
                str(reason)
                for reason in list(latest_run["metrics_snapshot"].get("reasons") or [])
                if str(reason).strip()
            ]
    repeated_reasons = [reason for reason in reasons if reason in previous_reasons]
    repeat_patterns: list[str] = []
    if any(reason in repeated_reasons for reason in ("metrics_stale", "kpi_stale")):
        repeat_patterns.append("stale_metrics_repeat")
    if "candidate_pressure" in repeated_reasons:
        repeat_patterns.append("catalog_pressure_repeat")
    if any(reason in repeated_reasons for reason in ("partial_missing_artifacts", "success_rate_low")):
        repeat_patterns.append("quality_gap_repeat")
    if "anti_loop_dispatch_tuning_needed" in repeated_reasons:
        repeat_patterns.append("anti_loop_dispatch_repeat")
    if "memory_governor_tuning_needed" in repeated_reasons:
        repeat_patterns.append("memory_governor_repeat")

    manual_review_reasons = [reason for reason in reasons if reason in set(self_heal_policy["manual_review_triggers"])]
    base_safe_actions: list[str] = []
    promoted_adaptive_actions: list[str] = []
    blocked_actions: list[str] = []
    for action_name in recommended_actions:
        if action_name in self_heal_policy["safe_actions"]:
            base_safe_actions.append(action_name)
            continue
        if action_name in self_heal_policy["adaptive_actions"]:
            if manual_review_reasons:
                blocked_actions.append(action_name)
            elif self_heal_policy["repeat_required_for_adaptive_actions"] and not repeat_patterns:
                blocked_actions.append(action_name)
            else:
                promoted_adaptive_actions.append(action_name)
            continue
        blocked_actions.append(action_name)

    for action_name in force_safe_actions:
        if (
            action_name not in base_safe_actions
            and action_name not in promoted_adaptive_actions
            and action_name not in blocked_actions
        ):
            base_safe_actions.append(action_name)
    if force_requested and force_safe_actions:
        recommended_actions = list(dict.fromkeys(recommended_actions + force_safe_actions))

    # The per-run cap bounds only promoted *adaptive* actions. Base safe actions
    # (refresh metrics, snapshot KPIs, audit, duplicate consolidation, pattern
    # analysis) are cheap and idempotent, so they always run -- otherwise the
    # tail of the safe list (e.g. consolidate_duplicate_experiences, which sits
    # in position 4) is starved every cycle by the cap and never executes.
    max_actions_per_run = int(self_heal_policy["max_actions_per_run"])
    if max_actions_per_run and len(promoted_adaptive_actions) > max_actions_per_run:
        blocked_actions = promoted_adaptive_actions[max_actions_per_run:] + blocked_actions
        promoted_adaptive_actions = promoted_adaptive_actions[:max_actions_per_run]
    safe_actions = base_safe_actions + promoted_adaptive_actions

    if not self_heal_policy["enabled"]:
        blocked_actions = list(dict.fromkeys(recommended_actions + blocked_actions))
        safe_actions = []
        decision = "disabled"
    elif manual_review_reasons and not safe_actions:
        decision = "manual_review"
    elif safe_actions:
        decision = "forced_proceed" if force_requested else "proceed"
    elif blocked_actions and repeat_patterns:
        decision = "forced_report_only" if force_requested else "report_only"
    elif force_requested:
        decision = "forced_report_only"
    else:
        decision = "skip"

    if manual_review_reasons:
        risk_level = "high"
    elif blocked_actions:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "enabled": self_heal_policy["enabled"],
        "decision": decision,
        "risk_level": risk_level,
        "recommended_actions": recommended_actions,
        "safe_actions": safe_actions,
        "blocked_actions": blocked_actions,
        "repeat_patterns": repeat_patterns,
        "repeated_reasons": repeated_reasons,
        "force_requested": force_requested,
        "force_safe_actions": force_safe_actions,
        "manual_review_reasons": manual_review_reasons,
        "continue_on_action_error": self_heal_policy["continue_on_action_error"],
        "halt_on_manual_review": self_heal_policy["halt_on_manual_review"],
        "rollback_on_action_failure": self_heal_policy["rollback_on_action_failure"],
        "max_actions_per_run": max_actions_per_run,
    }


def _execute_maintenance_action(
    *,
    action_name: str,
    project_id: str,
    maintenance_policy: dict,
    args: argparse.Namespace,
) -> dict:
    started_at_perf = time.perf_counter()
    action_result: dict[str, object] = {
        "action": action_name,
        "status": "skipped",
    }

    try:
        if action_name == "refresh_daily_metrics":
            refresh_summary = refresh_daily_metrics(project_id)
            action_result.update(
                {
                    "status": "success",
                    "risk": "low",
                    "result": {
                        "rows_refreshed": int(refresh_summary.get("rows_refreshed") or 0),
                        "latest_day": refresh_summary.get("latest_day"),
                    },
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "safe",
                        "manual_review_required": False,
                        "rollback_expected": False,
                    },
                }
            )
        elif action_name == "snapshot_project_kpis":
            snapshot_rows = snapshot_project_kpis(
                project_id=project_id,
                window_days=int(maintenance_policy.get("snapshot_window_days", 14) or 14),
                notes=f"maintenance:{getattr(args, 'maintenance_name', 'daily_guard')}",
            )
            action_result.update(
                {
                    "status": "success",
                    "risk": "low",
                    "result": {
                        "rows_snapshot": len(snapshot_rows),
                        "latest_snapshot_date": snapshot_rows[-1].get("snapshot_date") if snapshot_rows else None,
                    },
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "safe",
                        "manual_review_required": False,
                        "rollback_expected": False,
                    },
                }
            )
        elif action_name == "audit_memory_governance":
            audit_summary = audit_memory_governance(
                project_id=project_id,
                policy={"memory_targets": maintenance_policy.get("memory_targets", {})},
            )
            action_result.update(
                {
                    "status": "success",
                    "risk": "low" if audit_summary.get("severity") != "high" else "medium",
                    "result": audit_summary,
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "safe",
                        "manual_review_required": False,
                        "rollback_expected": False,
                    },
                }
            )
        elif action_name == "consolidate_duplicate_experiences":
            consolidation_summary = consolidate_duplicate_experiences(
                project_id=project_id,
                policy={"duplicate_consolidation": maintenance_policy.get("memory_targets", {})},
            )
            action_result.update(
                {
                    "status": "success",
                    "risk": "low",
                    "result": consolidation_summary,
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "safe",
                        "manual_review_required": False,
                        "rollback_expected": False,
                    },
                }
            )
        elif action_name == "analyze_pattern_candidates":
            pattern_maintenance = dict(maintenance_policy.get("pattern_intelligence") or {})
            discovery_summary = run_pattern_discovery(
                project_id=project_id,
                policy=load_pattern_policy(),
                write_candidates=True,
            )
            if discovery_summary.get("status") == "failure":
                raise RuntimeError(str(discovery_summary.get("error") or "Pattern discovery failed."))
            activation_backlog = get_activation_backlog(
                project_id=project_id,
                max_age_days=int(pattern_maintenance.get("activation_backlog_max_age_days", 90) or 90),
                limit=int(pattern_maintenance.get("activation_backlog_limit", 20) or 20),
            )
            # Governed auto-promotion: only acts when pattern_policy.auto_promote
            # is true; each candidate is re-checked against every quality gate by
            # activate_pattern, so a failing gate leaves it as a draft.
            pattern_policy_cfg = load_pattern_policy()
            auto_promote_enabled = bool(pattern_policy_cfg.get("auto_promote", False))
            auto_promotion = (
                auto_promote_ready_candidates(
                    project_id=project_id,
                    policy=pattern_policy_cfg,
                    max_promotions=int(pattern_maintenance.get("activation_backlog_limit", 20) or 20),
                )
                if auto_promote_enabled and discovery_summary.get("status") == "success"
                else {"status": "disabled", "auto_promote": auto_promote_enabled, "promoted": [], "rejected": []}
            )
            promoted_count = len(auto_promotion.get("promoted") or [])
            action_result.update(
                {
                    "status": "success" if discovery_summary.get("status") == "success" else "skipped",
                    "risk": "low",
                    "result": {
                        key: discovery_summary.get(key)
                        for key in (
                            "status",
                            "mode",
                            "discovery_run_id",
                            "experiences_scanned",
                            "embeddings_generated",
                            "embeddings_reused",
                            "groups_analyzed",
                            "candidates_observed",
                            "candidates_review_ready",
                        )
                    },
                    "findings": {
                        "activation_backlog": activation_backlog,
                        "auto_promote": auto_promote_enabled,
                        "auto_promotion": auto_promotion,
                    },
                    "rollback": {"required": False, "status": "not_required"},
                    "audit": {
                        "classification": "governed_auto_promotion" if auto_promote_enabled else "safe_shadow_analysis",
                        "manual_review_required": bool(
                            not auto_promote_enabled and activation_backlog.get("count", 0) > 0
                        ),
                        "rollback_expected": False,
                        "auto_promote": auto_promote_enabled,
                        "patterns_promoted": promoted_count,
                    },
                }
            )
        elif action_name == "run_skill_factory":
            skill_factory_policy = dict(maintenance_policy.get("skill_factory") or {})
            bootstrap_ratio_limit = float(
                skill_factory_policy.get(
                    "max_candidate_ratio_for_bootstrap",
                    (maintenance_policy.get("catalog_targets") or {}).get("max_candidate_active_ratio", 3.0),
                )
                or 3.0
            )
            factory_summary = run_skill_factory_cycle(
                project_id=project_id,
                auto_bootstrap=bool(skill_factory_policy.get("enabled", True)),
                lookback_days=int(skill_factory_policy.get("lookback_days", 30) or 30),
                min_occurrences=int(skill_factory_policy.get("min_occurrences", 2) or 2),
                low_confidence_threshold=float(skill_factory_policy.get("low_confidence_threshold", 0.72) or 0.72),
                max_candidates=int(skill_factory_policy.get("max_candidates", 1) or 1),
                max_candidate_ratio_for_bootstrap=bootstrap_ratio_limit,
                max_pending_results=int(skill_factory_policy.get("max_pending_results", 12) or 12),
                max_monitoring_results=int(skill_factory_policy.get("max_monitoring_results", 5) or 5),
                consolidate_candidate_duplicates=bool(skill_factory_policy.get("enable_candidate_family_consolidation", True)),
                min_active_tests=int(skill_factory_policy.get("min_active_tests", 8) or 8),
                min_successful_uses=int(skill_factory_policy.get("min_successful_uses", 2) or 2),
                min_success_rate=float(skill_factory_policy.get("min_success_rate", 0.75) or 0.75),
                min_lifecycle_score=float(skill_factory_policy.get("min_lifecycle_score", 0.78) or 0.78),
                min_testing_uses=int(skill_factory_policy.get("min_testing_uses", 2) or 2),
                activation_score=float(skill_factory_policy.get("activation_score", 0.82) or 0.82),
                rollback_score=float(skill_factory_policy.get("rollback_score", 0.55) or 0.55),
            )
            action_result.update(
                {
                    "status": "success",
                    "risk": "medium",
                    "result": {
                        "signals": len(factory_summary.get("signals", [])),
                        "created": len(factory_summary.get("created", [])),
                        "promoted": len(factory_summary.get("promoted", [])),
                        "pending": len(factory_summary.get("pending", [])),
                        "applied_hints": len(factory_summary.get("applied_hints", [])),
                        "testing_activated": len(factory_summary.get("testing_reviews", {}).get("activated", [])),
                        "testing_rolled_back": len(factory_summary.get("testing_reviews", {}).get("rolled_back", [])),
                        "pending_total": int(factory_summary.get("pending_total") or 0),
                        "pending_truncated": int(factory_summary.get("pending_truncated") or 0),
                        "monitoring_total": int(factory_summary.get("testing_reviews", {}).get("monitoring_total") or 0),
                        "monitoring_truncated": int(factory_summary.get("testing_reviews", {}).get("monitoring_truncated") or 0),
                        "catalog_pressure": factory_summary.get("catalog_pressure", {}),
                        "actionable_signals_total": int(factory_summary.get("actionable_signals_total") or 0),
                        "actionable_signals_truncated": int(factory_summary.get("actionable_signals_truncated") or 0),
                        "candidate_duplicates_merged": len(factory_summary.get("candidate_consolidation", {}).get("consolidated", [])),
                    },
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "adaptive",
                        "manual_review_required": False,
                        "rollback_expected": False,
                        "repeat_gate_required": bool(skill_factory_policy.get("enabled", True)),
                    },
                }
            )
        elif action_name == "tune_anti_loop_dispatch_bias":
            tuning_summary = analyze_anti_loop_dispatch_effectiveness(
                project_id=project_id,
                policy={
                    "maintenance_name": str(maintenance_policy.get("maintenance_name") or "daily_guard"),
                    "anti_loop_dispatch_tuning": maintenance_policy.get("anti_loop_dispatch_tuning", {}),
                },
            )
            patch_applied = False
            previous_values = {
                "score_boost": tuning_summary.get("current_score_boost"),
                "priority_boost": tuning_summary.get("current_priority_boost"),
            }
            updated_values = dict(previous_values)
            if tuning_summary.get("recommended_action") == "tune_anti_loop_dispatch_bias":
                updated_policy = update_execution_policy(
                    {
                        "anti_loop": {
                            "dispatch_preference_score_boost": tuning_summary.get("suggested_score_boost"),
                            "dispatch_preference_priority_boost": tuning_summary.get("suggested_priority_boost"),
                        }
                    }
                )
                patch_applied = True
                updated_values = {
                    "score_boost": ((updated_policy.get("anti_loop") or {}).get("dispatch_preference_score_boost")),
                    "priority_boost": ((updated_policy.get("anti_loop") or {}).get("dispatch_preference_priority_boost")),
                }
            action_result.update(
                {
                    "status": "success",
                    "risk": "low" if patch_applied else "medium",
                    "result": {
                        "analysis": tuning_summary,
                        "policy_updated": patch_applied,
                        "previous_values": previous_values,
                        "updated_values": updated_values,
                    },
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "adaptive",
                        "manual_review_required": False,
                        "rollback_expected": False,
                        "repeat_gate_required": True,
                    },
                }
            )
        elif action_name == "tune_memory_governor":
            tuning_summary = analyze_memory_governor_effectiveness(
                project_id=project_id,
                policy={
                    "maintenance_name": str(maintenance_policy.get("maintenance_name") or "daily_guard"),
                    "memory_targets": maintenance_policy.get("memory_targets", {}),
                    "memory_governor_tuning": maintenance_policy.get("memory_governor_tuning", {}),
                },
            )
            patch_applied = False
            previous_values = {
                "assist_penalty_weight": tuning_summary.get("current_assist_penalty_weight"),
                "cross_project_risk": tuning_summary.get("current_cross_project_risk"),
                "verbosity_soft_cap": tuning_summary.get("current_verbosity_soft_cap"),
            }
            updated_values = dict(previous_values)
            if tuning_summary.get("recommended_action") == "tune_memory_governor":
                updated_policy = update_execution_policy(
                    {
                        "memory_governor": {
                            "assist_penalty_weight": tuning_summary.get("suggested_assist_penalty_weight"),
                            "cross_project_risk": tuning_summary.get("suggested_cross_project_risk"),
                            "verbosity_soft_cap": tuning_summary.get("suggested_verbosity_soft_cap"),
                        }
                    }
                )
                patch_applied = True
                updated_memory_governor = dict(updated_policy.get("memory_governor") or {})
                updated_values = {
                    "assist_penalty_weight": updated_memory_governor.get("assist_penalty_weight"),
                    "cross_project_risk": updated_memory_governor.get("cross_project_risk"),
                    "verbosity_soft_cap": updated_memory_governor.get("verbosity_soft_cap"),
                }
            action_result.update(
                {
                    "status": "success",
                    "risk": "low" if patch_applied else "medium",
                    "result": {
                        "analysis": tuning_summary,
                        "policy_updated": patch_applied,
                        "previous_values": previous_values,
                        "updated_values": updated_values,
                    },
                    "rollback": {
                        "required": False,
                        "status": "not_required",
                    },
                    "audit": {
                        "classification": "adaptive",
                        "manual_review_required": False,
                        "rollback_expected": False,
                        "repeat_gate_required": True,
                    },
                }
            )
        else:
            action_result.update(
                {
                    "status": "skipped",
                    "risk": "low",
                    "reason": "unsupported_action",
                    "audit": {
                        "classification": "unsupported",
                        "manual_review_required": False,
                        "rollback_expected": False,
                    },
                }
            )
    except Exception as exc:
        rollback_required = action_name == "run_skill_factory"
        action_result.update(
            {
                "status": "failure",
                "risk": "high" if rollback_required else "low",
                "error": str(exc),
                "rollback": {
                    "required": rollback_required,
                    "status": "manual_review_required" if rollback_required else "not_required",
                    "reason": f"{action_name} failed during controlled maintenance cycle.",
                },
                "audit": {
                    "classification": "failure",
                    "manual_review_required": rollback_required,
                    "rollback_expected": rollback_required,
                },
            }
        )
    finally:
        action_result["duration_ms"] = int((time.perf_counter() - started_at_perf) * 1000)

    return action_result


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


def _validated_experience_category(args: argparse.Namespace) -> str | None:
    if not getattr(args, "save_experience", False):
        return None

    category = str(getattr(args, "experience_category", "implementation_recipe") or "").strip()
    if not category:
        category = "implementation_recipe"
    if category not in VALID_CATEGORIES:
        valid = ", ".join(sorted(VALID_CATEGORIES))
        raise ValueError(f"Invalid experience category: {category}. Valid categories: {valid}")
    return category


def _experience_data(args: argparse.Namespace, summary: str, command: str | None = None) -> dict | None:
    if not getattr(args, "save_experience", False):
        return None

    category = _validated_experience_category(args)
    conclusion = args.conclusion or summary or "Task completed under MCUM-managed workflow."
    context = args.context or command or args.task

    return {
        "category": category,
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

    brief = {
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
    brief.update({k: v for k, v in _task_brief_extras(args).items() if v not in (None, "")})
    return brief


def _multi_agent_plan(args: argparse.Namespace, task_brief: dict, selected_skill: str | None = None) -> dict | None:
    if not bool(
        getattr(args, "supervised_multi_agent", False)
        or getattr(args, "emit_multi_agent_plan", False)
        or getattr(args, "auto_multi_run", False)
        or list(getattr(args, "worker_command", []) or [])
    ):
        return None
    execution_policy = apply_execution_profile(load_execution_policy(), task_brief)
    return build_multi_agent_plan(
        task_brief,
        execution_policy,
        selected_skill=selected_skill or getattr(args, "force_skill", None),
    )


def _apply_run_anti_loop_preflight(args: argparse.Namespace, task_brief: dict[str, Any]) -> dict[str, Any]:
    execution_policy = load_execution_policy()
    anti_loop_policy = dict(execution_policy.get("anti_loop") or {})
    if not bool(anti_loop_policy.get("enabled", True)):
        return {}

    try:
        project = get_or_create_project(args.project_path, getattr(args, "project_name", None))
    except Exception:
        return {}

    selected_skill_hint = (
        str(getattr(args, "force_skill", None) or task_brief.get("selected_skill_hint") or "mcum-orchestrator")
        .strip()
    )
    anti_loop_state = analyze_problem_loop(
        project_id=project.get("id"),
        task_description=str(getattr(args, "task", "") or ""),
        task_brief=task_brief,
        policy=anti_loop_policy,
    )
    anti_loop_state = enrich_loop_state_with_strategy(
        loop_state=anti_loop_state,
        skill_name=selected_skill_hint or "mcum-orchestrator",
        dispatch_method="preflight",
        retrieval_mode="preflight",
        execution_mode=task_brief.get("execution_mode"),
        playbook_scope="preflight",
        orchestration={"mode": "single_agent"},
        policy=anti_loop_policy,
    )
    anti_loop_metadata = sanitize_loop_state(anti_loop_state)
    if not anti_loop_metadata.get("enabled"):
        return anti_loop_metadata

    loop_risk = float(anti_loop_metadata.get("loop_risk") or 0.0)
    medium_threshold = float(anti_loop_policy.get("warning_risk_threshold", 0.35) or 0.35)
    recommendation = str(anti_loop_metadata.get("recommendation") or "").strip()
    if (
        recommendation in {"switch_strategy_before_retry", "increase_validation_and_diverge"}
        and loop_risk >= medium_threshold
    ):
        validation_note = (
            "Anti-loop preflight: route through supervised multi-run or attach independent validation before retry."
        )
        validation_required = str(task_brief.get("validation_required") or "").strip()
        if validation_note not in validation_required:
            task_brief["validation_required"] = (
                f"{validation_required} | {validation_note}" if validation_required else validation_note
            )
        constraints = list(task_brief.get("constraints") or [])
        for constraint in [
            "Anti-loop preflight: avoid repeating the same strategy without a changed path.",
            "Anti-loop preflight: compare against an alternate perspective before closing success.",
        ]:
            if constraint not in constraints:
                constraints.append(constraint)
        if constraints:
            task_brief["constraints"] = constraints
        if bool(anti_loop_policy.get("escalate_to_multi_run_on_high_risk", True)) and list(
            getattr(args, "worker_command", []) or []
        ):
            task_brief["supervised_multi_agent"] = True
            task_brief["anti_loop_force_multi_run"] = True

    alternate_skills = [
        str(item).strip()
        for item in (anti_loop_metadata.get("alternate_success_skills") or anti_loop_metadata.get("success_escape_skills") or [])
        if str(item).strip()
    ]
    if alternate_skills and bool(anti_loop_policy.get("allow_preferred_write_skill_hint", True)):
        if not getattr(args, "force_skill", None):
            task_brief["preferred_write_skill_hint"] = alternate_skills[0]
        sources = list(task_brief.get("sources_to_review") or [])
        hint = f"Anti-loop preflight: compare prior successful alternate skill -> {', '.join(alternate_skills[:3])}"
        if hint not in sources:
            sources.append(hint)
            task_brief["sources_to_review"] = sources

    task_brief["anti_loop_preflight"] = anti_loop_metadata
    return anti_loop_metadata


def _resolve_multi_run_worker_commands(args: argparse.Namespace, plan: dict[str, Any] | None) -> dict[str, str]:
    commands = _parse_worker_commands(getattr(args, "worker_command", None))
    if not plan:
        return commands
    worker_policy = dict(plan.get("worker_policy") or {})
    if not bool(worker_policy.get("map_primary_command_to_write_worker", True)):
        return commands
    primary_command = str(getattr(args, "command", "") or "").strip()
    if not primary_command:
        return commands
    write_roles = [str(worker.get("role")) for worker in list(plan.get("workers") or []) if worker.get("mode") == "write"]
    if len(write_roles) == 1 and write_roles[0] not in commands:
        commands[write_roles[0]] = primary_command
    return commands


def _can_auto_promote_to_multi_run(args: argparse.Namespace, plan: dict[str, Any] | None) -> tuple[bool, dict[str, str]]:
    if not plan or plan.get("mode") != "supervised":
        return False, {}
    worker_policy = dict(plan.get("worker_policy") or {})
    execution_policy = worker_policy
    auto_requested = bool(getattr(args, "auto_multi_run", False))
    auto_enabled = bool(execution_policy.get("auto_promote_run_when_complex", False))
    anti_loop_requested = bool(plan.get("anti_loop_recommended"))
    if not (auto_requested or auto_enabled or anti_loop_requested):
        return False, {}

    commands = _resolve_multi_run_worker_commands(args, plan)
    if not commands:
        return False, {}

    if bool(execution_policy.get("require_worker_commands_for_auto_promote", True)) and not list(
        getattr(args, "worker_command", []) or []
    ):
        return False, {}

    if not auto_requested and not (bool(plan.get("recommended")) or anti_loop_requested):
        return False, {}

    write_roles = [str(worker.get("role")) for worker in list(plan.get("workers") or []) if worker.get("mode") == "write"]
    provided_write_roles = [role for role in commands if role in write_roles]
    if provided_write_roles and plan.get("coordinator", {}).get("require_validator", True) and "validator" not in commands:
        return False, commands
    return True, commands


def _compact_worker_message(text: str | None, *, limit: int) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return ""
    return _clip(cleaned, limit=limit)


def _build_multi_run_merge_summary(
    plan: dict[str, Any],
    worker_runs: list[dict[str, Any]],
    phase_reports: list[dict[str, Any]],
    *,
    stop_reason: str | None,
) -> dict[str, Any]:
    merge_policy = dict(plan.get("merge_policy") or {})
    max_worker_highlights = max(1, int(merge_policy.get("max_worker_highlights", 3) or 3))
    max_highlight_chars = max(80, int(merge_policy.get("max_highlight_chars", 160) or 160))
    include_phase_metrics = bool(merge_policy.get("include_phase_metrics", True))

    successful_runs = [run for run in worker_runs if run.get("status") == "success"]
    failed_runs = [run for run in worker_runs if run.get("status") == "failure"]
    skipped_runs = [run for run in worker_runs if run.get("status") == "skipped"]

    highlights: list[str] = []
    for run in successful_runs[:max_worker_highlights]:
        detail = _compact_worker_message(run.get("validation_summary") or run.get("summary"), limit=max_highlight_chars)
        if detail:
            highlights.append(f"{run.get('role')}: {detail}")
    for run in failed_runs[:max(1, max_worker_highlights - len(highlights))]:
        detail = _compact_worker_message(run.get("error") or run.get("validation_summary") or run.get("summary"), limit=max_highlight_chars)
        if detail:
            highlights.append(f"{run.get('role')}: {detail}")

    phase_digest: list[str] = []
    if include_phase_metrics:
        for phase in phase_reports[:max_worker_highlights]:
            roles = ",".join(str(role) for role in phase.get("worker_roles", [])[:4]) or "none"
            phase_digest.append(
                f"phase {phase.get('phase_index')} {phase.get('phase_kind')} "
                f"(parallel={str(bool(phase.get('parallelized'))).lower()}, roles={roles}, "
                f"wall_ms={phase.get('phase_wall_clock_ms')}, overlap_ms={phase.get('estimated_overlap_ms')})"
            )

    summary = (
        f"Coordinated {len(successful_runs) + len(failed_runs)} worker(s): "
        f"{len(successful_runs)} success, {len(failed_runs)} failure, {len(skipped_runs)} skipped."
    )
    if stop_reason:
        summary += f" Stop reason: {stop_reason}."
    if phase_digest:
        summary += f" {' '.join(phase_digest[:2])}"

    model_digest = [
        f"{run.get('role')}->{run.get('recommended_model')}"
        for run in worker_runs
        if run.get("recommended_model")
    ][:max_worker_highlights]
    if model_digest:
        summary += f" Models: {', '.join(model_digest)}."

    validation_summary_parts = list(highlights[:max_worker_highlights])
    if failed_runs:
        failed_roles = ", ".join(str(run.get("role")) for run in failed_runs)
        validation_summary_parts.append(f"failed_roles={failed_roles}")
    validation_summary = " | ".join(part for part in validation_summary_parts if part) or "No worker findings recorded."

    compact_context_lines = [summary]
    compact_context_lines.extend(highlights[:max_worker_highlights])
    compact_context_lines.extend(phase_digest[:max_worker_highlights])
    compact_context_lines.extend(model_digest)
    compact_context = "\n".join(compact_context_lines)

    return {
        "summary": summary,
        "validation_summary": validation_summary,
        "highlights": highlights[:max_worker_highlights],
        "phase_digest": phase_digest[:max_worker_highlights],
        "successful_roles": [str(run.get("role")) for run in successful_runs],
        "failed_roles": [str(run.get("role")) for run in failed_runs],
        "skipped_roles": [str(run.get("role")) for run in skipped_runs],
        "model_digest": model_digest,
        "model_routing": plan.get("model_routing") or {},
        "compact_context": compact_context,
        "compact_context_tokens_estimate": estimate_tokens(compact_context),
    }


def _multi_run_experience_category(task_type: str, worker_runs: list[dict[str, Any]]) -> str:
    successful_write = any(
        run.get("status") == "success" and run.get("mode") == "write"
        for run in worker_runs
    )
    if successful_write:
        return "implementation_recipe"
    if task_type in {"analizar", "validar"}:
        return "testing_strategy"
    if task_type == "planificar":
        return "architecture_pattern"
    return "implementation_recipe"


def _build_multi_run_experience_data(
    args: argparse.Namespace,
    *,
    task_type: str,
    outcome: str,
    worker_runs: list[dict[str, Any]],
    merge_summary: dict[str, Any],
) -> dict[str, Any] | None:
    if outcome not in {"success", "partial"}:
        return None

    highlights = list(merge_summary.get("highlights") or [])[:2]
    phase_digest = list(merge_summary.get("phase_digest") or [])[:2]
    context_parts = highlights + phase_digest
    if not context_parts:
        context_parts = [str(merge_summary.get("summary") or "").strip()]

    successful_roles = ", ".join(merge_summary.get("successful_roles") or []) or "coordinator"
    failed_roles = ", ".join(merge_summary.get("failed_roles") or []) or "none"
    return {
        "category": _multi_run_experience_category(task_type, worker_runs),
        "title": f"Supervised multi-run: {str(args.task)[:96]}",
        "content": {
            "conclusion": merge_summary.get("summary"),
            "context": " | ".join(part for part in context_parts if part),
            "worker_roles": merge_summary.get("successful_roles") or [],
            "failed_roles": merge_summary.get("failed_roles") or [],
        },
        "applicability": {
            "when": (
                f"Use for {task_type} tasks where supervised multi-run with roles "
                f"{successful_roles} fits the problem."
            ),
        },
        "not_applicable_cases": {
            "when_not": (
                f"Avoid when no worker contract exists or when failed roles ({failed_roles}) "
                "suggest a different task shape."
            ),
        },
    }


def _prompt_text(label: str, default: str | None = None, required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError as exc:
            raise RuntimeError(
                "Interactive intake input is unavailable. "
                "Run this command from an interactive terminal or provide the required arguments."
            ) from exc
        if value:
            return value
        if default not in (None, ""):
            return str(default)
        if not required:
            return ""
        _safe_print("Este campo es obligatorio.")


def _prompt_choice(label: str, options: list[str], default: str | None = None) -> str:
    options_text = "/".join(options)
    while True:
        value = _prompt_text(f"{label} ({options_text})", default=default, required=True).lower()
        if value in options:
            return value
        _safe_print(f"Opción inválida. Debe ser una de: {options_text}")


def _prompt_list(label: str, current: list[str] | None = None) -> list[str]:
    default = ", ".join(current or [])
    raw = _prompt_text(label, default=default, required=False)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_intake_intro() -> None:
    _safe_print("MCUM orchestrates the task from intake to retrieval, execution, validation, and logging.")
    _safe_print("Answer the intake questions to build a structured brief before any project work begins.\n")


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
        "task_id": brief.get("task_id"),
        "primary_metric": brief.get("primary_metric"),
        "baseline": brief.get("baseline"),
        "target": brief.get("target"),
        "editable_scope": brief.get("editable_scope"),
        "read_only_scope": brief.get("read_only_scope"),
        "protected_scope": brief.get("protected_scope"),
        "iteration_budget": brief.get("iteration_budget"),
        "metrics": metrics,
    }
    _safe_print("\nMCUM Intake Summary")
    _safe_print(json.dumps(summary, ensure_ascii=False, indent=2))


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
    primary_metric = _prompt_text("11. Métrica principal", default=existing.get("primary_metric"), required=False)
    baseline = _prompt_text("12. Baseline", default=existing.get("baseline"), required=False)
    target = _prompt_text("13. Target", default=existing.get("target"), required=False)
    editable_scope = _prompt_text("14. Scope editable", default=existing.get("editable_scope"), required=False)
    read_only_scope = _prompt_text("15. Scope solo lectura", default=existing.get("read_only_scope"), required=False)
    protected_scope = _prompt_text("16. Scope protegido", default=existing.get("protected_scope"), required=False)
    iteration_budget = _prompt_text("17. Presupuesto de iteraciones", default=str(existing.get("iteration_budget") or ""), required=False)

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
            "primary_metric": primary_metric,
            "baseline": baseline,
            "target": target,
            "editable_scope": editable_scope,
            "read_only_scope": read_only_scope,
            "protected_scope": protected_scope,
            "iteration_budget": int(iteration_budget) if str(iteration_budget).strip().isdigit() else None,
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
    candidate = _apply_execution_profile_to_args(args, candidate)
    candidate = _attach_spec_contract(args, candidate)
    candidate["metrics"] = task_brief_metrics(candidate, load_intake_policy())
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
    brief = _apply_execution_profile_to_args(args, brief)
    brief = _attach_spec_contract(args, brief)
    brief["metrics"] = task_brief_metrics(brief, load_intake_policy())
    return brief


def _run_multi_plan(args: argparse.Namespace) -> int:
    _ensure_runtime_trace_files(args.project_path)
    task_brief = _resolve_task_brief(args)
    task_id = _trace_task_id(args)
    execution_policy = apply_execution_profile(load_execution_policy(), task_brief)
    plan = build_multi_agent_plan(
        task_brief,
        execution_policy,
        selected_skill=getattr(args, "force_skill", None),
    )
    artifact_payload = {
        "task_id": task_id,
        "mode": "multi_plan",
        "project_path": args.project_path,
        "project_name": args.project_name,
        "task": args.task,
        "selected_skill_hint": getattr(args, "force_skill", None),
        "plan": plan,
    }
    artifacts = _auto_artifacts(
        args,
        base_dir=args.project_path,
        task_id=task_id,
        runtime_payload=artifact_payload,
        program_path=None,
    )
    _append_runtime_trace(
        args.project_path,
        {
            "timestamp": _now_iso(),
            "task_id": task_id,
            "project": str(getattr(args, "project_name", "") or Path(args.project_path).name),
            "scope": str(getattr(args, "editable_scope", None) or args.project_path),
            "status": "planned",
            "metric_name": "multi_agent_complexity",
            "metric_before": "",
            "metric_after": str(plan.get("complexity_score")),
            "validation": "multi_agent_plan_generated",
            "decision": str(plan.get("mode")),
            "summary": f"MCUM multi-agent plan generated with {len(plan.get('workers', []))} worker(s).",
            "artifacts": ",".join(_existing_paths(artifacts)),
        },
    )
    _safe_print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _parse_worker_commands(values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in list(values or []):
        text = str(raw or "").strip()
        if not text:
            continue
        role, separator, command = text.partition("=")
        role = role.strip()
        command = command.strip()
        if not separator or not role or not command:
            raise ValueError("Worker commands must use the format role=command.")
        if role in parsed:
            raise ValueError(f"Duplicate worker command for role: {role}")
        parsed[role] = command
    return parsed


def _worker_runtime_artifacts(
    project_path: str,
    task_id: str,
    payload: dict[str, Any],
    *,
    skip_runtime_artifact: bool = False,
) -> list[dict]:
    if skip_runtime_artifact:
        return []
    runtime_artifact = _write_runtime_artifact(project_path, task_id, payload)
    return _artifact_payload([runtime_artifact], project_path)


def _build_multi_run_phases(
    workers: list[dict[str, Any]],
    worker_lookup: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    worker_commands: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    phases: list[dict[str, Any]] = []
    skipped_runs: list[dict[str, Any]] = []

    for worker in workers:
        role = str(worker.get("role"))
        command = worker_commands.get(role)
        if not command:
            skipped_runs.append(
                {
                    "role": role,
                    "mode": worker.get("mode"),
                    "agent_profile": worker.get("agent_profile"),
                    "recommended_model": worker.get("recommended_model"),
                    "status": "skipped",
                    "reason": "no_command_supplied",
                }
            )
            continue
        phase_kind = "write" if worker.get("mode") == "write" else "read_only"
        entry = {
            "role": role,
            "worker": worker,
            "worker_brief": worker_lookup[role][1],
            "command": command,
        }
        if phases and phases[-1]["kind"] == phase_kind:
            phases[-1]["entries"].append(entry)
        else:
            phases.append({"kind": phase_kind, "entries": [entry]})

    return phases, skipped_runs


def _execute_worker_phase(
    args: argparse.Namespace,
    *,
    coordinator_session_id: str,
    coordinator_task_id: str,
    coordinator_skill: str | None,
    phase: dict[str, Any],
    phase_index: int,
    parallel_read_only_workers: bool,
    max_parallel_read_only: int,
) -> list[dict[str, Any]]:
    entries = list(phase.get("entries") or [])
    phase_kind = str(phase.get("kind") or "read_only")
    should_parallelize = (
        phase_kind == "read_only"
        and parallel_read_only_workers
        and len(entries) > 1
    )
    phase_started_at = _now_iso()
    phase_started_at_perf = time.perf_counter()

    def _run_entry(entry: dict[str, Any]) -> dict[str, Any]:
        result = _execute_supervised_worker(
            args,
            coordinator_session_id=coordinator_session_id,
            coordinator_task_id=coordinator_task_id,
            coordinator_skill=coordinator_skill,
            worker=entry["worker"],
            worker_brief=entry["worker_brief"],
            command=entry["command"],
        )
        result["phase_index"] = phase_index
        result["phase_kind"] = phase_kind
        return result

    if not should_parallelize:
        results = [_run_entry(entry) for entry in entries]
    else:
        results: list[dict[str, Any] | None] = [None] * len(entries)
        with ThreadPoolExecutor(max_workers=min(max_parallel_read_only, len(entries))) as executor:
            futures = {executor.submit(_run_entry, entry): index for index, entry in enumerate(entries)}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        results = [result for result in results if result is not None]

    phase_wall_clock_ms = int((time.perf_counter() - phase_started_at_perf) * 1000)
    workers_wall_clock_ms = sum(int(result.get("worker_wall_clock_ms") or 0) for result in results)
    return {
        "results": results,
        "summary": {
            "phase_index": phase_index,
            "phase_kind": phase_kind,
            "parallelized": should_parallelize,
            "worker_roles": [entry["role"] for entry in entries],
            "phase_started_at": phase_started_at,
            "phase_finished_at": _now_iso(),
            "phase_wall_clock_ms": phase_wall_clock_ms,
            "workers_wall_clock_ms": workers_wall_clock_ms,
            "estimated_overlap_ms": max(0, workers_wall_clock_ms - phase_wall_clock_ms),
        },
    }


def _execute_supervised_worker(
    args: argparse.Namespace,
    *,
    coordinator_session_id: str,
    coordinator_task_id: str,
    coordinator_skill: str | None,
    worker: dict[str, Any],
    worker_brief: dict[str, Any],
    command: str,
) -> dict[str, Any]:
    role = str(worker.get("role") or worker_brief.get("worker_role") or "worker")
    entrypoint_agent = getattr(args, "entrypoint_agent", None) or os.environ.get("MCUM_ENTRYPOINT_AGENT")
    worker_brief = dict(worker_brief)
    if entrypoint_agent and not worker_brief.get("entrypoint_agent"):
        worker_brief["entrypoint_agent"] = entrypoint_agent
    worker_task_id = f"{coordinator_task_id}-{_slugify(role)}"
    worker_task_description = f"{args.task} [worker:{role}]"
    workdir = getattr(args, "workdir", None) or args.project_path
    force_skill = worker.get("skill_hint") or worker_brief.get("selected_skill_hint") or coordinator_skill
    model_route = dict(worker.get("model_route") or {})
    execution_policy = load_execution_policy()
    runner = resolve_worker_runner(
        requested_runner=getattr(args, "worker_runner", None),
        model_aware_workers=getattr(args, "model_aware_workers", False),
        no_model_aware_workers=getattr(args, "no_model_aware_workers", False),
        execution_policy=execution_policy,
    )
    runner_invocation = build_worker_runner_invocation(
        runner=runner,
        command=command,
        workdir=workdir,
        project_path=args.project_path,
        project_name=args.project_name,
        worker=worker,
        worker_brief=worker_brief,
        execution_policy=execution_policy,
        worker_timeout_seconds=getattr(args, "timeout", None),
    )
    runner_metadata = dict(runner_invocation.get("metadata") or {})
    session = OrchestratorSession(
        project_path=args.project_path,
        project_name=args.project_name,
        task_description=worker_task_description,
        force_skill=force_skill,
        verbose=not args.quiet,
        auto_improve=False,
        task_brief={
            **dict(worker_brief),
            "task_id": worker_task_id,
            "parent_session_id": coordinator_session_id,
            "parent_task_id": coordinator_task_id,
            "supervised_multi_agent": True,
            "orchestration_role": "worker",
            "worker_role": role,
            "entrypoint_agent": entrypoint_agent,
        },
    )
    session_started = False
    session_closed = False
    ctx = None
    worker_started_at = _now_iso()
    worker_started_at_perf = time.perf_counter()

    try:
        ctx = session.begin()
        session_started = True
        runner_args = list(runner_invocation.get("args") or [])
        completed = subprocess.run(
            runner_args,
            cwd=workdir,
            input=runner_invocation.get("stdin"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=getattr(args, "timeout", None),
        )
        runner_payload = _parse_runner_stdout_json(completed.stdout)
        runner_usage = _runner_usage_from_payload(runner_payload)
        if runner_usage:
            runner_metadata["usage"] = runner_usage
        for payload_key, metadata_key in (
            ("provider", "provider"),
            ("protocol", "protocol"),
            ("model", "recommended_model"),
            ("source", "credential_source"),
            ("base_url", "base_url"),
            ("available", "credential_available"),
        ):
            if payload_key in runner_payload and runner_payload.get(payload_key) not in (None, ""):
                runner_metadata[metadata_key] = runner_payload.get(payload_key)
        stdout_tail = _clip(completed.stdout)
        stderr_tail = _clip(completed.stderr)
        outcome = "success" if completed.returncode == 0 else "failure"
        runner_label = str(runner_invocation.get("runner") or "powershell")
        model_label = runner_metadata.get("recommended_model")
        if not model_label:
            model_label = "cli_default" if runner_label == "gemini_cli" else worker.get("recommended_model")
        summary_parts = [
            f"Worker {role} executed under supervised MCUM via {runner_label} "
            f"model={model_label or 'n/a'} exit_code={completed.returncode}."
        ]
        if stdout_tail:
            summary_parts.append(f"stdout_tail: {stdout_tail}")
        if stderr_tail:
            summary_parts.append(f"stderr_tail: {stderr_tail}")
        summary = " ".join(summary_parts)
        validation_summary = (
            f"Worker {role} exit_code={completed.returncode}; "
            f"runner={runner_label}; "
            f"model={model_label or 'n/a'}; "
            f"validation_required={worker_brief.get('validation_required') or 'unspecified'}; "
            f"workdir={workdir}"
        )
        artifacts = _worker_runtime_artifacts(
            args.project_path,
            worker_task_id,
            {
                "task_id": worker_task_id,
                "mode": "multi_run_worker",
                "parent_task_id": coordinator_task_id,
                "parent_session_id": coordinator_session_id,
                "worker_role": role,
                "worker_mode": worker.get("mode"),
                "agent_profile": worker.get("agent_profile"),
                "recommended_model": worker.get("recommended_model"),
                "model_route": model_route,
                "project_path": args.project_path,
                "project_name": args.project_name,
                "task": worker_task_description,
                "command": command,
                "runner": runner_label,
                "runner_args": list(runner_invocation.get("args") or []),
                "runner_metadata": runner_metadata,
                "runner_payload": runner_payload,
                "workdir": workdir,
                "exit_code": completed.returncode,
                "outcome": outcome,
                "lifecycle_status": "completed",
                "lifecycle_events": [
                    {"status": "running", "at": worker_started_at},
                    {"status": "completed", "at": _now_iso()},
                ],
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "worker_brief": worker_brief,
                "validation_summary": validation_summary,
            },
            skip_runtime_artifact=bool(getattr(args, "skip_runtime_artifact", False)),
        )
        playbook_data = {
            "title": worker_task_description[:120],
            "objective": worker_brief.get("objective") or worker_task_description,
            "commands": [command],
            "files_touched": _playbook_files_touched(args.project_path, artifacts),
            "reusable_when": worker_brief.get("validation_required"),
            "editable_scope": worker_brief.get("editable_scope"),
            "read_only_scope": worker_brief.get("read_only_scope"),
            "protected_scope": worker_brief.get("protected_scope"),
        }
        close_result = session.close(
            TaskResult(
                task_description=worker_task_description,
                skill_used=ctx.skill_selected,
                outcome=outcome,
                confidence_score=0.94 if outcome == "success" else 0.35,
                output_summary=summary,
                artifacts=artifacts,
                error_description=stderr_tail or (f"exit_code={completed.returncode}" if completed.returncode else None),
                validation_summary=validation_summary,
                context_tokens_out=runner_usage.get("output_tokens"),
                playbook_data=playbook_data,
                extra_metadata={
                    "multi_agent_worker": True,
                    "worker_role": role,
                    "parent_task_id": coordinator_task_id,
                    "parent_session_id": coordinator_session_id,
                "worker_mode": worker.get("mode"),
                "agent_profile": worker.get("agent_profile"),
                "recommended_model": worker.get("recommended_model"),
                "model_route": model_route,
                "runner": runner_label,
                "runner_metadata": runner_metadata,
                "runner_payload_status": {
                    key: runner_payload.get(key)
                    for key in ("status", "summary", "available", "protocol", "model", "source", "base_url")
                    if key in runner_payload
                },
            },
        )
        )
        session_closed = True
        log_id = close_result.get("log_id") if isinstance(close_result, dict) else close_result
        invocation_id = _record_worker_agent_invocation(
            project_id=getattr(ctx, "project_id", None),
            session_id=close_result.get("session_id") if isinstance(close_result, dict) else getattr(ctx, "session_id", None),
            task_log_id=log_id,
            task_id=worker_task_id,
            role=role,
            runner_label=runner_label,
            runner_metadata=runner_metadata,
            runner_payload=runner_payload,
            outcome=outcome,
            exit_code=completed.returncode,
            started_at=worker_started_at,
            finished_at=_now_iso(),
            wall_clock_ms=int((time.perf_counter() - worker_started_at_perf) * 1000),
            command=command,
        )
        return {
            "role": role,
            "mode": worker.get("mode"),
            "agent_profile": worker.get("agent_profile"),
            "recommended_model": worker.get("recommended_model"),
            "model_route": model_route,
            "status": outcome,
            "lifecycle_status": "completed",
            "runner": runner_label,
            "runner_metadata": runner_metadata,
            "agent_invocation_id": invocation_id,
            "command": command,
            "task_id": worker_task_id,
            "mcum_log_id": log_id,
            "mcum_session_id": close_result.get("session_id") if isinstance(close_result, dict) else None,
            "mcum_record_status": close_result.get("record_status") if isinstance(close_result, dict) else None,
            "exit_code": completed.returncode,
            "summary": summary,
            "validation_summary": validation_summary,
            "artifacts": artifacts,
            "worker_started_at": worker_started_at,
            "worker_finished_at": _now_iso(),
            "worker_wall_clock_ms": int((time.perf_counter() - worker_started_at_perf) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        stdout_tail = _clip(exc.stdout)
        stderr_tail = _clip(exc.stderr)
        timeout_note = f"Worker {role} timed out after {args.timeout} second(s)."
        timeout_finished_at = _now_iso()
        timeout_artifacts = _worker_runtime_artifacts(
            args.project_path,
            worker_task_id,
            {
                "task_id": worker_task_id,
                "mode": "multi_run_worker",
                "parent_task_id": coordinator_task_id,
                "parent_session_id": coordinator_session_id,
                "worker_role": role,
                "worker_mode": worker.get("mode"),
                "agent_profile": worker.get("agent_profile"),
                "recommended_model": worker.get("recommended_model"),
                "model_route": model_route,
                "project_path": args.project_path,
                "project_name": args.project_name,
                "task": worker_task_description,
                "command": command,
                "runner": str(runner_invocation.get("runner") or "unknown"),
                "runner_args": list(runner_invocation.get("args") or []),
                "runner_metadata": runner_metadata,
                "workdir": workdir,
                "exit_code": None,
                "outcome": "timeout",
                "lifecycle_status": "timeout",
                "lifecycle_events": [
                    {"status": "running", "at": worker_started_at},
                    {"status": "timeout", "at": timeout_finished_at},
                ],
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "worker_brief": worker_brief,
                "validation_summary": f"Worker {role} timeout; workdir={workdir}",
            },
            skip_runtime_artifact=bool(getattr(args, "skip_runtime_artifact", False)),
        )
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=timeout_note,
                output_summary=" ".join(
                    part for part in [timeout_note, f"stdout_tail: {stdout_tail}" if stdout_tail else "", f"stderr_tail: {stderr_tail}" if stderr_tail else ""] if part
                ),
                validation_summary=f"Worker {role} timeout; workdir={workdir}",
            )
        return {
            "role": role,
            "mode": worker.get("mode"),
            "agent_profile": worker.get("agent_profile"),
            "recommended_model": worker.get("recommended_model"),
            "model_route": model_route,
            "status": "timeout",
            "lifecycle_status": "timeout",
            "runner": str(runner_invocation.get("runner") or "unknown"),
            "runner_metadata": runner_metadata,
            "command": command,
            "task_id": worker_task_id,
            "mcum_log_id": None,
            "mcum_session_id": getattr(ctx, "session_id", None),
            "exit_code": None,
            "summary": timeout_note,
            "validation_summary": f"Worker {role} timeout; workdir={workdir}",
            "artifacts": timeout_artifacts,
            "error": timeout_note,
            "worker_started_at": worker_started_at,
            "worker_finished_at": timeout_finished_at,
            "worker_wall_clock_ms": int((time.perf_counter() - worker_started_at_perf) * 1000),
        }
    except Exception as exc:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=str(exc),
                output_summary=f"Worker {role} aborted before completion.",
                validation_summary=f"Worker {role} exception; workdir={workdir}",
            )
        return {
            "role": role,
            "mode": worker.get("mode"),
            "agent_profile": worker.get("agent_profile"),
            "recommended_model": worker.get("recommended_model"),
            "model_route": model_route,
            "status": "failure",
            "lifecycle_status": "failed",
            "runner": str(runner_invocation.get("runner") or "unknown"),
            "runner_metadata": runner_metadata,
            "command": command,
            "task_id": worker_task_id,
            "mcum_log_id": None,
            "mcum_session_id": getattr(ctx, "session_id", None),
            "exit_code": None,
            "summary": f"Worker {role} failed before completion.",
            "validation_summary": f"Worker {role} exception; workdir={workdir}",
            "artifacts": [],
            "error": str(exc),
            "worker_started_at": worker_started_at,
            "worker_finished_at": _now_iso(),
            "worker_wall_clock_ms": int((time.perf_counter() - worker_started_at_perf) * 1000),
        }


def _run_multi_execution(
    args: argparse.Namespace,
    *,
    task_brief: dict[str, Any] | None = None,
    precomputed_plan: dict[str, Any] | None = None,
    precomputed_worker_commands: dict[str, str] | None = None,
    auto_promoted_from: str | None = None,
) -> int:
    _ensure_runtime_trace_files(args.project_path)
    program_path = _write_program_file(args)
    task_brief = dict(task_brief or _resolve_task_brief(args))
    task_id = _trace_task_id(args)
    worker_commands = dict(precomputed_worker_commands or _resolve_multi_run_worker_commands(args, precomputed_plan))
    if not worker_commands:
        raise ValueError("multi-run requires at least one --worker-command role=command.")

    coordinator = OrchestratorSession(
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
        ctx = coordinator.begin()
        session_started = True
        plan_task_brief = {
            **task_brief,
            "project_context_envelope": dict(getattr(ctx, "project_context_envelope", {}) or {}),
        }
        plan = build_multi_agent_plan(
            plan_task_brief,
            load_execution_policy(),
            selected_skill=ctx.skill_selected,
            parent_session_id=ctx.session_id,
        )
        if plan.get("mode") != "supervised":
            raise RuntimeError("The task did not qualify for supervised multi-agent execution.")

        workers = list(plan.get("workers") or [])
        worker_briefs = list(plan.get("worker_briefs") or [])
        worker_lookup = {
            str(worker.get("role")): (worker, worker_brief)
            for worker, worker_brief in zip(workers, worker_briefs, strict=False)
        }
        unknown_roles = sorted(set(worker_commands) - set(worker_lookup))
        if unknown_roles:
            raise ValueError(f"Unknown worker role(s) for multi-run: {', '.join(unknown_roles)}")

        write_roles = [str(worker.get("role")) for worker in workers if worker.get("mode") == "write"]
        provided_write_roles = [role for role in worker_commands if role in write_roles]
        if len(provided_write_roles) > int(plan.get("worker_policy", {}).get("max_write_workers", 1) or 1):
            raise ValueError("multi-run only allows the configured number of write workers.")
        if provided_write_roles and plan.get("coordinator", {}).get("require_validator", True) and "validator" not in worker_commands:
            raise ValueError("Write worker execution requires a validator worker command.")

        worker_policy = dict(plan.get("worker_policy") or {})
        phases, skipped_runs = _build_multi_run_phases(workers, worker_lookup, worker_commands)
        worker_runs: list[dict[str, Any]] = list(skipped_runs)
        phase_reports: list[dict[str, Any]] = []
        stop_reason = None
        remaining_phase_entries: list[dict[str, Any]] = []
        for phase_index, phase in enumerate(phases, start=1):
            phase_outcome = _execute_worker_phase(
                args,
                coordinator_session_id=ctx.session_id,
                coordinator_task_id=task_id,
                coordinator_skill=ctx.skill_selected,
                phase=phase,
                phase_index=phase_index,
                parallel_read_only_workers=bool(worker_policy.get("parallel_read_only_workers", True)),
                max_parallel_read_only=max(1, int(worker_policy.get("max_parallel_read_only", 2) or 2)),
            )
            phase_reports.append(dict(phase_outcome.get("summary") or {}))
            phase_results = list(phase_outcome.get("results") or [])
            worker_runs.extend(phase_results)
            failed_result = next((result for result in phase_results if result.get("status") != "success"), None)
            if failed_result is not None:
                stop_reason = f"worker_{failed_result.get('role')}_failed"
                if bool(worker_policy.get("stop_on_first_failure", True)):
                    remaining_phase_entries = [
                        entry
                        for later_phase in phases[phase_index:]
                        for entry in later_phase.get("entries", [])
                    ]
                    for entry in remaining_phase_entries:
                        worker_runs.append(
                            {
                                "role": entry["role"],
                                "mode": entry["worker"].get("mode"),
                                "agent_profile": entry["worker"].get("agent_profile"),
                                "recommended_model": entry["worker"].get("recommended_model"),
                                "model_route": entry["worker"].get("model_route"),
                                "status": "skipped",
                                "reason": "not_run_due_to_prior_failure",
                            }
                        )
                    break

        if stop_reason is None and remaining_phase_entries:
            stop_reason = "phase_aborted"

        if worker_policy.get("parallel_read_only_workers", True):
            worker_runs.sort(
                key=lambda run: (
                    int(run.get("phase_index") or 999),
                    0 if run.get("status") == "skipped" and run.get("reason") == "no_command_supplied" else 1,
                    str(run.get("role") or ""),
                )
            )

        executed_runs = [run for run in worker_runs if run.get("status") != "skipped"]
        failed_runs = [run for run in executed_runs if run.get("status") != "success"]
        outcome = "success" if executed_runs and not failed_runs else "failure"
        merge_summary = _build_multi_run_merge_summary(
            plan,
            worker_runs,
            phase_reports,
            stop_reason=stop_reason,
        )
        summary = merge_summary["summary"]
        validation_summary = merge_summary["validation_summary"]
        task_type = str(task_brief.get("task_type") or getattr(args, "task_type", "analizar")).lower()
        experience_data = _build_multi_run_experience_data(
            args,
            task_type=task_type,
            outcome=outcome,
            worker_runs=worker_runs,
            merge_summary=merge_summary,
        )
        runtime_payload = {
            "task_id": task_id,
            "mode": "multi_run",
            "auto_promoted_from": auto_promoted_from,
            "project_path": args.project_path,
            "project_name": args.project_name,
            "task": args.task,
            "plan": plan,
            "worker_runs": worker_runs,
            "phase_reports": phase_reports,
            "merge_summary": merge_summary,
            "summary": summary,
            "validation_summary": validation_summary,
            "outcome": outcome,
            "stop_reason": stop_reason,
        }
        artifacts = _auto_artifacts(
            args,
            base_dir=args.project_path,
            task_id=task_id,
            runtime_payload=runtime_payload,
            program_path=program_path,
        )
        playbook_data = {
            "title": args.experience_title or args.task[:120],
            "objective": args.objective or args.task,
            "commands": [f"{role}: {command}" for role, command in worker_commands.items()],
            "files_touched": _playbook_files_touched(args.project_path, artifacts),
            "reusable_when": args.success_criteria or args.validation_required,
            "output_summary": merge_summary["compact_context"],
            "validation_summary": merge_summary["validation_summary"],
            "program_path": program_path,
            "editable_scope": getattr(args, "editable_scope", None),
            "read_only_scope": getattr(args, "read_only_scope", None),
            "protected_scope": getattr(args, "protected_scope", None),
        }
        close_result = coordinator.close(
            TaskResult(
                task_description=args.task,
                skill_used=ctx.skill_selected,
                outcome=outcome,
                confidence_score=0.95 if outcome == "success" else 0.42,
                output_summary=summary,
                artifacts=artifacts,
                error_description=stop_reason,
                experience_data=experience_data,
                validation_summary=validation_summary,
                playbook_data=playbook_data,
                extra_metadata={
                    "multi_agent_plan": plan,
                    "worker_runs": worker_runs,
                    "phase_reports": phase_reports,
                    "merge_summary": merge_summary,
                    "stop_reason": stop_reason,
                    "auto_promoted_from": auto_promoted_from,
                },
            )
        )
        log_id = close_result.get("log_id") if isinstance(close_result, dict) else close_result
        _mark_spec_contract_from_result(
            task_brief,
            log_id=log_id,
            outcome=outcome,
            summary=summary,
            validation_summary=validation_summary,
            artifacts=artifacts,
        )
        _maybe_launch_opportunistic_daily_guard(
            args,
            task_brief,
            mode="multi-run",
            task_log_id=log_id,
            outcome=outcome,
        )
        _append_runtime_trace(
            args.project_path,
            {
                "timestamp": _now_iso(),
                "task_id": task_id,
                "project": str(getattr(args, "project_name", "") or Path(args.project_path).name),
                "scope": str(getattr(args, "editable_scope", None) or args.project_path),
                "status": outcome,
                "metric_name": "multi_run_workers",
                "metric_before": "",
                "metric_after": str(len(executed_runs)),
                "validation": validation_summary,
                "decision": "keep" if outcome == "success" else "crash",
                "summary": summary,
                "artifacts": ",".join(_existing_paths(artifacts) + ([program_path] if program_path else [])),
                "mcum_log_id": log_id,
                "mcum_session_id": close_result.get("session_id") if isinstance(close_result, dict) else ctx.session_id,
                "mcum_record_status": close_result.get("record_status") if isinstance(close_result, dict) else None,
                "multi_agent_mode": plan.get("mode"),
                "merge_tokens_estimate": merge_summary.get("compact_context_tokens_estimate"),
            },
        )
        session_closed = True
    except KeyboardInterrupt:
        if session_started and not session_closed:
            _abort_active_session(
                coordinator,
                error_description="KeyboardInterrupt",
                output_summary="Supervised multi-run interrupted before completion.",
                validation_summary="Coordinator interrupted before successful close.",
            )
        raise
    except Exception as exc:
        if session_started and not session_closed:
            _abort_active_session(
                coordinator,
                error_description=str(exc),
                output_summary="Supervised multi-run aborted before completion.",
                validation_summary="Unhandled exception before successful coordinator close.",
            )
        raise

    _safe_print(f"mcum_log_id={log_id}")
    _safe_print(f"mcum_session_id={ctx.session_id}")
    _safe_print(
        json.dumps(
                {
                    "status": outcome,
                    "worker_runs": worker_runs,
                    "phase_reports": phase_reports,
                    "merge_summary": merge_summary,
                    "stop_reason": stop_reason,
                    "mode": plan.get("mode"),
                    "auto_promoted_from": auto_promoted_from,
                },
                ensure_ascii=False,
                indent=2,
            default=str,
        )
    )
    return 0 if outcome == "success" else 1


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


def _command_file_mentions_playwright(command: str, workdir: str) -> bool:
    for match in re.findall(r"(?P<path>['\"]?[^'\"\s]+\.m?js['\"]?)", command or ""):
        raw = match.strip("'\"")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(workdir) / candidate
        try:
            if candidate.exists() and "playwright" in candidate.read_text(encoding="utf-8", errors="ignore").lower():
                return True
        except OSError:
            continue
    return False


def _playwright_preflight_for_command(command: str, workdir: str) -> dict[str, Any] | None:
    lowered = str(command or "").lower()
    install_intent = "playwright install" in lowered or "npm install" in lowered or "pnpm add" in lowered
    if install_intent:
        return None
    explicit_playwright = "playwright" in lowered or "@playwright" in lowered
    script_uses_playwright = _command_file_mentions_playwright(command, workdir)
    if not explicit_playwright and not script_uses_playwright:
        return None
    preflight = preflight_playwright_environment(workdir)
    missing = set(preflight.get("missing") or [])
    blocker_missing = {"node", "npx", "playwright_browser"}
    if script_uses_playwright:
        blocker_missing.add("local_playwright_package")
    if missing & blocker_missing:
        return preflight
    return None


def _run_command(args: argparse.Namespace) -> int:
    _validated_experience_category(args)
    _ensure_runtime_trace_files(args.project_path)
    program_path = _write_program_file(args)
    task_brief = _resolve_task_brief(args, command=args.command)
    _apply_run_anti_loop_preflight(args, task_brief)
    preflight_multi_agent_plan = _multi_agent_plan(args, task_brief, selected_skill=getattr(args, "force_skill", None))
    should_auto_multi_run, auto_worker_commands = _can_auto_promote_to_multi_run(args, preflight_multi_agent_plan)
    if should_auto_multi_run:
        return _run_multi_execution(
            args,
            task_brief=task_brief,
            precomputed_plan=preflight_multi_agent_plan,
            precomputed_worker_commands=auto_worker_commands,
            auto_promoted_from="run",
        )
    workdir = args.workdir or args.project_path
    playwright_preflight = _playwright_preflight_for_command(args.command, workdir)
    task_id = _trace_task_id(args)
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
        multi_agent_plan = preflight_multi_agent_plan or _multi_agent_plan(args, task_brief, selected_skill=ctx.skill_selected)

        completed = None
        preflight_blocked = bool(playwright_preflight) and not os.getenv("MCUM_BYPASS_PLAYWRIGHT_PREFLIGHT")
        if preflight_blocked:
            stdout_tail = ""
            missing = ", ".join(str(item) for item in list((playwright_preflight or {}).get("missing") or []))
            recommendations = "; ".join(str(item) for item in list((playwright_preflight or {}).get("recommendations") or []))
            stderr_tail = _clip(
                f"Playwright preflight blocked command before expensive retry. "
                f"missing=[{missing}]. recommendations={recommendations}"
            )
            outcome = "failure"
            return_code = None
        else:
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
            return_code = completed.returncode
            outcome = "success" if completed.returncode == 0 else "failure"
        confidence = args.confidence_success if outcome == "success" else args.confidence_failure
        final_skill, delegated_skills, correction_source = _task_skill_payload(args, ctx.skill_selected)

        if args.summary:
            summary = args.summary
        else:
            if preflight_blocked:
                summary_parts = ["Command skipped by MCUM Playwright preflight to avoid blind heavy retry."]
            else:
                summary_parts = [f"Command executed under MCUM with exit_code={return_code}."]
            if stdout_tail:
                summary_parts.append(f"stdout_tail: {stdout_tail}")
            if stderr_tail:
                summary_parts.append(f"stderr_tail: {stderr_tail}")
            summary = " ".join(summary_parts)

        validation_summary = (
            f"Playwright preflight blocked command; workdir={workdir}; "
            f"missing={list((playwright_preflight or {}).get('missing') or [])}"
            if preflight_blocked
            else f"Command exit_code={return_code}; workdir={workdir}"
        )
        artifacts = _auto_artifacts(
            args,
            base_dir=workdir,
            task_id=task_id,
            runtime_payload={
                "task_id": task_id,
                "mode": "run",
                "project_path": args.project_path,
                "project_name": args.project_name,
                "task": args.task,
                "command": args.command,
                "workdir": workdir,
                "exit_code": return_code,
                "outcome": outcome,
                "playwright_preflight": playwright_preflight,
                "preflight_blocked": preflight_blocked,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "summary": summary,
                "validation_summary": validation_summary,
                "multi_agent_plan": multi_agent_plan,
            },
            program_path=program_path,
        )
        playbook_data = {
            "title": args.experience_title or args.task[:120],
            "objective": args.objective or args.task,
            "commands": [args.command],
            "files_touched": _playbook_files_touched(args.project_path, artifacts),
            "reusable_when": args.success_criteria or args.validation_required,
            "program_path": program_path,
            "primary_metric": getattr(args, "primary_metric", None),
            "metric_before": getattr(args, "metric_baseline", None),
            "metric_after": getattr(args, "metric_after", None),
            "decision": getattr(args, "decision", None),
            "editable_scope": getattr(args, "editable_scope", None),
            "read_only_scope": getattr(args, "read_only_scope", None),
            "protected_scope": getattr(args, "protected_scope", None),
        }
        close_result = session.close(
            TaskResult(
                task_description=args.task,
                skill_used=final_skill,
                outcome=outcome,
                confidence_score=confidence,
                output_summary=summary,
                artifacts=artifacts,
                error_description=stderr_tail or (f"exit_code={return_code}" if return_code else None),
                experience_data=_experience_data(args, summary, command=args.command),
                validation_summary=validation_summary,
                playbook_data=playbook_data,
                skills_orchestrated=delegated_skills,
                correction_source=correction_source,
                extra_metadata={
                    **({"multi_agent_plan": multi_agent_plan} if multi_agent_plan else {}),
                    **({"playwright_preflight": playwright_preflight, "preflight_blocked": preflight_blocked} if playwright_preflight else {}),
                },
            )
        )
        log_id = close_result.get("log_id") if isinstance(close_result, dict) else close_result
        _mark_spec_contract_from_result(
            task_brief,
            log_id=log_id,
            outcome=outcome,
            summary=summary,
            validation_summary=validation_summary,
            artifacts=artifacts,
        )
        _maybe_launch_opportunistic_daily_guard(
            args,
            task_brief,
            mode="run",
            task_log_id=log_id,
            outcome=outcome,
        )
        _append_runtime_trace(args.project_path, {
            "timestamp": _now_iso(),
            "task_id": _trace_task_id(args),
            "project": str(getattr(args, "project_name", "") or Path(args.project_path).name),
            "scope": str(getattr(args, "editable_scope", "") or workdir),
            "status": getattr(args, "decision", None)
            or ("blocked" if preflight_blocked else ("keep" if return_code == 0 else "crash")),
            "metric_name": str(getattr(args, "primary_metric", None) or "command_exit_code"),
            "metric_before": str(getattr(args, "metric_baseline", None) or ""),
            "metric_after": str(getattr(args, "metric_after", None) if getattr(args, "metric_after", None) is not None else return_code),
            "validation": validation_summary,
            "decision": str(
                getattr(args, "decision", None)
                or (
                    "blocked por preflight playwright"
                    if preflight_blocked
                    else ("keep por comando exitoso" if return_code == 0 else "crash por exit_code no cero")
                )
            ),
            "summary": summary,
            "artifacts": ",".join(_existing_paths(artifacts) + ([program_path] if program_path else [])),
            "mcum_log_id": log_id,
            "mcum_session_id": close_result.get("session_id") if isinstance(close_result, dict) else ctx.session_id,
            "mcum_record_status": close_result.get("record_status") if isinstance(close_result, dict) else None,
            "multi_agent_mode": (multi_agent_plan or {}).get("mode"),
        })
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

    _safe_print(f"mcum_log_id={log_id}")
    _safe_print(f"mcum_session_id={ctx.session_id}")
    _safe_print(f"exit_code={return_code}")
    if preflight_blocked:
        _safe_print("preflight_blocked=playwright")
    if stdout_tail:
        _safe_print(f"stdout_tail={stdout_tail}")
    if stderr_tail:
        _safe_print(f"stderr_tail={stderr_tail}")
    return int(return_code) if return_code is not None else 1


def _record_only(args: argparse.Namespace) -> int:
    _validated_experience_category(args)
    _ensure_runtime_trace_files(args.project_path)
    program_path = _write_program_file(args)
    if str(getattr(args, "execution_profile", None) or "auto").strip().lower() == "auto":
        args.execution_profile = "fast"
    task_brief = _resolve_task_brief(args)
    task_id = _trace_task_id(args)
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
        multi_agent_plan = _multi_agent_plan(args, task_brief, selected_skill=ctx.skill_selected)
        artifacts = _auto_artifacts(
            args,
            base_dir=args.project_path,
            task_id=task_id,
            runtime_payload={
                "task_id": task_id,
                "mode": "record",
                "project_path": args.project_path,
                "project_name": args.project_name,
                "task": args.task,
                "summary": args.summary,
                "outcome": args.outcome,
                "confidence": args.confidence,
                "validation_summary": args.validation_summary,
                "error_description": args.error_description,
                "multi_agent_plan": multi_agent_plan,
            },
            program_path=program_path,
        )
        final_skill, delegated_skills, correction_source = _task_skill_payload(args, ctx.skill_selected)
        playbook_data = {
            "title": args.experience_title or args.task[:120],
            "objective": args.objective or args.task,
            "files_touched": _playbook_files_touched(args.project_path, artifacts),
            "reusable_when": args.success_criteria or args.validation_required,
            "program_path": program_path,
            "primary_metric": getattr(args, "primary_metric", None),
            "metric_before": getattr(args, "metric_baseline", None),
            "metric_after": getattr(args, "metric_after", None),
            "decision": getattr(args, "decision", None),
            "editable_scope": getattr(args, "editable_scope", None),
            "read_only_scope": getattr(args, "read_only_scope", None),
            "protected_scope": getattr(args, "protected_scope", None),
        }
        close_result = session.close(
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
                playbook_data=playbook_data,
                skills_orchestrated=delegated_skills,
                correction_source=correction_source,
                extra_metadata={"multi_agent_plan": multi_agent_plan} if multi_agent_plan else {},
            )
        )
        log_id = close_result.get("log_id") if isinstance(close_result, dict) else close_result
        _mark_spec_contract_from_result(
            task_brief,
            log_id=log_id,
            outcome=args.outcome,
            summary=args.summary,
            validation_summary=args.validation_summary,
            artifacts=artifacts,
        )
        _maybe_launch_opportunistic_daily_guard(
            args,
            task_brief,
            mode="record",
            task_log_id=log_id,
            outcome=args.outcome,
        )
        _append_runtime_trace(args.project_path, {
            "timestamp": _now_iso(),
            "task_id": _trace_task_id(args),
            "project": str(getattr(args, "project_name", "") or Path(args.project_path).name),
            "scope": str(getattr(args, "editable_scope", None) or args.project_path),
            "status": getattr(args, "decision", None) or args.outcome,
            "metric_name": str(getattr(args, "primary_metric", None) or "manual_record"),
            "metric_before": str(getattr(args, "metric_baseline", None) or ""),
            "metric_after": str(getattr(args, "metric_after", None) or ""),
            "validation": str(args.validation_summary or "manual record"),
            "decision": str(getattr(args, "decision", None) or args.outcome),
            "summary": args.summary,
            "artifacts": ",".join(_existing_paths(artifacts) + ([program_path] if program_path else [])),
            "mcum_log_id": log_id,
            "mcum_session_id": close_result.get("session_id") if isinstance(close_result, dict) else ctx.session_id,
            "mcum_record_status": close_result.get("record_status") if isinstance(close_result, dict) else None,
            "multi_agent_mode": (multi_agent_plan or {}).get("mode"),
        })
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

    _safe_print(f"mcum_log_id={log_id}")
    _safe_print(f"mcum_session_id={ctx.session_id}")
    return 0


def _intake_only(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "The intake subcommand requires a TTY. "
            "For non-interactive use, provide --project-path and --task to another MCUM command."
        )
    args.interactive_intake = True
    if not getattr(args, "task", None):
        args.task = _prompt_text("Resumen breve de la tarea", required=True)
    if not getattr(args, "project_path", None):
        args.project_path = _prompt_text("Ruta del proyecto", required=True)

    brief = _resolve_task_brief(args)
    _safe_print("\nConfirmed Task Brief")
    _safe_print(json.dumps(brief, ensure_ascii=False, indent=2))
    return 0


def _run_frontend_qa(args: argparse.Namespace) -> int:
    _ensure_runtime_trace_files(args.project_path)
    task_brief = _resolve_task_brief(args)
    task_id = _trace_task_id(args)
    headless = not bool(getattr(args, "headed", False))
    plan = build_frontend_qa_plan(
        args.project_path,
        base_url=getattr(args, "base_url", None),
        target_agent=getattr(args, "target_agent", "generic"),
        execution_policy=load_execution_policy(),
        qa_profile=getattr(args, "qa_profile", None),
        task_text=args.task,
        headless=headless,
        browser=getattr(args, "browser", None),
    )
    config_path = None
    if bool(getattr(args, "write_config", True)):
        config_path = write_frontend_qa_config(plan)
        args.artifact = _merge_artifact_paths(getattr(args, "artifact", []) or [], [config_path])

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
        runtime_payload = {
            "task_id": task_id,
            "mode": "frontend_qa",
            "project_path": args.project_path,
            "project_name": args.project_name,
            "task": args.task,
            "frontend_qa_plan": plan,
            "config_path": config_path,
        }
        artifacts = _auto_artifacts(
            args,
            base_dir=args.project_path,
            task_id=task_id,
            runtime_payload=runtime_payload,
        )
        detection = dict(plan.get("detection") or {})
        preflight = dict(plan.get("preflight") or {})
        status = str(plan.get("status") or "unknown")
        outcome = "success" if status == "ready" else "partial"
        summary = (
            f"Prepared Playwright MCP frontend QA plan for {plan.get('base_url')} "
            f"profile={plan.get('qa_profile')}; status={status}; framework={detection.get('framework')}; "
            f"execution_readiness={plan.get('execution_readiness')}; "
            f"config={config_path or 'not_written'}."
        )
        validation_summary = (
            f"Playwright MCP config {'written' if config_path else 'not written'}; "
            f"frontend_found={bool(detection.get('found'))}; "
            f"profile={plan.get('qa_profile')}; "
            f"checks={len(plan.get('checks') or [])}; "
            f"preflight={preflight.get('status')}; "
            f"target_agent={plan.get('target_agent')}"
        )
        close_result = session.close(
            TaskResult(
                task_description=args.task,
                skill_used=ctx.skill_selected,
                outcome=outcome,
                confidence_score=0.9 if outcome == "success" else 0.72,
                output_summary=summary,
                artifacts=artifacts,
                validation_summary=validation_summary,
                playbook_data={
                    "title": args.task[:120],
                    "objective": args.objective or args.task,
                    "commands": [
                        f"configure MCP profile={plan.get('qa_profile')}: "
                        + " ".join(
                            [
                                str((plan.get("mcp_config") or {}).get("mcpServers", {}).get("playwright", {}).get("command") or "npx"),
                                *[
                                    str(item)
                                    for item in list(
                                        (plan.get("mcp_config") or {})
                                        .get("mcpServers", {})
                                        .get("playwright", {})
                                        .get("args")
                                        or []
                                    )
                                ],
                            ]
                        ),
                    ],
                    "files_touched": _playbook_files_touched(args.project_path, artifacts),
                    "reusable_when": "Frontend project requires AI-assisted browser QA through MCP.",
                    "output_summary": plan.get("qa_prompt"),
                    "validation_summary": validation_summary,
                },
                extra_metadata={
                    "frontend_qa": True,
                    "frontend_qa_plan": plan,
                    "config_path": config_path,
                },
            )
        )
        log_id = close_result.get("log_id") if isinstance(close_result, dict) else close_result
        _mark_spec_contract_from_result(
            task_brief,
            log_id=log_id,
            outcome=outcome,
            summary=summary,
            validation_summary=validation_summary,
            artifacts=artifacts,
        )
        _maybe_launch_opportunistic_daily_guard(
            args,
            task_brief,
            mode="frontend-qa",
            task_log_id=log_id,
            outcome=outcome,
        )
        _append_runtime_trace(
            args.project_path,
            {
                "timestamp": _now_iso(),
                "task_id": task_id,
                "project": str(getattr(args, "project_name", "") or Path(args.project_path).name),
                "scope": args.project_path,
                "status": outcome,
                "metric_name": "frontend_qa_checks",
                "metric_before": str(getattr(args, "qa_profile", "auto")),
                "metric_after": str(len(plan.get("checks") or [])),
                "validation": validation_summary,
                "decision": f"{'keep' if outcome == 'success' else 'partial'}:{plan.get('qa_profile')}",
                "summary": summary,
                "artifacts": ",".join(_existing_paths(artifacts)),
                "mcum_log_id": log_id,
                "mcum_session_id": close_result.get("session_id") if isinstance(close_result, dict) else ctx.session_id,
                "mcum_record_status": close_result.get("record_status") if isinstance(close_result, dict) else None,
            },
        )
        session_closed = True
    except Exception as exc:
        if session_started and not session_closed:
            _abort_active_session(
                session,
                error_description=str(exc),
                output_summary="Frontend QA MCP plan aborted before completion.",
                validation_summary="frontend_qa exception",
            )
        raise

    _safe_print(f"mcum_log_id={log_id}")
    _safe_print(f"mcum_session_id={ctx.session_id}")
    _safe_print(
        json.dumps(
            {
                "status": status,
                "qa_profile": plan.get("qa_profile"),
                "profile_reason": plan.get("profile_reason"),
                "execution_readiness": plan.get("execution_readiness"),
                "preflight_missing": (plan.get("preflight") or {}).get("missing"),
                "base_url": plan.get("base_url"),
                "framework": detection.get("framework"),
                "config_path": config_path,
                "output_dir": plan.get("output_dir"),
                "checks": plan.get("checks"),
                "target_agent": plan.get("target_agent"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
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
    _safe_print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
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
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
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

    _safe_print(json.dumps({"results": results}, ensure_ascii=False, indent=2, default=str))
    return 0


def _record_or_update_maintenance_run(args: argparse.Namespace, **payload: Any) -> str:
    queued_run_id = str(getattr(args, "queued_run_id", "") or "").strip()
    if queued_run_id:
        updated = update_maintenance_run(
            maintenance_run_id=queued_run_id,
            status=str(payload.get("status") or "success"),
            trigger_reason=payload.get("trigger_reason"),
            finished_at=payload.get("finished_at"),
            last_seen_activity_at=payload.get("last_seen_activity_at"),
            metrics_snapshot=payload.get("metrics_snapshot"),
            findings=payload.get("findings"),
            actions_applied=payload.get("actions_applied"),
            tokens_estimated=payload.get("tokens_estimated"),
            notes=payload.get("notes"),
        )
        if updated:
            return queued_run_id
        LOGGER.warning("Queued maintenance run id not found; inserting a new maintenance row: %s", queued_run_id)
    return record_maintenance_run(**payload)


def _run_maintenance_cycle(args: argparse.Namespace) -> int:
    maintenance_policy = load_maintenance_policy()
    if getattr(args, "window_hours", None):
        maintenance_policy["delta_window_hours"] = int(args.window_hours)
    if getattr(args, "snapshot_window_days", None):
        maintenance_policy["snapshot_window_days"] = int(args.snapshot_window_days)

    args.project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    args.project_name = getattr(args, "project_name", None) or "MCUM"
    args.task = getattr(args, "task", None) or "Ejecutar maintenance cycle delta-driven de MCUM."
    args.task_type = getattr(args, "task_type", None) or "automatizar"
    args.objective = getattr(args, "objective", None) or (
        "Analizar la base de datos y aplicar solo mejoras seguras cuando exista señal nueva."
    )
    args.expected_deliverable = getattr(args, "expected_deliverable", None) or (
        "Reporte de mantenimiento con acciones ejecutadas o skip justificado."
    )
    args.success_criteria = getattr(args, "success_criteria", None) or (
        "El ciclo evita gasto sin novedad y, si hay señal nueva, refresca KPI y aplica acciones seguras."
    )
    args.execution_mode = getattr(args, "execution_mode", None) or "ejecutar"
    args.validation_required = getattr(args, "validation_required", None) or (
        "maintenance_run persistido y evidencia de KPI/artifacts cuando corresponda."
    )
    args.force_skill = "mcum-orchestrator"
    args.final_skill = "mcum-orchestrator"

    maintenance_name = str(
        getattr(args, "maintenance_name", None)
        or maintenance_policy.get("maintenance_name")
        or "daily_guard"
    ).strip() or "daily_guard"
    project = get_or_create_project(args.project_path, project_name=args.project_name)
    operational_closure: dict[str, Any] = {}
    try:
        operational_closure["maintenance_reaper"] = reap_stale_maintenance_runs(
            max_age_minutes=int(
                (maintenance_policy.get("operational_closure") or {}).get(
                    "stale_maintenance_minutes",
                    30,
                )
            )
        )
    except Exception as exc:
        operational_closure["maintenance_reaper"] = {
            "status": "failure",
            "error": str(exc),
        }
    try:
        from MCUM.core.connector_health import sync_local_connector_health

        operational_closure["connector_health"] = sync_local_connector_health(
            project_id=str(project["id"]),
        )
    except Exception as exc:
        operational_closure["connector_health"] = {
            "status": "failure",
            "error": str(exc),
        }
    latest_run = get_latest_maintenance_run(project_id=project["id"], maintenance_name=maintenance_name)
    since_marker = None
    if latest_run:
        since_marker = latest_run.get("finished_at") or latest_run.get("started_at")
    delta = detect_maintenance_delta(
        project_id=project["id"],
        since=since_marker,
        policy=maintenance_policy,
    )
    delta["operational_closure"] = operational_closure
    started_at = datetime.now(timezone.utc)
    started_at_perf = time.perf_counter()
    task_id = _trace_task_id(args)
    trigger_reason = ", ".join(delta.get("reasons") or []) or ("forced_run" if getattr(args, "force", False) else "no_signal")
    min_hours_between_runs = max(0, int(maintenance_policy.get("min_hours_between_runs", 0) or 0))
    cooldown_active = False
    if latest_run and min_hours_between_runs > 0:
        latest_finished = latest_run.get("finished_at") or latest_run.get("started_at")
        if isinstance(latest_finished, datetime):
            latest_finished_utc = (
                latest_finished.replace(tzinfo=timezone.utc)
                if latest_finished.tzinfo is None
                else latest_finished.astimezone(timezone.utc)
            )
            cooldown_active = (started_at - latest_finished_utc).total_seconds() < (min_hours_between_runs * 3600)

    if (
        not getattr(args, "force", False)
        and cooldown_active
        and not bool(delta.get("fresh_signal_present", False))
    ):
        maintenance_run_id = _record_or_update_maintenance_run(
            args,
            project_id=project["id"],
            maintenance_name=maintenance_name,
            scope="project",
            status="skipped",
            trigger_reason="cooldown_active",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            last_seen_activity_at=delta.get("last_activity_at"),
            metrics_snapshot=delta,
            findings={"latest_run": latest_run, "delta": delta},
            actions_applied=[],
            tokens_estimated=0,
            notes=f"Skipped because maintenance cooldown ({min_hours_between_runs}h) is still active and there is no fresh signal.",
        )
        _safe_print(f"mcum_maintenance_run_id={maintenance_run_id}")
        _safe_print(
            json.dumps(
                {
                    "status": "skipped",
                    "maintenance_name": maintenance_name,
                    "project_id": project["id"],
                    "reason": "cooldown_active",
                    "fresh_signal_present": delta.get("fresh_signal_present", False),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if not getattr(args, "force", False) and not delta.get("should_run", False):
        maintenance_run_id = _record_or_update_maintenance_run(
            args,
            project_id=project["id"],
            maintenance_name=maintenance_name,
            scope="project",
            status="skipped",
            trigger_reason=trigger_reason,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            last_seen_activity_at=delta.get("last_activity_at"),
            metrics_snapshot=delta,
            findings={"latest_run": latest_run, "delta": delta},
            actions_applied=[],
            tokens_estimated=0,
            notes="Skipped because no fresh signal or stale KPI required maintenance actions.",
        )
        _safe_print(f"mcum_maintenance_run_id={maintenance_run_id}")
        _safe_print(
            json.dumps(
                {
                    "status": "skipped",
                    "maintenance_name": maintenance_name,
                    "project_id": project["id"],
                    "reasons": delta.get("reasons", []),
                    "recommended_actions": delta.get("recommended_actions", []),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    actions: list[dict] = []

    try:
        task_brief = _resolve_task_brief(args)
        force_requested = bool(getattr(args, "force", False))
        self_heal_plan = _classify_maintenance_actions(
            delta,
            maintenance_policy,
            latest_run=latest_run,
            force_requested=force_requested,
        )
        action_labels: list[str] = []
        action_failures = 0

        for action_name in self_heal_plan.get("safe_actions", []):
            if action_name == "refresh_daily_metrics" and getattr(args, "skip_metrics_refresh", False):
                continue
            if action_name == "snapshot_project_kpis" and getattr(args, "skip_kpi_snapshot", False):
                continue
            if action_name == "run_skill_factory" and getattr(args, "skip_skill_factory", False):
                continue

            action_outcome = _execute_maintenance_action(
                action_name=action_name,
                project_id=project["id"],
                maintenance_policy=maintenance_policy,
                args=args,
            )
            actions.append(action_outcome)
            if action_outcome.get("status") == "success":
                if action_name == "refresh_daily_metrics":
                    result = action_outcome.get("result", {})
                    action_labels.append(
                        "refresh_daily_metrics"
                        f"(rows={result.get('rows_refreshed', 0)}, latest_day={result.get('latest_day')})"
                    )
                elif action_name == "snapshot_project_kpis":
                    result = action_outcome.get("result", {})
                    action_labels.append(
                        "snapshot_project_kpis"
                        f"(count={result.get('rows_snapshot', 0)}, latest_day={result.get('latest_snapshot_date')})"
                    )
                elif action_name == "audit_memory_governance":
                    result = action_outcome.get("result", {})
                    action_labels.append(
                        "audit_memory_governance"
                        f"(severity={result.get('severity')}, contamination={result.get('contamination_score')}, reasons={len(result.get('reasons', []))})"
                    )
                elif action_name == "consolidate_duplicate_experiences":
                    result = action_outcome.get("result", {})
                    action_labels.append(
                        "consolidate_duplicate_experiences"
                        f"(groups={result.get('groups_merged', 0)}, superseded={result.get('experiences_superseded', 0)})"
                    )
                elif action_name == "analyze_pattern_candidates":
                    backlog = (action_outcome.get("findings") or {}).get("activation_backlog", {})
                    action_labels.append(
                        "analyze_pattern_candidates"
                        f"(observed={action_outcome.get('result', {}).get('candidates_observed', 0)}, "
                        f"review_ready={backlog.get('count', 0)}, "
                        f"oldest_age_days={backlog.get('oldest_age_days', 0)})"
                    )
                elif action_name == "run_skill_factory":
                    result = action_outcome.get("result", {})
                    action_labels.append(
                        "run_skill_factory"
                        f"(created={result.get('created', 0)}, promoted={result.get('promoted', 0)}, merged={result.get('candidate_duplicates_merged', 0)}, bootstrap={result.get('catalog_pressure', {}).get('auto_bootstrap_applied')})"
                    )
                elif action_name == "tune_anti_loop_dispatch_bias":
                    result = action_outcome.get("result", {})
                    analysis = result.get("analysis", {})
                    stability_guard = analysis.get("stability_guard", {})
                    action_labels.append(
                        "tune_anti_loop_dispatch_bias"
                        f"(updated={result.get('policy_updated')}, recommendation={analysis.get('recommendation')}, hinted={analysis.get('metrics', {}).get('hinted_tasks', 0)}, guard={stability_guard.get('reason')})"
                    )
                elif action_name == "tune_memory_governor":
                    result = action_outcome.get("result", {})
                    analysis = result.get("analysis", {})
                    stability_guard = analysis.get("stability_guard", {})
                    action_labels.append(
                        "tune_memory_governor"
                        f"(updated={result.get('policy_updated')}, recommendation={analysis.get('recommendation')}, contamination={analysis.get('metrics', {}).get('contamination_score')}, local_filter_rate={analysis.get('metrics', {}).get('governor_local_filter_activation_rate')}, guard={stability_guard.get('reason')})"
                    )
            elif action_outcome.get("status") == "failure":
                action_failures += 1
                rollback_info = action_outcome.get("rollback") or {}
                if rollback_info.get("required"):
                    action_labels.append(
                        f"{action_name}(failed, rollback={rollback_info.get('status', 'manual_review_required')})"
                    )
                else:
                    action_labels.append(f"{action_name}(failed)")
                if not self_heal_plan.get("continue_on_action_error", True):
                    break
            else:
                action_labels.append(f"{action_name}(skipped)")

        if not actions:
            runtime_flag_blocked = bool(
                self_heal_plan.get("safe_actions")
                and all(
                    (
                        action_name == "refresh_daily_metrics" and getattr(args, "skip_metrics_refresh", False)
                    )
                    or (
                        action_name == "snapshot_project_kpis" and getattr(args, "skip_kpi_snapshot", False)
                    )
                    or (
                        action_name == "run_skill_factory" and getattr(args, "skip_skill_factory", False)
                    )
                    for action_name in self_heal_plan.get("safe_actions", [])
                )
            )
            if force_requested and runtime_flag_blocked:
                no_action_reason = "forced_safe_actions_blocked_by_runtime_flags"
            elif force_requested:
                no_action_reason = "forced_no_executable_actions"
            elif not self_heal_plan.get("safe_actions"):
                no_action_reason = "no_safe_actions"
            else:
                no_action_reason = "safe_actions_blocked_by_runtime_flags"
            report_payload = {
                "maintenance_name": maintenance_name,
                "project_id": project["id"],
                "project_name": project.get("project_name"),
                "task_id": task_id,
                "started_at": started_at,
                "latest_run": latest_run,
                "delta": delta,
                "self_heal_plan": self_heal_plan,
                "action_results": actions,
                "maintenance_status": "skipped",
                "force_requested": force_requested,
                "no_action_reason": no_action_reason,
                "audit_summary": {
                    "decision": "forced_report_only" if force_requested else self_heal_plan.get("decision"),
                    "risk_level": self_heal_plan.get("risk_level"),
                    "blocked_actions": self_heal_plan.get("blocked_actions", []),
                    "manual_review_reasons": self_heal_plan.get("manual_review_reasons", []),
                    "repeat_patterns": self_heal_plan.get("repeat_patterns", []),
                    "force_requested": force_requested,
                    "executed_actions": [],
                },
                "policy_version": maintenance_policy.get("version"),
            }
            maintenance_run_id = _record_or_update_maintenance_run(
                args,
                project_id=project["id"],
                maintenance_name=maintenance_name,
                scope="project",
                status="skipped",
                trigger_reason=trigger_reason,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                last_seen_activity_at=delta.get("last_activity_at"),
                metrics_snapshot=delta,
                findings=report_payload,
                actions_applied=[],
                tokens_estimated=estimate_tokens(report_payload),
                notes=(
                    f"Skipped because no safe self-heal actions were executable. "
                    f"Reason: {no_action_reason}. "
                    f"Blocked: {', '.join(self_heal_plan.get('blocked_actions', []) or ['none'])}."
                ),
            )
            _safe_print(f"mcum_maintenance_run_id={maintenance_run_id}")
            _safe_print(
                json.dumps(
                    {
                        "status": "skipped",
                        "maintenance_name": maintenance_name,
                        "project_id": project["id"],
                        "reasons": delta.get("reasons", []),
                        "manual_review_reasons": self_heal_plan.get("manual_review_reasons", []),
                        "blocked_actions": self_heal_plan.get("blocked_actions", []),
                        "force_requested": force_requested,
                        "no_action_reason": no_action_reason,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0

        maintenance_status = "success"
        if not actions:
            maintenance_status = "skipped"
        elif action_failures:
            maintenance_status = "partial" if any(action.get("status") == "success" for action in actions) else "failure"

        report_payload = {
            "maintenance_name": maintenance_name,
            "project_id": project["id"],
            "project_name": project.get("project_name"),
            "task_id": task_id,
            "started_at": started_at,
            "latest_run": latest_run,
            "delta": delta,
            "self_heal_plan": self_heal_plan,
            "action_results": actions,
            "maintenance_status": maintenance_status,
            "force_requested": force_requested,
            "audit_summary": {
                "decision": self_heal_plan.get("decision"),
                "risk_level": self_heal_plan.get("risk_level"),
                "blocked_actions": self_heal_plan.get("blocked_actions", []),
                "manual_review_reasons": self_heal_plan.get("manual_review_reasons", []),
                "repeat_patterns": self_heal_plan.get("repeat_patterns", []),
                "force_requested": force_requested,
                "action_failures": action_failures,
                "executed_actions": [action.get("action") for action in actions],
                "rolled_back_actions": [
                    action.get("action")
                    for action in actions
                    if action.get("rollback", {}).get("required")
                ],
            },
            "policy_version": maintenance_policy.get("version"),
        }
        artifacts = _auto_artifacts(
            args,
            base_dir=args.project_path,
            task_id=task_id,
            runtime_payload=report_payload,
        )
        summary = (
            f"Maintenance cycle {maintenance_name} {maintenance_status}. "
            f"Reasons: {', '.join(delta.get('reasons', []) or ['forced_run'])}. "
            f"Actions: {', '.join(action_labels or ['report_only'])}."
        )
        operational_summary = delta.get("operational_summary", {})
        if operational_summary:
            summary += (
                " Ops: "
                f"success_rate={operational_summary.get('success_rate')}, "
                f"token_efficiency_per_1k={operational_summary.get('token_efficiency_per_1k')}, "
                f"hinted_rate={operational_summary.get('anti_loop_hinted_rate')}, "
                f"governor_local_filter_rate={operational_summary.get('governor_local_filter_activation_rate')}."
            )
        if self_heal_plan.get("blocked_actions"):
            summary += f" Blocked: {', '.join(self_heal_plan['blocked_actions'])}."
        if force_requested:
            summary += " Force requested."
        validation_summary = (
            "Maintenance validation: "
            + "; ".join(action_labels or ["delta analyzed with no safe actions required"])
        )
        if self_heal_plan.get("manual_review_reasons"):
            validation_summary += f"; manual_review_reasons={', '.join(self_heal_plan['manual_review_reasons'])}"
        confidence_score = 0.92 if maintenance_status == "success" else 0.78 if maintenance_status == "partial" else 0.68
        log_id, session_id = _log_maintenance_task(
            project_path=args.project_path,
            project_name=args.project_name,
            task_description=args.task,
            task_brief=task_brief,
            maintenance_name=maintenance_name,
            trigger_reason=trigger_reason,
            summary=summary,
            validation_summary=validation_summary,
            artifacts=artifacts,
            confidence_score=confidence_score,
            outcome=maintenance_status,
            started_at_perf=started_at_perf,
            findings=report_payload,
            actions=actions,
        )
        _mark_spec_contract_from_result(
            task_brief,
            log_id=log_id,
            outcome=maintenance_status,
            summary=summary,
            validation_summary=validation_summary,
            artifacts=artifacts,
        )
        maintenance_run_id = _record_or_update_maintenance_run(
            args,
            project_id=project["id"],
            maintenance_name=maintenance_name,
            scope="project",
            status=maintenance_status,
            trigger_reason=trigger_reason,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            last_seen_activity_at=delta.get("last_activity_at"),
            metrics_snapshot=delta,
            findings=report_payload,
            actions_applied=actions,
            tokens_estimated=estimate_tokens(report_payload),
            notes=summary,
        )
        _safe_print(f"mcum_log_id={log_id}")
        _safe_print(f"mcum_session_id={session_id}")
        _safe_print(f"mcum_maintenance_run_id={maintenance_run_id}")
        _safe_print(
            json.dumps(
                {
                    "status": maintenance_status,
                    "maintenance_name": maintenance_name,
                    "project_id": project["id"],
                    "reasons": delta.get("reasons", []),
                    "operational_summary": delta.get("operational_summary", {}),
                    "anti_loop_dispatch_audit": delta.get("anti_loop_dispatch_audit", {}),
                    "actions": actions,
                    "blocked_actions": self_heal_plan.get("blocked_actions", []),
                    "force_requested": force_requested,
                    "audit_summary": report_payload.get("audit_summary", {}),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    except Exception as exc:
        _record_or_update_maintenance_run(
            args,
            project_id=project["id"],
            maintenance_name=maintenance_name,
            scope="project",
            status="failure",
            trigger_reason=trigger_reason,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            last_seen_activity_at=delta.get("last_activity_at"),
            metrics_snapshot=delta,
            findings={
                "latest_run": latest_run,
                "delta": delta,
                "error": str(exc),
                "audit_summary": {
                    "decision": self_heal_plan.get("decision") if "self_heal_plan" in locals() else None,
                    "force_requested": bool(getattr(args, "force", False)),
                    "rollback": "manual_review_required",
                    "blocked_actions": self_heal_plan.get("blocked_actions", []) if "self_heal_plan" in locals() else [],
                    "manual_review_reasons": self_heal_plan.get("manual_review_reasons", [])
                    if "self_heal_plan" in locals()
                    else [],
                },
            },
            actions_applied=actions,
            tokens_estimated=estimate_tokens({"delta": delta, "actions": actions, "error": str(exc)}),
            notes=f"Maintenance cycle failed: {exc}",
        )
        raise


def _run_health(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    execution_policy = load_execution_policy()
    since_days = max(1, int(getattr(args, "since_days", 7) or 7))
    agent_invocations: dict[str, Any] = {"available": False}
    code_graph_status: dict[str, Any] = {"available": False}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE log_type = 'task') AS tasks,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'success') AS successes,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'partial') AS partials,
                    COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'failure') AS failures,
                    ROUND(AVG(confidence_score)::numeric, 4) AS avg_confidence,
                    COALESCE(SUM(context_tokens_in), 0) AS context_tokens_in,
                    COALESCE(SUM(context_tokens_out), 0) AS context_tokens_out,
                    COALESCE(ROUND(AVG(retrieval_latency_ms)::numeric, 2), 0) AS avg_retrieval_latency_ms,
                    MAX(created_at) AS last_log_at
                FROM project_registry.project_logs
                WHERE project_id = %s
                  AND created_at >= NOW() - (%s || ' days')::interval
                """,
                (project["id"], since_days),
            )
            logs = cur.fetchone() or {}

            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM project_registry.spec_contracts
                WHERE project_id = %s
                GROUP BY status
                ORDER BY status
                """,
                (project["id"],),
            )
            spec_counts = {row["status"]: row["count"] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM project_registry.maintenance_runs
                WHERE project_id = %s
                  AND maintenance_name = 'daily_guard'
                  AND started_at::date = CURRENT_DATE
                GROUP BY status
                ORDER BY status
                """,
                (project["id"],),
            )
            today_guard = {row["status"]: row["count"] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT id, status, trigger_reason, started_at, finished_at, notes
                FROM project_registry.maintenance_runs
                WHERE project_id = %s AND maintenance_name = 'daily_guard'
                ORDER BY COALESCE(finished_at, started_at) DESC, created_at DESC
                LIMIT 1
                """,
                (project["id"],),
            )
            latest_guard = cur.fetchone()

            cur.execute("SELECT to_regclass('project_registry.agent_invocations') AS table_name")
            if (cur.fetchone() or {}).get("table_name"):
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS calls,
                        COALESCE(SUM(input_tokens), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(total_tokens), 0) AS total_tokens,
                        COALESCE(ROUND(AVG(wall_clock_ms)::numeric, 2), 0) AS avg_wall_clock_ms
                    FROM project_registry.agent_invocations
                    WHERE project_id = %s
                      AND created_at >= NOW() - (%s || ' days')::interval
                    """,
                    (project["id"], since_days),
                )
                agent_usage = cur.fetchone() or {}
                cur.execute(
                    """
                    SELECT runner, provider, model, COUNT(*) AS calls
                    FROM project_registry.agent_invocations
                    WHERE project_id = %s
                      AND created_at >= NOW() - (%s || ' days')::interval
                    GROUP BY runner, provider, model
                    ORDER BY calls DESC, runner, model
                    LIMIT 8
                    """,
                    (project["id"], since_days),
                )
                agent_invocations = {
                    "available": True,
                    "calls": int(agent_usage.get("calls") or 0),
                    "input_tokens": int(agent_usage.get("input_tokens") or 0),
                    "output_tokens": int(agent_usage.get("output_tokens") or 0),
                    "total_tokens": int(agent_usage.get("total_tokens") or 0),
                    "avg_wall_clock_ms": float(agent_usage.get("avg_wall_clock_ms") or 0.0),
                    "by_runner": [dict(row) for row in cur.fetchall()],
                }

            cur.execute("SELECT to_regclass('code_graph.graphs') AS table_name")
            if (cur.fetchone() or {}).get("table_name"):
                cur.execute(
                    """
                    SELECT
                        id, status, graph_version, files_indexed, files_skipped,
                        nodes_total, edges_total, tokens_indexed_estimate,
                        tokens_context_saved_estimate, updated_at, finished_at
                    FROM code_graph.graphs
                    WHERE project_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (project["id"],),
                )
                graph_row = cur.fetchone()
                if graph_row:
                    code_graph_status = {"available": True, **dict(graph_row)}
                else:
                    code_graph_status = {"available": True, "status": "not_indexed"}

    tasks = int(logs.get("tasks") or 0)
    successes = int(logs.get("successes") or 0)
    success_rate = round(successes / tasks, 4) if tasks else 0.0
    open_specs = int(spec_counts.get("auto_generated", 0) or 0) + int(spec_counts.get("active", 0) or 0)
    worker_runner_policy = dict(execution_policy.get("worker_runner") or {})
    multi_policy = dict(execution_policy.get("multi_agent") or {})
    recommendations: list[str] = []
    if not today_guard.get("success"):
        recommendations.append("daily_guard has not completed today; next MCUM session should schedule it.")
    if open_specs:
        recommendations.append(f"{open_specs} spec contract(s) remain open; review failed/aborted sessions.")
    if tasks and success_rate < 0.75:
        recommendations.append("Recent success rate is below 75%; run maintenance-cycle or inspect failing tasks.")
    if not bool(worker_runner_policy.get("model_aware_workers_default", False)):
        recommendations.append("model-aware workers are not default; token savings only apply when explicitly enabled.")
    pattern_intelligence = get_pattern_health(project_id=str(project["id"]), candidate_limit=5)
    graph_intelligence = get_unified_graph_health(project_id=str(project["id"]))
    review_ready = int((pattern_intelligence.get("candidates") or {}).get("quality_ready") or 0)
    if review_ready:
        recommendations.append(
            f"{review_ready} pattern candidate(s) satisfy quality gates and await explicit review."
        )

    payload = {
        "project": {
            "id": str(project["id"]),
            "name": project.get("project_name") or project_name,
            "path": project_path,
        },
        "window_days": since_days,
        "logs": {
            "tasks": tasks,
            "successes": successes,
            "partials": int(logs.get("partials") or 0),
            "failures": int(logs.get("failures") or 0),
            "success_rate": success_rate,
            "avg_confidence": float(logs.get("avg_confidence") or 0.0),
            "context_tokens_in": int(logs.get("context_tokens_in") or 0),
            "context_tokens_out": int(logs.get("context_tokens_out") or 0),
            "avg_retrieval_latency_ms": float(logs.get("avg_retrieval_latency_ms") or 0.0),
            "last_log_at": logs.get("last_log_at"),
        },
        "spec_contracts": {
            "counts": spec_counts,
            "open_count": open_specs,
        },
        "daily_guard": {
            "today_counts": today_guard,
            "latest": latest_guard,
        },
        "model_savings": {
            "model_router_enabled": bool((execution_policy.get("model_router") or {}).get("enabled", True)),
            "model_aware_workers_default": bool(worker_runner_policy.get("model_aware_workers_default", False)),
            "default_runner": worker_runner_policy.get("default_runner", "powershell"),
            "model_aware_runner": worker_runner_policy.get("model_aware_runner", "minimax_sdk"),
            "auto_promote_run_when_complex": bool((multi_policy.get("execution") or {}).get("auto_promote_run_when_complex", False)),
        },
        "agent_invocations": agent_invocations,
        "code_graph": code_graph_status,
        "graph_intelligence": graph_intelligence,
        "pattern_intelligence": pattern_intelligence,
        "recommendations": recommendations,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_code_graph_index(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    execution_policy = load_execution_policy()
    code_graph_policy = dict(execution_policy.get("code_graph") or {})
    graph_policy = load_graph_policy()
    excluded_dirs = list(getattr(args, "exclude_dir", None) or code_graph_policy.get("excluded_dirs") or [])
    max_file_bytes = int(getattr(args, "max_file_bytes", None) or code_graph_policy.get("max_file_bytes") or 1_000_000)
    mode = str(getattr(args, "index_mode", None) or "incremental")

    started = time.perf_counter()
    if mode == "incremental":
        sync_result = sync_project_code_graph(
            project_id=str(project["id"]),
            project_path=project_path,
            project_name=project_name,
            trigger="manual_cli",
            policy={
                **code_graph_policy,
                "excluded_dirs": excluded_dirs,
                "max_file_bytes": max_file_bytes,
            },
        )
        if sync_result.get("status") == "failure":
            raise RuntimeError(str(sync_result.get("error") or "Incremental code graph sync failed."))
        index_result = {
            "stats": dict(sync_result.get("scan_stats") or {}),
            "metadata": dict(sync_result.get("metadata") or {}),
            "delta": dict(sync_result.get("delta") or {}),
        }
        persist_result = dict(sync_result)
    else:
        index_result = scan_project_code_graph(
            project_path,
            excluded_dirs=excluded_dirs,
            max_file_bytes=max_file_bytes,
            tree_sitter_enabled=bool(graph_policy.features.tree_sitter),
            tree_sitter_languages=list(graph_policy.priority_languages),
            tree_sitter_max_nodes=int(graph_policy.budgets.analytics.max_nodes),
        )
        persist_result = persist_index_result(
            project_id=str(project["id"]),
            project_path=project_path,
            project_name=project_name,
            mode=mode,
            index_result=index_result,
        )
    wall_clock_ms = int((time.perf_counter() - started) * 1000)
    summary = (
        f"code_graph indexed {persist_result['files_indexed']} files, "
        f"{persist_result['nodes_indexed']} nodes, {persist_result['edges_indexed']} edges."
    )
    log_id = log_entry(
        project_id=str(project["id"]),
        log_type="task",
        title="Code graph indexed",
        description=summary,
        skill_used="mcum-orchestrator",
        outcome="success",
        outcome_details=summary,
        context_tokens_in=0,
        context_tokens_out=0,
        task_wall_clock_ms=wall_clock_ms,
        log_metadata={
            "mode": "code-graph-index",
            "project_path": project_path,
            "index_mode": mode,
            "code_graph": persist_result,
            "stats": index_result.get("stats"),
            "metadata": index_result.get("metadata"),
        },
    )
    payload = {
        "status": str(persist_result.get("status") or "success"),
        "project_id": str(project["id"]),
        "project_path": project_path,
        "project_name": project_name,
        "mcum_log_id": log_id,
        **persist_result,
        "wall_clock_ms": wall_clock_ms,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_ensure_code_graph(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    payload = ensure_code_graph(
        project_path,
        project_name=project_name,
        task_type=getattr(args, "task_type", None),
        force=bool(getattr(args, "force", False)),
        allow_large=bool(getattr(args, "allow_large", False)),
        check_only=bool(getattr(args, "check_only", False)),
        run_unified_sync=not bool(getattr(args, "no_unified_sync", False)),
        max_file_bytes=getattr(args, "max_file_bytes", None),
    )
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_code_graph_query(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    result = retrieve_code_graph_context(
        project_id=str(project["id"]),
        query=str(getattr(args, "query", None) or ""),
        limit=int(getattr(args, "limit", None) or 8),
        depth=int(getattr(args, "depth", None) or 1),
        languages=list(getattr(args, "language", None) or []),
        exclude_languages=list(getattr(args, "exclude_language", None) or []),
        path_prefix=getattr(args, "path_prefix", None),
        node_kinds=list(getattr(args, "node_kind", None) or []),
    )
    payload = {
        "status": "success",
        "project_id": str(project["id"]),
        "project_path": project_path,
        "project_name": project_name,
        **result,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_graph_sync(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    execution_policy = load_execution_policy()
    code_graph = sync_project_code_graph(
        project_id=str(project["id"]),
        project_path=project_path,
        project_name=project_name,
        trigger="graph_sync_cli",
        policy=dict(execution_policy.get("code_graph") or {}),
    )
    result = sync_unified_project_graph(
        project_id=str(project["id"]),
        trigger="graph_sync_cli",
        selected_skill=getattr(args, "selected_skill", None) or "mcum-orchestrator",
        code_graph_sync=(
            {**code_graph, "status": "success", "forced_projection": True}
            if bool(getattr(args, "force_code_projection", False))
            else code_graph
        ),
        metadata={"project_path": project_path, "source": "workspace_session"},
    )
    payload = {
        "project_id": str(project["id"]),
        "project_path": project_path,
        "project_name": project_name,
        "code_graph": code_graph,
        "graph_intelligence": result,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") == "success" else 1


def _run_graph_query(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    result = query_unified_graph(
        project_id=str(project["id"]),
        query=str(getattr(args, "query", None) or ""),
        limit=int(getattr(args, "limit", None) or 12),
        entity_types=list(getattr(args, "entity_type", None) or []),
    )
    _safe_print(
        json.dumps(
            {
                "project_id": str(project["id"]),
                "project_path": project_path,
                "project_name": project_name,
                **result,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def _run_graph_path(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    result = find_unified_graph_path(
        project_id=str(project["id"]),
        source_entity_id=str(args.source_entity_id),
        target_entity_id=str(args.target_entity_id),
        max_depth=int(getattr(args, "max_depth", None) or 4),
    )
    _safe_print(json.dumps({"project_id": str(project["id"]), **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "not_found"} else 1


def _run_graph_health(args: argparse.Namespace) -> int:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    result = get_unified_graph_health(project_id=str(project["id"]))
    _safe_print(
        json.dumps(
            {
                "project_id": str(project["id"]),
                "project_path": project_path,
                "project_name": project_name,
                **result,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def _graph_project(args: argparse.Namespace) -> tuple[str, str, dict[str, Any]]:
    project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
    project_name = getattr(args, "project_name", None) or Path(project_path).name
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    return project_path, project_name, project


def _run_graph_get_node(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    service = get_graph_query_service(policy=load_graph_policy())
    result = service.get_node(
        project_id=str(project["id"]),
        node_ref=str(args.node_ref),
        direction=str(getattr(args, "direction", "both")),
        relation_types=list(getattr(args, "relation_type", None) or []),
        relation_limit=int(getattr(args, "limit", None) or 25),
        relation_offset=int(getattr(args, "offset", None) or 0),
    )
    _safe_print(json.dumps({"project_path": project_path, "project_name": project_name, **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "not_found"} else 1


def _run_graph_neighbors(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    service = get_graph_query_service(policy=load_graph_policy())
    result = service.neighbors(
        project_id=str(project["id"]),
        node_ref=str(args.node_ref),
        direction=str(getattr(args, "direction", "both")),
        depth=int(getattr(args, "depth", None) or 1),
        relation_types=list(getattr(args, "relation_type", None) or []),
        entity_types=list(getattr(args, "entity_type", None) or []),
        limit=int(getattr(args, "limit", None) or 25),
        offset=int(getattr(args, "offset", None) or 0),
        node_budget=int(getattr(args, "node_budget", None) or 250),
    )
    _safe_print(json.dumps({"project_path": project_path, "project_name": project_name, **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "not_found"} else 1


def _run_graph_explain(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    service = get_graph_query_service(policy=load_graph_policy())
    result = service.explain(
        project_id=str(project["id"]),
        node_ref=str(args.node_ref),
        relation_types=list(getattr(args, "relation_type", None) or []),
    )
    _safe_print(json.dumps({"project_path": project_path, "project_name": project_name, **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "not_found"} else 1


def _run_graph_analytics(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    policy = load_graph_policy()
    if not policy.features.analytics and not bool(getattr(args, "force", False)):
        _safe_print(json.dumps({"status": "disabled", "feature": "analytics", "project_path": project_path}, indent=2))
        return 1
    graph = load_project_graph(
        project_id=str(project["id"]),
        max_nodes=int(getattr(args, "max_nodes", None) or policy.budgets.analytics.max_nodes),
        max_edges=int(getattr(args, "max_edges", None) or policy.budgets.analytics.max_nodes * 4),
    )
    result = analyze_graph(
        graph,
        project_id=str(project["id"]),
        snapshot_id=str(graph.get("snapshot_id") or ""),
        seed=int(getattr(args, "seed", None) or 0),
        resolution=float(getattr(args, "resolution", None) or 1.0),
        hub_limit=int(getattr(args, "hub_limit", None) or 20),
        surprise_limit=int(getattr(args, "surprise_limit", None) or 20),
    )
    persistence = persist_analytics_result(result) if bool(getattr(args, "persist", False)) else {"status": "skipped"}
    payload = {
        "project_path": project_path,
        "project_name": project_name,
        "persistence": persistence,
        **result,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_graph_impact(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    policy = load_graph_policy()
    if not policy.features.impact and not bool(getattr(args, "force", False)):
        _safe_print(json.dumps({"status": "disabled", "feature": "impact", "project_path": project_path}, indent=2))
        return 1
    graph = load_project_graph(
        project_id=str(project["id"]),
        max_nodes=int(getattr(args, "max_nodes", None) or policy.budgets.impact.max_nodes),
        max_edges=int(getattr(args, "max_edges", None) or policy.budgets.impact.max_nodes * 4),
    )
    result = analyze_impact(
        graph,
        project_id=str(project["id"]),
        snapshot_id=str(graph.get("snapshot_id") or ""),
        changed_paths=list(getattr(args, "changed_path", None) or []),
        changed_entities=list(getattr(args, "changed_entity", None) or []),
        max_depth=int(getattr(args, "max_depth", None) or policy.budgets.impact.max_depth),
        max_items=int(getattr(args, "max_items", None) or policy.budgets.impact.max_nodes),
        confidence_threshold=float(getattr(args, "confidence_threshold", None) or 0.65),
    )
    persistence = persist_impact_result(result) if bool(getattr(args, "persist", False)) else {"status": "skipped"}
    _safe_print(
        json.dumps(
            {"project_path": project_path, "project_name": project_name, "persistence": persistence, **result},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def _run_graph_export(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    policy = load_graph_policy()
    if not policy.features.exports and not bool(getattr(args, "force", False)):
        _safe_print(json.dumps({"status": "disabled", "feature": "exports", "project_path": project_path}, indent=2))
        return 1
    export_format = str(getattr(args, "format", None) or "json")
    graph = load_project_graph(
        project_id=str(project["id"]),
        max_nodes=int(getattr(args, "max_nodes", None) or policy.budgets.exports.max_nodes),
        max_edges=int(getattr(args, "max_edges", None) or policy.budgets.exports.max_nodes * 3),
    )
    rendered = export_graph(
        graph,
        project_id=str(project["id"]),
        export_format=export_format,
        snapshot_id=str(graph.get("snapshot_id") or ""),
        max_nodes=int(getattr(args, "max_nodes", None) or policy.budgets.exports.max_nodes),
        max_edges=int(getattr(args, "max_edges", None) or policy.budgets.exports.max_nodes * 3),
    )
    extension = {"markdown": "md", "wiki": "md", "mermaid": "mmd"}.get(export_format, export_format)
    output = Path(getattr(args, "output", None) or (Path(project_path) / ".mcum" / "exports" / f"graph.{extension}")).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    _safe_print(
        json.dumps(
            {
                "status": "success",
                "project_id": str(project["id"]),
                "project_path": project_path,
                "project_name": project_name,
                "format": export_format,
                "output": str(output),
                "bytes": output.stat().st_size,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_graph_compare(args: argparse.Namespace) -> int:
    left_path, left_name, left = _graph_project(args)
    right_path = str(Path(args.right_project_path).resolve())
    right_name = getattr(args, "right_project_name", None) or Path(right_path).name
    right = get_or_create_project(project_path=right_path, project_name=right_name)
    policy = load_graph_policy()
    cross_project = str(left["id"]) != str(right["id"])
    if cross_project and (not policy.features.cross_project or not bool(getattr(args, "confirm_cross_project", False))):
        _safe_print(json.dumps({"status": "blocked", "reason": "cross_project_requires_enabled_policy_and_confirmation"}, indent=2))
        return 1
    left_graph = load_project_graph(project_id=str(left["id"]), max_nodes=policy.budgets.cross_project.max_nodes_per_project)
    right_graph = load_project_graph(project_id=str(right["id"]), max_nodes=policy.budgets.cross_project.max_nodes_per_project)
    result = compare_graphs(
        left_graph,
        right_graph,
        left_project_id=str(left["id"]),
        right_project_id=str(right["id"]),
        left_snapshot_id=str(left_graph.get("snapshot_id") or ""),
        right_snapshot_id=str(right_graph.get("snapshot_id") or ""),
    )
    persistence = persist_comparison_result(result) if bool(getattr(args, "persist", False)) else {"status": "skipped"}
    _safe_print(json.dumps({"left_project_path": left_path, "left_project_name": left_name, "right_project_path": right_path, "right_project_name": right_name, "persistence": persistence, **result}, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_artifact_index(args: argparse.Namespace) -> int:
    project_path, project_name, project = _graph_project(args)
    policy = load_graph_policy()
    if not policy.features.multimedia and not bool(getattr(args, "force", False)):
        _safe_print(json.dumps({"status": "disabled", "feature": "multimedia", "project_path": project_path}, indent=2))
        return 1
    extractor = ArtifactExtractor(
        project_path,
        policy=ArtifactPolicy(
            max_bytes=policy.budgets.multimedia.max_file_bytes,
            enable_ocr=bool(getattr(args, "enable_ocr", False)),
            enable_transcription=bool(getattr(args, "enable_transcription", False)),
            budget=ArtifactBudget(
                ocr_chars=int(getattr(args, "ocr_chars", None) or 0),
                transcription_chars=int(getattr(args, "transcription_chars", None) or 0),
            ),
        ),
    )
    result = extractor.extract(str(args.artifact_path)).to_dict()
    persistence = (
        persist_artifact_result(project_id=str(project["id"]), result=result)
        if bool(getattr(args, "persist", False)) and bool(result.get("content_hash"))
        else {"status": "skipped"}
    )
    _safe_print(json.dumps({"project_id": str(project["id"]), "project_path": project_path, "project_name": project_name, "persistence": persistence, **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("content_hash") else 1


def _run_dashboard_serve(args: argparse.Namespace) -> int:
    host = str(getattr(args, "host", None) or "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not bool(getattr(args, "allow_public", False)):
        _safe_print(json.dumps({"status": "blocked", "reason": "non_loopback_requires_allow_public"}, indent=2))
        return 1
    from MCUM.integrations.dashboard.server import create_server

    server = create_server(host=host, port=int(getattr(args, "port", None) or 8765))
    _safe_print(json.dumps({"status": "serving", "url": f"http://{host}:{server.server_port}", "mode": "read-only"}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_pattern_discover(args: argparse.Namespace) -> int:
    project_id = None
    project_path = None
    project_name = None
    if bool(getattr(args, "project_scope", False)):
        project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
        project_name = getattr(args, "project_name", None) or Path(project_path).name
        project = get_or_create_project(project_path=project_path, project_name=project_name)
        project_id = str(project["id"])

    result = run_pattern_discovery(
        project_id=project_id,
        policy=load_pattern_policy(),
        write_candidates=not bool(getattr(args, "no_write", False)),
    )
    payload = {
        "project_id": project_id,
        "project_path": project_path,
        "project_name": project_name,
        **result,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in {"success", "blocked"} else 1


def _run_pattern_health(args: argparse.Namespace) -> int:
    project_id = None
    project_path = None
    if bool(getattr(args, "project_scope", False)):
        project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
        project_name = getattr(args, "project_name", None) or Path(project_path).name
        project = get_or_create_project(project_path=project_path, project_name=project_name)
        project_id = str(project["id"])
    payload = {
        "project_id": project_id,
        "project_path": project_path,
        **get_pattern_health(
            project_id=project_id,
            candidate_limit=int(getattr(args, "limit", 20) or 20),
        ),
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("available") else 1


def _run_pattern_accept(args: argparse.Namespace) -> int:
    if not bool(getattr(args, "confirm", False)):
        _safe_print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "explicit_confirmation_required",
                    "candidate_id": args.candidate_id,
                    "auto_promote": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    try:
        result = materialize_candidate_to_draft(
            candidate_id=str(args.candidate_id),
            reviewed_by=str(getattr(args, "reviewed_by", None) or "manual-review"),
            review_notes=getattr(args, "review_notes", None),
        )
    except ValueError as exc:
        _safe_print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": str(exc),
                    "candidate_id": args.candidate_id,
                    "auto_promote": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    _safe_print(json.dumps({**result, "auto_promote": False}, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_pattern_activate(args: argparse.Namespace) -> int:
    if not bool(getattr(args, "confirm", False)):
        _safe_print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "explicit_confirmation_required",
                    "pattern_id": args.pattern_id,
                    "auto_promote": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    try:
        result = activate_pattern(
            pattern_id=str(args.pattern_id),
            reviewed_by=str(getattr(args, "reviewed_by", None) or "manual-review"),
            review_notes=getattr(args, "review_notes", None),
            quality_gates=dict((load_pattern_policy().get("quality_gates") or {})),
        )
    except ValueError as exc:
        _safe_print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": str(exc),
                    "pattern_id": args.pattern_id,
                    "auto_promote": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    _safe_print(json.dumps({**result, "auto_promote": False}, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_pattern_review(args: argparse.Namespace) -> int:
    """Lista candidatos review-ready para supervision humana.

    No es destructivo. Devuelve:
      - backlog: cuantos candidatos esperan aceptacion, edad maxima
      - candidates: lista detallada de los N mejores
    Cada candidato incluye el comando sugerido para promoverlo.
    """
    project_id = None
    if bool(getattr(args, "project_scope", False)):
        project_path = str(Path(getattr(args, "project_path", None) or SKILL_ROOT).resolve())
        project_name = getattr(args, "project_name", None) or Path(project_path).name
        project = get_or_create_project(project_path=project_path, project_name=project_name)
        project_id = str(project["id"])

    backlog = get_activation_backlog(
        project_id=project_id,
        max_age_days=int(getattr(args, "max_age_days", 90) or 90),
        limit=int(getattr(args, "limit", 20) or 20),
    )
    candidates = list_review_ready_candidates(
        project_id=project_id,
        limit=int(getattr(args, "limit", 20) or 20),
        max_age_days=int(getattr(args, "max_age_days", 90) or 90),
    )
    # Adjuntar comando sugerido por candidato (idempotente, no ejecuta)
    for cand in candidates:
        cand["suggested_command"] = (
            f"workspace_session.py pattern-accept "
            f"--candidate-id {cand['id']} "
            f"--reviewed-by <reviewer> --confirm"
        )
    payload = {
        "backlog": backlog,
        "candidates": candidates,
        "count": int(backlog.get("count") or 0),
        "listed_count": len(candidates),
        "auto_promote": False,
    }
    _safe_print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload["backlog"].get("available", True) else 1


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
        choices=sorted(VALID_CATEGORIES),
        help="Experience category when --save-experience is enabled.",
    )
    common.add_argument("--conclusion", help="Experience conclusion when --save-experience is enabled.")
    common.add_argument("--context", help="Experience context when --save-experience is enabled.")
    common.add_argument("--quiet", action="store_true", help="Reduce MCUM console output.")
    common.add_argument("--no-auto-improve", action="store_true", help="Disable autonomous SISL after the session closes.")
    common.add_argument("--skip-daily-guard", action="store_true", help="Skip the opportunistic Daily Guard scheduler for this session.")
    common.add_argument(
        "--execution-profile",
        choices=["auto", "fast", "lite", "full"],
        default="auto",
        help="Execution budget profile. auto uses lite for analysis/validation/planning and full for write or high-risk tasks.",
    )
    common.add_argument("--spec-interactive", action="store_true", help="Ask guided Spec Contract clarification questions before persisting the spec.")
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
    common.add_argument("--task-id", help="Stable task identifier for runtime traceability.")
    common.add_argument("--primary-metric", help="Primary metric name for the task or experiment.")
    common.add_argument("--metric-baseline", help="Metric baseline before the change.")
    common.add_argument("--metric-target", help="Desired target for the primary metric.")
    common.add_argument("--metric-after", help="Observed metric after the task or iteration.")
    common.add_argument("--editable-scope", help="Editable scope for the task.")
    common.add_argument("--read-only-scope", help="Read-only scope for the task.")
    common.add_argument("--protected-scope", help="Protected scope for the task.")
    common.add_argument("--iteration-budget", type=int, default=5, help="Maximum intended iterations for the task.")
    common.add_argument("--decision-rule", help="Decision rule for keep/discard/crash judgments.")
    common.add_argument("--decision", choices=["keep", "discard", "crash", "partial"], help="Outcome decision for runtime traceability.")
    common.add_argument("--validation-command", help="Validation command used by the task program.")
    common.add_argument("--max-runtime-minutes", type=int, default=30, help="Expected max runtime minutes for the task program.")
    common.add_argument("--emit-program-file", action="store_true", help="Emit PROGRAM_<task_id>.md into .agent/runtime.")
    common.add_argument("--skip-runtime-artifact", action="store_true", help="Skip the automatic per-task runtime JSON artifact.")
    common.add_argument("--supervised-multi-agent", action="store_true", help="Attach a supervised multi-agent contract to this task.")
    common.add_argument("--entrypoint-agent", help="Interactive agent/runtime that asked MCUM to orchestrate this task, e.g. codex, opencode, claude-code, or antigravity.")
    common.add_argument("--emit-multi-agent-plan", action="store_true", help="Emit a supervised multi-agent plan into runtime artifacts.")
    common.add_argument("--auto-multi-run", action="store_true", help="Allow run tasks to auto-promote into supervised multi-run when the task is complex and worker commands are available.")
    common.add_argument(
        "--worker-command",
        action="append",
        default=[],
        help="Worker command in the form role=PowerShell command. Repeat for each worker to execute or for run auto-promotion.",
    )
    common.add_argument("--orchestration-role", choices=["coordinator", "worker"], help="Role of this session inside a supervised multi-agent task.")
    common.add_argument("--worker-role", help="Human-readable worker role when orchestration-role=worker.")
    common.add_argument("--parent-task-id", help="Parent task identifier for a worker session.")
    common.add_argument("--parent-session-id", help="Parent MCUM session identifier for a worker session.")
    common.add_argument("--worker-index", type=int, help="1-based worker index for a supervised task.")
    common.add_argument("--worker-count", type=int, help="Total workers expected for the supervised task.")
    common.add_argument("--max-workers", type=int, help="Override the recommended worker count for multi-agent planning.")
    common.add_argument(
        "--worker-runner",
        choices=["auto", "powershell", "codex-exec", "gemini-cli", "minimax-sdk", "spreadsheet-extractor"],
        default="auto",
        help="Worker execution adapter. 'minimax-sdk' launches the default MCUM-governed MiniMax worker; 'spreadsheet-extractor' emits bounded JSON from .xlsx files.",
    )
    common.add_argument(
        "--model-aware-workers",
        action="store_true",
        help="Run supervised workers through the model-aware runner selected by MCUM policy.",
    )
    common.add_argument(
        "--no-model-aware-workers",
        action="store_true",
        help="Force legacy PowerShell worker execution even if model-aware workers are enabled by policy.",
    )
    common.add_argument("--suppress-autonomy-hooks", action="store_true", help="Skip SISL and skill-factory hooks for this session.")
    common.add_argument("--allow-worker-learning-writes", action="store_true", help="Allow a worker session to persist experience/playbook writes.")

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

    frontend_qa_parser = subparsers.add_parser("frontend-qa", parents=[common], help="Prepare Playwright MCP frontend QA config and test plan under MCUM.")
    frontend_qa_parser.add_argument("--base-url", help="Frontend URL to validate. Defaults by detected framework.")
    frontend_qa_parser.add_argument(
        "--target-agent",
        choices=["codex", "antigravity", "claude-code", "opencode", "generic"],
        default="generic",
        help="Agent/client that will consume the generated MCP configuration.",
    )
    frontend_qa_parser.add_argument("--browser", choices=["chrome", "firefox", "webkit", "msedge"], default=None)
    frontend_qa_parser.add_argument(
        "--qa-profile",
        choices=["auto", "fast", "standard", "strict"],
        default="auto",
        help="QA depth. auto chooses the cheapest useful profile; strict is reserved for final visual validation.",
    )
    frontend_qa_parser.add_argument("--headed", action="store_true", help="Generate MCP config in headed mode instead of headless.")
    frontend_qa_parser.add_argument("--no-write-config", dest="write_config", action="store_false", help="Do not write .mcum/playwright-mcp.json.")
    frontend_qa_parser.set_defaults(
        write_config=True,
        task="Preparar QA frontend con Playwright MCP desde MCUM.",
        task_type="validar",
        objective="Generar configuracion MCP y plan QA frontend reproducible con Playwright.",
        expected_deliverable="Archivo .mcum/playwright-mcp.json, runtime artifact y prompt/checklist QA.",
        success_criteria="MCUM detecta el frontend, genera config MCP segura y deja el plan registrado.",
        execution_mode="proponer",
        risk_level="bajo",
        validation_required="Config MCP y plan QA quedan como artifacts; ejecucion browser se realiza por el agente MCP conectado.",
        no_auto_improve=True,
    )

    multi_plan_parser = subparsers.add_parser("multi-plan", parents=[common], help="Generate a supervised multi-agent plan without executing child work.")
    multi_plan_parser.set_defaults(
        task_type="planificar",
        execution_mode="proponer",
        risk_level="medio",
        objective="Generar un plan multiagente supervisado y seguro para la tarea.",
        expected_deliverable="Plan multiagente con coordinador, workers, budgets y merge policy.",
        success_criteria="El plan define roles, guardrails y writeback seguro sin ejecutar child work.",
        validation_required="El plan queda persistido en runtime artifact y salida JSON.",
        quiet=True,
        no_auto_improve=True,
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
    )

    multi_run_parser = subparsers.add_parser("multi-run", parents=[common], help="Execute a supervised multi-agent workflow with coordinator and worker sessions.")
    multi_run_parser.add_argument("--workdir", help="Working directory for worker commands. Defaults to project-path.")
    multi_run_parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds for each worker command.")
    multi_run_parser.set_defaults(
        task_type="corregir",
        execution_mode="ejecutar",
        risk_level="medio",
        objective="Ejecutar una tarea compleja mediante coordinador y workers supervisados.",
        expected_deliverable="Resultado coordinado con evidencia por worker y validación final.",
        success_criteria="Los workers corren bajo guardrails, hay a lo sumo un writer, y si hay escritura existe validación independiente.",
        validation_required="Cada worker entrega evidencia y el coordinador cierra la tarea con trazabilidad completa.",
        supervised_multi_agent=True,
        emit_multi_agent_plan=True,
        no_auto_improve=True,
    )

    maintenance_parser = subparsers.add_parser("maintenance-cycle", parents=[common], help="Run a delta-driven maintenance cycle for MCUM.")
    maintenance_parser.add_argument("--maintenance-name", default="daily_guard", help="Logical maintenance cycle name used for delta tracking.")
    maintenance_parser.add_argument("--window-hours", type=int, help="Override the delta lookback window when there is no previous maintenance run.")
    maintenance_parser.add_argument("--snapshot-window-days", type=int, help="Override the KPI snapshot aggregation window.")
    maintenance_parser.add_argument("--force", action="store_true", help="Run even if the preflight delta finds no new signal.")
    maintenance_parser.add_argument("--skip-metrics-refresh", action="store_true", help="Do not refresh mv_daily_metrics during this cycle.")
    maintenance_parser.add_argument("--skip-kpi-snapshot", action="store_true", help="Do not create/update project_kpis during this cycle.")
    maintenance_parser.add_argument("--skip-skill-factory", action="store_true", help="Do not run the safe skill-factory cycle during this maintenance pass.")
    maintenance_parser.add_argument("--queued-run-id", help=argparse.SUPPRESS)
    maintenance_parser.set_defaults(
        project_path=str(SKILL_ROOT),
        project_name="MCUM",
        task="Ejecutar maintenance cycle delta-driven de MCUM.",
        task_type="automatizar",
        objective="Analizar la base de datos y aplicar solo mejoras seguras cuando exista señal nueva.",
        expected_deliverable="Reporte de mantenimiento con KPI refrescados, snapshot KPI y acciones seguras aplicadas.",
        success_criteria="El ciclo evita gasto si no hubo novedades y registra acciones verificables cuando detecta señal nueva.",
        execution_mode="ejecutar",
        risk_level="medio",
        validation_required="maintenance_run persistido y evidencia de KPI/artifacts cuando corresponda.",
    )

    health_parser = subparsers.add_parser("health", help="Print MCUM operational health, Spec Contract, Daily Guard and model-savings status.")
    health_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Project path to inspect.")
    health_parser.add_argument("--project-name", default="MCUM", help="Project name override.")
    health_parser.add_argument("--since-days", type=int, default=7, help="Recent log window in days.")

    code_graph_index_parser = subparsers.add_parser("code-graph-index", help="Index a project into the native MCUM PostgreSQL code_graph.")
    code_graph_index_parser.add_argument("--project-path", required=True, help="Project path to index.")
    code_graph_index_parser.add_argument("--project-name", help="Project name override.")
    code_graph_index_parser.add_argument("--index-mode", choices=["full", "incremental"], default="incremental", help="Index only changed files by default, or rebuild the complete graph.")
    code_graph_index_parser.add_argument("--exclude-dir", action="append", default=[], help="Directory name or relative path to exclude. Repeatable.")
    code_graph_index_parser.add_argument("--max-file-bytes", type=int, default=None, help="Skip source files larger than this many bytes.")

    ensure_code_graph_parser = subparsers.add_parser("ensure-code-graph", help="Gated freshness check that auto-indexes the code graph only when missing or stale.")
    ensure_code_graph_parser.add_argument("--project-path", required=True, help="Project path to ensure.")
    ensure_code_graph_parser.add_argument("--project-name", help="Project name override.")
    ensure_code_graph_parser.add_argument("--task-type", help="Task type; non-code task types are skipped.")
    ensure_code_graph_parser.add_argument("--check-only", action="store_true", help="Report freshness only; never index.")
    ensure_code_graph_parser.add_argument("--force", action="store_true", help="Re-index even when the fingerprint matches.")
    ensure_code_graph_parser.add_argument("--allow-large", action="store_true", help="Build inline even when a first build is large (no deferral).")
    ensure_code_graph_parser.add_argument("--no-unified-sync", action="store_true", help="Skip the federated graph projection after indexing.")
    ensure_code_graph_parser.add_argument("--max-file-bytes", type=int, default=None, help="Skip source files larger than this many bytes.")

    code_graph_query_parser = subparsers.add_parser("code-graph-query", help="Query compact context from the native MCUM code_graph.")
    code_graph_query_parser.add_argument("--project-path", required=True, help="Project path registered in MCUM.")
    code_graph_query_parser.add_argument("--project-name", help="Project name override.")
    code_graph_query_parser.add_argument("--query", required=True, help="Code graph search query.")
    code_graph_query_parser.add_argument("--limit", type=int, default=8, help="Maximum nodes to return.")
    code_graph_query_parser.add_argument("--depth", type=int, default=1, help="Reserved dependency depth parameter.")
    code_graph_query_parser.add_argument("--language", action="append", default=[], help="Include a language. Repeatable.")
    code_graph_query_parser.add_argument("--exclude-language", action="append", default=[], help="Exclude a language. Repeatable.")
    code_graph_query_parser.add_argument("--path-prefix", help="Only return nodes under this relative project path.")
    code_graph_query_parser.add_argument("--node-kind", action="append", default=[], help="Include a node kind. Repeatable.")

    graph_sync_parser = subparsers.add_parser("graph-sync", help="Synchronize code and federated MCUM project graph.")
    graph_sync_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Project root. Each explicit path is isolated as one project.")
    graph_sync_parser.add_argument("--project-name", help="Project name override.")
    graph_sync_parser.add_argument("--selected-skill", help="Selected skill to project for this task.")
    graph_sync_parser.add_argument("--force-code-projection", action="store_true", help="Rebuild the federated code projection even after a no-change scan.")

    graph_query_parser = subparsers.add_parser("graph-query", help="Query the federated MCUM project graph.")
    graph_query_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Project root.")
    graph_query_parser.add_argument("--project-name", help="Project name override.")
    graph_query_parser.add_argument("--query", required=True, help="Project graph query.")
    graph_query_parser.add_argument("--limit", type=int, default=12, help="Maximum entities to return.")
    graph_query_parser.add_argument("--entity-type", action="append", default=[], help="Include one entity type. Repeatable.")

    graph_path_parser = subparsers.add_parser("graph-path", help="Find a directed path between two federated graph entities.")
    graph_path_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Project root.")
    graph_path_parser.add_argument("--project-name", help="Project name override.")
    graph_path_parser.add_argument("--source-entity-id", required=True)
    graph_path_parser.add_argument("--target-entity-id", required=True)
    graph_path_parser.add_argument("--max-depth", type=int, default=4)

    graph_health_parser = subparsers.add_parser("graph-health", help="Inspect federated graph health for one project.")
    graph_health_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Project root.")
    graph_health_parser.add_argument("--project-name", help="Project name override.")

    graph_get_node_parser = subparsers.add_parser("graph-get-node", help="Get one project-scoped graph entity and direct relations.")
    graph_get_node_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_get_node_parser.add_argument("--project-name")
    graph_get_node_parser.add_argument("--node-ref", required=True, help="Entity UUID or canonical key.")
    graph_get_node_parser.add_argument("--direction", choices=["in", "out", "both"], default="both")
    graph_get_node_parser.add_argument("--relation-type", action="append", default=[])
    graph_get_node_parser.add_argument("--limit", type=int, default=25)
    graph_get_node_parser.add_argument("--offset", type=int, default=0)

    graph_neighbors_parser = subparsers.add_parser("graph-neighbors", help="Traverse bounded project-scoped graph neighbors.")
    graph_neighbors_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_neighbors_parser.add_argument("--project-name")
    graph_neighbors_parser.add_argument("--node-ref", required=True)
    graph_neighbors_parser.add_argument("--direction", choices=["in", "out", "both"], default="both")
    graph_neighbors_parser.add_argument("--depth", type=int, default=1)
    graph_neighbors_parser.add_argument("--relation-type", action="append", default=[])
    graph_neighbors_parser.add_argument("--entity-type", action="append", default=[])
    graph_neighbors_parser.add_argument("--limit", type=int, default=25)
    graph_neighbors_parser.add_argument("--offset", type=int, default=0)
    graph_neighbors_parser.add_argument("--node-budget", type=int, default=250)

    graph_explain_parser = subparsers.add_parser("graph-explain", help="Explain one graph entity from deterministic evidence.")
    graph_explain_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_explain_parser.add_argument("--project-name")
    graph_explain_parser.add_argument("--node-ref", required=True)
    graph_explain_parser.add_argument("--relation-type", action="append", default=[])

    graph_analytics_parser = subparsers.add_parser("graph-analytics", help="Compute communities, hubs and surprising connections.")
    graph_analytics_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_analytics_parser.add_argument("--project-name")
    graph_analytics_parser.add_argument("--max-nodes", type=int)
    graph_analytics_parser.add_argument("--max-edges", type=int)
    graph_analytics_parser.add_argument("--seed", type=int, default=0)
    graph_analytics_parser.add_argument("--resolution", type=float, default=1.0)
    graph_analytics_parser.add_argument("--hub-limit", type=int, default=20)
    graph_analytics_parser.add_argument("--surprise-limit", type=int, default=20)
    graph_analytics_parser.add_argument("--persist", action="store_true")
    graph_analytics_parser.add_argument("--force", action="store_true", help="Run even when the feature flag is disabled.")

    graph_impact_parser = subparsers.add_parser("graph-impact", help="Analyze change impact and select tests conservatively.")
    graph_impact_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_impact_parser.add_argument("--project-name")
    graph_impact_parser.add_argument("--changed-path", action="append", default=[])
    graph_impact_parser.add_argument("--changed-entity", action="append", default=[])
    graph_impact_parser.add_argument("--max-depth", type=int)
    graph_impact_parser.add_argument("--max-items", type=int)
    graph_impact_parser.add_argument("--max-nodes", type=int)
    graph_impact_parser.add_argument("--max-edges", type=int)
    graph_impact_parser.add_argument("--confidence-threshold", type=float, default=0.65)
    graph_impact_parser.add_argument("--persist", action="store_true")
    graph_impact_parser.add_argument("--force", action="store_true")

    graph_export_parser = subparsers.add_parser("graph-export", help="Export a governed project graph.")
    graph_export_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    graph_export_parser.add_argument("--project-name")
    graph_export_parser.add_argument("--format", choices=["json", "ndjson", "markdown", "wiki", "mermaid", "html"], default="json")
    graph_export_parser.add_argument("--output")
    graph_export_parser.add_argument("--max-nodes", type=int)
    graph_export_parser.add_argument("--max-edges", type=int)
    graph_export_parser.add_argument("--force", action="store_true")

    graph_compare_parser = subparsers.add_parser("graph-compare", help="Compare two explicit project graphs.")
    graph_compare_parser.add_argument("--project-path", default=str(SKILL_ROOT), help="Left project root.")
    graph_compare_parser.add_argument("--project-name", help="Left project name.")
    graph_compare_parser.add_argument("--right-project-path", required=True)
    graph_compare_parser.add_argument("--right-project-name")
    graph_compare_parser.add_argument("--confirm-cross-project", action="store_true")
    graph_compare_parser.add_argument("--persist", action="store_true")

    artifact_index_parser = subparsers.add_parser("artifact-index", help="Extract and optionally persist one governed project artifact.")
    artifact_index_parser.add_argument("--project-path", default=str(SKILL_ROOT))
    artifact_index_parser.add_argument("--project-name")
    artifact_index_parser.add_argument("--artifact-path", required=True)
    artifact_index_parser.add_argument("--enable-ocr", action="store_true")
    artifact_index_parser.add_argument("--enable-transcription", action="store_true")
    artifact_index_parser.add_argument("--ocr-chars", type=int, default=0)
    artifact_index_parser.add_argument("--transcription-chars", type=int, default=0)
    artifact_index_parser.add_argument("--persist", action="store_true")
    artifact_index_parser.add_argument("--force", action="store_true")

    dashboard_serve_parser = subparsers.add_parser("dashboard-serve", help="Serve the local read-only connectors dashboard.")
    dashboard_serve_parser.add_argument("--host", default="127.0.0.1")
    dashboard_serve_parser.add_argument("--port", type=int, default=8765)
    dashboard_serve_parser.add_argument("--allow-public", action="store_true", help="Required for non-loopback binding.")

    pattern_discover_parser = subparsers.add_parser(
        "pattern-discover",
        help="Analyze operational experiences and stage governed pattern candidates in shadow mode.",
    )
    pattern_discover_parser.add_argument("--project-scope", action="store_true", help="Limit discovery to one registered project.")
    pattern_discover_parser.add_argument("--project-path", help="Project path used only with --project-scope.")
    pattern_discover_parser.add_argument("--project-name", help="Optional project name override.")
    pattern_discover_parser.add_argument("--no-write", action="store_true", help="Analyze without persisting candidates or embeddings.")

    pattern_health_parser = subparsers.add_parser(
        "pattern-health",
        help="Print Pattern Intelligence candidates, lifecycle and utility health.",
    )
    pattern_health_parser.add_argument("--project-scope", action="store_true", help="Limit health output to one registered project.")
    pattern_health_parser.add_argument("--project-path", help="Project path used only with --project-scope.")
    pattern_health_parser.add_argument("--project-name", help="Optional project name override.")
    pattern_health_parser.add_argument("--limit", type=int, default=20, help="Maximum candidate rows to include.")

    pattern_accept_parser = subparsers.add_parser(
        "pattern-accept",
        help="Materialize one quality-ready candidate as a draft after explicit human review.",
    )
    pattern_accept_parser.add_argument("--candidate-id", required=True, help="Candidate UUID to materialize.")
    pattern_accept_parser.add_argument("--reviewed-by", required=True, help="Reviewer identity stored in the audit trail.")
    pattern_accept_parser.add_argument("--review-notes", help="Optional reviewer notes.")
    pattern_accept_parser.add_argument("--confirm", action="store_true", help="Required explicit confirmation; creates a draft, never an active pattern.")

    pattern_activate_parser = subparsers.add_parser(
        "pattern-activate",
        help="Activate one reviewed draft only when every quality gate still passes.",
    )
    pattern_activate_parser.add_argument("--pattern-id", required=True, help="Draft pattern UUID to activate.")
    pattern_activate_parser.add_argument("--reviewed-by", required=True, help="Reviewer identity stored in the audit trail.")
    pattern_activate_parser.add_argument("--review-notes", help="Optional activation review notes.")
    pattern_activate_parser.add_argument("--confirm", action="store_true", help="Required explicit confirmation; activation is never automatic.")

    pattern_review_parser = subparsers.add_parser(
        "pattern-review",
        help="List quality-ready candidates awaiting human review (non-destructive).",
    )
    pattern_review_parser.add_argument("--project-scope", action="store_true", help="Limit backlog to one registered project.")
    pattern_review_parser.add_argument("--project-path", help="Project path used only with --project-scope.")
    pattern_review_parser.add_argument("--project-name", help="Optional project name override.")
    pattern_review_parser.add_argument("--limit", type=int, default=20, help="Maximum candidates to list (default 20, max 100).")
    pattern_review_parser.add_argument("--max-age-days", type=int, default=90, help="Ignore candidates not seen in the last N days.")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    quiet = bool(getattr(args, "quiet", False))
    configure_logging(logging.WARNING if quiet else None, force=True)
    _configure_console_streams()

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
    if args.mode == "frontend-qa":
        return _run_frontend_qa(args)
    if args.mode == "multi-plan":
        return _run_multi_plan(args)
    if args.mode == "multi-run":
        return _run_multi_execution(args)
    if args.mode == "maintenance-cycle":
        return _run_maintenance_cycle(args)
    if args.mode == "health":
        return _run_health(args)
    if args.mode == "code-graph-index":
        return _run_code_graph_index(args)
    if args.mode == "ensure-code-graph":
        return _run_ensure_code_graph(args)
    if args.mode == "code-graph-query":
        return _run_code_graph_query(args)
    if args.mode == "graph-sync":
        return _run_graph_sync(args)
    if args.mode == "graph-query":
        return _run_graph_query(args)
    if args.mode == "graph-path":
        return _run_graph_path(args)
    if args.mode == "graph-health":
        return _run_graph_health(args)
    if args.mode == "graph-get-node":
        return _run_graph_get_node(args)
    if args.mode == "graph-neighbors":
        return _run_graph_neighbors(args)
    if args.mode == "graph-explain":
        return _run_graph_explain(args)
    if args.mode == "graph-analytics":
        return _run_graph_analytics(args)
    if args.mode == "graph-impact":
        return _run_graph_impact(args)
    if args.mode == "graph-export":
        return _run_graph_export(args)
    if args.mode == "graph-compare":
        return _run_graph_compare(args)
    if args.mode == "artifact-index":
        return _run_artifact_index(args)
    if args.mode == "dashboard-serve":
        return _run_dashboard_serve(args)
    if args.mode == "pattern-discover":
        return _run_pattern_discover(args)
    if args.mode == "pattern-health":
        return _run_pattern_health(args)
    if args.mode == "pattern-accept":
        return _run_pattern_accept(args)
    if args.mode == "pattern-activate":
        return _run_pattern_activate(args)
    if args.mode == "pattern-review":
        return _run_pattern_review(args)
    return _record_only(args)


if __name__ == "__main__":
    sys.exit(main())
