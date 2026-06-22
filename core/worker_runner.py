"""
Execution adapters for supervised MCUM workers.

The model router decides which model should handle a worker. This module turns
that routing decision into a concrete runner command while preserving the
existing PowerShell path as the safe fallback.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

from .minimax_credentials import DEFAULT_MINIMAX_MODEL, minimax_credential_status
from .project_context_orchestrator import render_worker_context_slice


DEFAULT_WORKER_RUNNER_POLICY: dict[str, Any] = {
    "enabled": True,
    "default_runner": "powershell",
    "model_aware_runner": "minimax_sdk",
    "model_aware_workers_default": True,
    "minimax_sdk": {
        "enabled": True,
        "protocol": "auto",
        "default_model": DEFAULT_MINIMAX_MODEL,
        "temperature": 0.1,
        "max_output_tokens": 1200,
        "max_prompt_chars": 9000,
        "timeout_seconds": 60,
    },
    "codex_exec": {
        "enabled": True,
        "binary": "codex",
        "sandbox": "workspace-write",
        "skip_git_repo_check": True,
        "color": "never",
        "approval_policy": "never",
        "pass_reasoning_effort": True,
        "max_prompt_chars": 7000,
    },
    "gemini_cli": {
        "enabled": True,
        "binary": "gemini.cmd",
        "default_model": "gemini-2.5-flash",
        "approval_mode": "yolo",
        "output_format": "json",
        "include_project_path": True,
        "skip_trust": True,
        "max_prompt_chars": 7000,
    },
    "spreadsheet_extractor": {
        "enabled": True,
        "binary": sys.executable or "python",
        "max_sheets": 20,
        "max_rows": 25,
        "max_cols": 30,
        "max_scan_rows": 200,
        "max_cell_chars": 180,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_worker_runner_policy(execution_policy: dict[str, Any] | None) -> dict[str, Any]:
    return _deep_merge(
        DEFAULT_WORKER_RUNNER_POLICY,
        (execution_policy or {}).get("worker_runner") or {},
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 64)].rstrip() + "\n...[prompt clipped by MCUM worker runner]"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "si", "on"}


def resolve_worker_runner(
    *,
    requested_runner: str | None,
    model_aware_workers: bool | None,
    no_model_aware_workers: bool | None,
    execution_policy: dict[str, Any] | None,
) -> str:
    policy = normalize_worker_runner_policy(execution_policy)
    if not bool(policy.get("enabled", True)):
        return "powershell"

    requested = str(requested_runner or "auto").strip().lower().replace("_", "-")
    if requested in {"powershell", "shell"}:
        return "powershell"
    if requested in {"codex-exec", "codex"}:
        return "codex_exec"
    if requested in {"gemini-cli", "gemini"}:
        return "gemini_cli"
    if requested in {"minimax-sdk", "minimax", "minimax-sdk-worker"}:
        return "minimax_sdk"
    if requested in {"spreadsheet-extractor", "spreadsheet", "xlsx-extractor", "excel-extractor"}:
        return "spreadsheet_extractor"
    if requested and requested != "auto":
        return "powershell"

    if _truthy(no_model_aware_workers):
        return "powershell"
    if _truthy(model_aware_workers):
        return str(policy.get("model_aware_runner") or "minimax_sdk")
    if bool(policy.get("model_aware_workers_default", False)):
        return str(policy.get("model_aware_runner") or "minimax_sdk")
    return str(policy.get("default_runner") or "powershell")


def _format_config_value(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _powershell_single_quoted(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _codex_reasoning_effort(model_route: dict[str, Any], codex_policy: dict[str, Any]) -> str | None:
    if not bool(codex_policy.get("pass_reasoning_effort", True)):
        return None
    effort = str(model_route.get("reasoning_effort") or "").strip().lower()
    return effort if effort in {"low", "medium", "high", "xhigh"} else None


def _gemini_model(worker: dict[str, Any], model_route: dict[str, Any], gemini_policy: dict[str, Any]) -> str:
    explicit = str(worker.get("gemini_model") or "").strip()
    if explicit:
        return explicit
    routed = str(model_route.get("recommended_model") or worker.get("recommended_model") or "").strip()
    if routed.lower().startswith("gemini"):
        return routed
    fast_decisions = {
        str(decision).strip().lower()
        for decision in list(gemini_policy.get("fast_route_decisions") or [])
        if str(decision).strip()
    }
    route_decision = str(model_route.get("decision") or worker.get("model_decision") or "").strip().lower()
    fast_model = str(gemini_policy.get("fast_model") or "").strip()
    if route_decision in fast_decisions and fast_model:
        return fast_model
    return str(gemini_policy.get("default_model") or "").strip()


def _minimax_model(worker: dict[str, Any], model_route: dict[str, Any], minimax_policy: dict[str, Any]) -> str:
    explicit = str(worker.get("minimax_model") or "").strip()
    if explicit:
        return explicit
    routed = str(model_route.get("recommended_model") or worker.get("recommended_model") or "").strip()
    if routed.lower().startswith("minimax"):
        return routed
    return str(minimax_policy.get("default_model") or DEFAULT_MINIMAX_MODEL).strip()


def _safe_slug(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "item"


def _flatten_path_hints(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        hints: list[str] = []
        for item in value:
            hints.extend(_flatten_path_hints(item))
        return hints
    if isinstance(value, dict):
        hints = []
        for item in value.values():
            hints.extend(_flatten_path_hints(item))
        return hints
    return [str(value)]


def _xlsx_path_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.findall(r"[A-Za-z]:\\[^\"'\r\n]+?\.xlsx", text or "", flags=re.IGNORECASE):
        candidates.append(match.strip())
    for match in re.findall(r"[^\"'\s]+\.xlsx", text or "", flags=re.IGNORECASE):
        candidates.append(match.strip())
    return candidates


def _resolve_existing_xlsx_path(raw: str, *, workdir: str, project_path: str) -> Path | None:
    if not raw:
        return None
    cleaned = str(raw).strip().strip('"').strip("'")
    if not cleaned:
        return None
    candidate = Path(cleaned).expanduser()
    search_candidates = [candidate] if candidate.is_absolute() else [Path(workdir) / candidate, Path(project_path) / candidate]
    for item in search_candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_file() and resolved.suffix.lower() == ".xlsx":
            return resolved
    return None


def _find_spreadsheet_source_path(
    *,
    command: str,
    workdir: str,
    project_path: str,
    worker: dict[str, Any],
    worker_brief: dict[str, Any],
) -> Path | None:
    hints: list[str] = []
    hints.extend(_xlsx_path_candidates_from_text(command))
    for key in (
        "source_path",
        "source_to_review",
        "read_only_scope",
        "editable_scope",
        "expected_deliverable",
        "objective",
    ):
        hints.extend(_flatten_path_hints(worker_brief.get(key)))
        hints.extend(_flatten_path_hints(worker.get(key)))
    for hint in hints:
        resolved = _resolve_existing_xlsx_path(hint, workdir=workdir, project_path=project_path)
        if resolved:
            return resolved
    for base in (Path(workdir), Path(project_path)):
        try:
            if base.exists() and base.is_dir():
                for item in base.glob("*.xlsx"):
                    if item.is_file():
                        return item.resolve()
        except OSError:
            continue
    return None


def _spreadsheet_output_path(*, source_path: Path | None, project_path: str, role: str) -> Path:
    project = Path(project_path).resolve()
    root = project.parent if project.exists() else Path(project_path).expanduser().resolve().parent
    stem = _safe_slug(source_path.stem if source_path else "workbook")
    role_slug = _safe_slug(role)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / ".agent" / "runtime" / "artifacts" / f"SPREADSHEET_EXTRACT_{stamp}-{stem}-{role_slug}.json"


def build_codex_worker_prompt(
    *,
    role: str,
    worker: dict[str, Any],
    worker_brief: dict[str, Any],
    command: str,
    project_path: str,
    project_name: str | None,
    workdir: str,
    model_route: dict[str, Any],
    max_prompt_chars: int,
) -> str:
    editable_scope = worker_brief.get("editable_scope") or worker.get("editable_scope")
    read_only_scope = worker_brief.get("read_only_scope") or worker.get("read_only_scope")
    protected_scope = worker_brief.get("protected_scope") or worker.get("protected_scope")
    token_budget = model_route.get("token_budget") or {}
    context_slice = render_worker_context_slice(worker_brief.get("worker_context_slice"))
    prompt = f"""
Eres un worker supervisado por MCUM dentro de una tarea multiagente.

Identidad del worker:
- role: {role}
- mode: {worker.get("mode") or "read_only"}
- agent_profile: {worker.get("agent_profile") or model_route.get("agent_profile") or role}
- recommended_model: {model_route.get("recommended_model") or worker.get("recommended_model") or "default"}
- reasoning_effort: {model_route.get("reasoning_effort") or "medium"}
- token_budget: context_in={token_budget.get("context_in")}, output={token_budget.get("output")}

Contexto de proyecto:
- project_name: {project_name or Path(project_path).name}
- project_path: {project_path}
- workdir: {workdir}

Guardrails:
- No estas solo en el codebase: no reviertas cambios ajenos.
- Respeta editable_scope: {editable_scope or "no write scope declared"}
- Usa read_only_scope solo para inspeccion: {read_only_scope or "not declared"}
- No toques protected_scope: {protected_scope or "not declared"}
- No escribas memoria MCUM directamente; el coordinador registrara el resultado.
- Si el comando/tarea no requiere cambios, no edites archivos.
- Mantente conciso para ahorrar tokens.

Brief del worker:
- objective: {worker_brief.get("objective") or ""}
- expected_deliverable: {worker_brief.get("expected_deliverable") or ""}
- success_criteria: {worker_brief.get("success_criteria") or ""}
- validation_required: {worker_brief.get("validation_required") or ""}

Contexto MCUM del proyecto:
{context_slice}

Instruccion asignada por el coordinador:
{command}

Entrega final:
- resumen corto
- validacion ejecutada o razon de no ejecutarla
- archivos tocados si corresponde
- riesgos o bloqueos
    """.strip()
    return _clip(prompt, max(1200, int(max_prompt_chars or 7000)))


def build_gemini_worker_prompt(
    *,
    role: str,
    worker: dict[str, Any],
    worker_brief: dict[str, Any],
    command: str,
    project_path: str,
    project_name: str | None,
    workdir: str,
    model_route: dict[str, Any],
    max_prompt_chars: int,
) -> str:
    editable_scope = worker_brief.get("editable_scope") or worker.get("editable_scope")
    read_only_scope = worker_brief.get("read_only_scope") or worker.get("read_only_scope")
    protected_scope = worker_brief.get("protected_scope") or worker.get("protected_scope")
    context_slice = render_worker_context_slice(worker_brief.get("worker_context_slice"))
    prompt = f"""
Eres un worker supervisado por MCUM e invocado desde un agente entrypoint.

Identidad:
- role: {role}
- mode: {worker.get("mode") or "read_only"}
- project_name: {project_name or Path(project_path).name}
- project_path: {project_path}
- workdir: {workdir}
- recommended_model: {model_route.get("recommended_model") or worker.get("recommended_model") or "default"}

Guardrails:
- No estas solo en el codebase: no reviertas cambios ajenos.
- Escribe solo dentro de editable_scope: {editable_scope or "no write scope declared"}.
- Usa read_only_scope solo para inspeccion: {read_only_scope or "not declared"}.
- No toques protected_scope: {protected_scope or "not declared"}.
- No escribas memoria MCUM directamente; el coordinador registrara el resultado.
- Si la tarea no requiere cambios, no edites archivos.

Brief:
- objective: {worker_brief.get("objective") or ""}
- expected_deliverable: {worker_brief.get("expected_deliverable") or ""}
- success_criteria: {worker_brief.get("success_criteria") or ""}
- validation_required: {worker_brief.get("validation_required") or ""}

Contexto MCUM del proyecto:
{context_slice}

Instruccion asignada:
{command}

Devuelve solo un JSON con esta forma:
{{
  "status": "success|partial|failure",
  "summary": "resumen corto",
  "files_changed": ["ruta"],
  "validation": "validacion ejecutada o razon de no ejecutarla",
  "risks": ["riesgo o bloqueo"]
}}
""".strip()
    return _clip(prompt, max(1200, int(max_prompt_chars or 7000)))


def build_worker_runner_invocation(
    *,
    runner: str,
    command: str,
    workdir: str,
    project_path: str,
    project_name: str | None,
    worker: dict[str, Any],
    worker_brief: dict[str, Any],
    execution_policy: dict[str, Any] | None,
    worker_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    policy = normalize_worker_runner_policy(execution_policy)
    role = str(worker.get("role") or worker_brief.get("worker_role") or "worker")
    model_route = dict(worker.get("model_route") or {})
    normalized_runner = str(runner or "powershell").strip().lower().replace("-", "_")

    if normalized_runner not in {"codex_exec", "gemini_cli", "spreadsheet_extractor", "minimax_sdk"}:
        return {
            "runner": "powershell",
            "args": ["powershell.exe", "-NoProfile", "-Command", command],
            "stdin": None,
            "metadata": {
                "runner": "powershell",
                "model_aware": False,
            },
        }

    if normalized_runner == "minimax_sdk":
        minimax_policy = dict(policy.get("minimax_sdk") or {})
        if not bool(minimax_policy.get("enabled", True)):
            return {
                "runner": "powershell",
                "args": ["powershell.exe", "-NoProfile", "-Command", command],
                "stdin": None,
                "metadata": {
                    "runner": "powershell",
                    "model_aware": False,
                    "fallback_reason": "minimax_sdk_disabled",
                },
            }
        model = _minimax_model(worker, model_route, minimax_policy)
        script_path = Path(__file__).resolve().parent / "minimax_worker.py"
        payload = {
            "runner": "minimax_sdk",
            "command": command,
            "project_path": project_path,
            "project_name": project_name,
            "workdir": workdir,
            "worker": worker,
            "worker_brief": worker_brief,
            "model_route": model_route,
            "model": model,
            "policy": {
                key: value
                for key, value in minimax_policy.items()
                if key not in {"api_key", "token", "secret", "authorization"}
            },
            "timeout_seconds": worker_timeout_seconds or minimax_policy.get("timeout_seconds"),
        }
        prompt_estimate = max(
            1,
            len(json.dumps(payload, ensure_ascii=False, default=str)) // 4,
        )
        credential_status = minimax_credential_status(minimax_policy)
        return {
            "runner": "minimax_sdk",
            "args": [str(minimax_policy.get("binary") or sys.executable or "python"), str(script_path)],
            "stdin": json.dumps(payload, ensure_ascii=False, default=str),
            "metadata": {
                "runner": "minimax_sdk",
                "model_aware": True,
                "recommended_model": model or None,
                "requested_route_model": model_route.get("recommended_model") or worker.get("recommended_model"),
                "provider": "minimax",
                "protocol": credential_status.get("protocol") or minimax_policy.get("protocol") or "auto",
                "credential_available": bool(credential_status.get("available")),
                "credential_source": credential_status.get("source"),
                "base_url": credential_status.get("base_url"),
                "prompt_tokens_estimate": prompt_estimate,
                "max_output_tokens": int(minimax_policy.get("max_output_tokens") or 1200),
                "temperature": minimax_policy.get("temperature", 0.1),
            },
        }

    if normalized_runner == "spreadsheet_extractor":
        extractor_policy = dict(policy.get("spreadsheet_extractor") or {})
        if not bool(extractor_policy.get("enabled", True)):
            return {
                "runner": "powershell",
                "args": ["powershell.exe", "-NoProfile", "-Command", command],
                "stdin": None,
                "metadata": {
                    "runner": "powershell",
                    "model_aware": False,
                    "fallback_reason": "spreadsheet_extractor_disabled",
                },
            }
        source_path = _find_spreadsheet_source_path(
            command=command,
            workdir=workdir,
            project_path=project_path,
            worker=worker,
            worker_brief=worker_brief,
        )
        if not source_path:
            return {
                "runner": "spreadsheet_extractor",
                "args": [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "Write-Error 'spreadsheet_extractor requires an existing .xlsx source path.'; exit 2",
                ],
                "stdin": None,
                "metadata": {
                    "runner": "spreadsheet_extractor",
                    "model_aware": False,
                    "error": "xlsx_source_not_found",
                },
            }
        output_path = _spreadsheet_output_path(source_path=source_path, project_path=project_path, role=role)
        script_path = Path(__file__).resolve().parent / "spreadsheet_extractor.py"
        args = [
            str(extractor_policy.get("binary") or sys.executable or "python"),
            str(script_path),
            str(source_path),
            "--output",
            str(output_path),
            "--max-sheets",
            str(int(extractor_policy.get("max_sheets") or 20)),
            "--max-rows",
            str(int(extractor_policy.get("max_rows") or 25)),
            "--max-cols",
            str(int(extractor_policy.get("max_cols") or 30)),
            "--max-scan-rows",
            str(int(extractor_policy.get("max_scan_rows") or 200)),
            "--max-cell-chars",
            str(int(extractor_policy.get("max_cell_chars") or 180)),
        ]
        return {
            "runner": "spreadsheet_extractor",
            "args": args,
            "stdin": None,
            "metadata": {
                "runner": "spreadsheet_extractor",
                "model_aware": False,
                "recommended_model": "local_openpyxl",
                "source_path": str(source_path),
                "output_path": str(output_path),
                "limits": {
                    "max_sheets": int(extractor_policy.get("max_sheets") or 20),
                    "max_rows": int(extractor_policy.get("max_rows") or 25),
                    "max_cols": int(extractor_policy.get("max_cols") or 30),
                    "max_scan_rows": int(extractor_policy.get("max_scan_rows") or 200),
                },
            },
        }

    if normalized_runner == "gemini_cli":
        gemini_policy = dict(policy.get("gemini_cli") or {})
        if not bool(gemini_policy.get("enabled", True)):
            return {
                "runner": "powershell",
                "args": ["powershell.exe", "-NoProfile", "-Command", command],
                "stdin": None,
                "metadata": {
                    "runner": "powershell",
                    "model_aware": False,
                    "fallback_reason": "gemini_cli_disabled",
                },
            }

        model = _gemini_model(worker, model_route, gemini_policy)
        args = [str(gemini_policy.get("binary") or "gemini")]
        if bool(gemini_policy.get("skip_trust", False)):
            args.append("--skip-trust")
        if model:
            args.extend(["--model", model])
        output_format = str(gemini_policy.get("output_format") or "").strip()
        if output_format:
            args.extend(["--output-format", output_format])
        approval_mode = str(gemini_policy.get("approval_mode") or "").strip()
        if approval_mode:
            args.extend(["--approval-mode", approval_mode])
        if bool(gemini_policy.get("include_project_path", False)):
            args.extend(["--include-directories", str(project_path)])
        args.extend(["-p", "Follow the MCUM worker brief from stdin and return only the requested JSON."])
        prompt = build_gemini_worker_prompt(
            role=role,
            worker=worker,
            worker_brief=worker_brief,
            command=command,
            project_path=project_path,
            project_name=project_name,
            workdir=workdir,
            model_route=model_route,
            max_prompt_chars=int(gemini_policy.get("max_prompt_chars") or 7000),
        )
        return {
            "runner": "gemini_cli",
            "args": args,
            "stdin": prompt,
            "metadata": {
                "runner": "gemini_cli",
                "model_aware": True,
                "recommended_model": model or None,
                "output_format": output_format or None,
                "approval_mode": approval_mode or None,
                "prompt_tokens_estimate": max(1, len(prompt) // 4),
            },
        }

    codex_policy = dict(policy.get("codex_exec") or {})
    if not bool(codex_policy.get("enabled", True)):
        return {
            "runner": "powershell",
            "args": ["powershell.exe", "-NoProfile", "-Command", command],
            "stdin": None,
            "metadata": {
                "runner": "powershell",
                "model_aware": False,
                "fallback_reason": "codex_exec_disabled",
            },
        }

    model = str(model_route.get("recommended_model") or worker.get("recommended_model") or "").strip()
    args = [
        str(codex_policy.get("binary") or "codex"),
        "exec",
    ]
    if model:
        args.extend(["--model", model])
    args.extend(["--cd", str(workdir)])
    sandbox = str(codex_policy.get("sandbox") or "workspace-write").strip()
    if sandbox:
        args.extend(["--sandbox", sandbox])
    if bool(codex_policy.get("skip_git_repo_check", True)):
        args.append("--skip-git-repo-check")
    color = str(codex_policy.get("color") or "").strip()
    if color:
        args.extend(["--color", color])

    approval_policy = str(codex_policy.get("approval_policy") or "").strip()
    if approval_policy:
        args.extend(["-c", f"approval_policy={_format_config_value(approval_policy)}"])

    effort = _codex_reasoning_effort(model_route, codex_policy)
    if effort:
        args.extend(["-c", f"model_reasoning_effort={_format_config_value(effort)}"])

    args.append("-")
    prompt = build_codex_worker_prompt(
        role=role,
        worker=worker,
        worker_brief=worker_brief,
        command=command,
        project_path=project_path,
        project_name=project_name,
        workdir=workdir,
        model_route=model_route,
        max_prompt_chars=int(codex_policy.get("max_prompt_chars") or 7000),
    )
    return {
        "runner": "codex_exec",
        "args": args,
        "stdin": prompt,
        "metadata": {
            "runner": "codex_exec",
            "model_aware": True,
            "recommended_model": model or None,
            "reasoning_effort": effort,
            "sandbox": sandbox or None,
            "prompt_tokens_estimate": max(1, len(prompt) // 4),
        },
    }
