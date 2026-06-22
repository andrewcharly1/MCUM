"""
Ephemeral MiniMax worker runner for MCUM.

This module is launched as a subprocess by MCUM. It receives a JSON payload on
stdin, resolves MiniMax credentials at runtime, calls the provider, and prints a
single JSON result. Secrets are never printed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib import error, request

try:
    from .minimax_credentials import DEFAULT_MINIMAX_MODEL, MiniMaxCredentials, resolve_minimax_credentials
except ImportError:  # pragma: no cover - used when invoked as a script path
    skill_parent = Path(__file__).resolve().parents[2]
    if str(skill_parent) not in sys.path:
        sys.path.insert(0, str(skill_parent))
    from MCUM.core.minimax_credentials import DEFAULT_MINIMAX_MODEL, MiniMaxCredentials, resolve_minimax_credentials


def _clip(text: Any, limit: int = 2400) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 32)].rstrip() + "\n...[clipped]"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_loads(text: str) -> Any:
    return json.loads(text)


def _parse_structured_content(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    candidates = [cleaned]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _summary_from_content(content: str, structured: dict[str, Any] | None) -> str:
    if structured:
        for key in ("summary", "resumen", "output_summary"):
            value = structured.get(key)
            if value:
                return _clip(value, 600)
    for line in str(content or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return _clip(cleaned, 600)
    return "MiniMax worker completed without textual output."


def _status_from_content(structured: dict[str, Any] | None) -> str:
    status = str((structured or {}).get("status") or "").strip().lower()
    if status in {"success", "partial", "failure"}:
        return status
    return "success"


def _build_worker_prompt(payload: dict[str, Any], *, max_prompt_chars: int) -> str:
    worker = _as_dict(payload.get("worker"))
    worker_brief = _as_dict(payload.get("worker_brief"))
    model_route = _as_dict(payload.get("model_route"))
    token_budget = _as_dict(model_route.get("token_budget"))
    command = str(payload.get("command") or "")
    project_path = str(payload.get("project_path") or "")
    project_name = str(payload.get("project_name") or Path(project_path).name)
    workdir = str(payload.get("workdir") or project_path)
    role = str(worker.get("role") or worker_brief.get("worker_role") or "worker")
    entrypoint_agent = str(payload.get("entrypoint_agent") or worker_brief.get("entrypoint_agent") or "unknown")
    context_slice = _clip(
        json.dumps(worker_brief.get("worker_context_slice") or {}, ensure_ascii=False, default=str, indent=2),
        3600,
    )

    prompt = f"""
Eres un subagente MiniMax efimero subordinado a MCUM.

Jerarquia operativa:
- entrypoint_agent: {entrypoint_agent}
- orquestador_maximo: MCUM
- worker_role: {role}
- worker_mode: {worker.get("mode") or "read_only"}
- agent_profile: {worker.get("agent_profile") or model_route.get("agent_profile") or role}
- recommended_model_route: {model_route.get("recommended_model") or worker.get("recommended_model") or "default"}
- token_budget: context_in={token_budget.get("context_in")}, output={token_budget.get("output")}

Proyecto:
- project_name: {project_name}
- project_path: {project_path}
- workdir: {workdir}

Reglas:
- MCUM decide, registra y conserva memoria; no escribas memoria MCUM directamente.
- No reviertas cambios ajenos.
- Respeta editable_scope: {worker_brief.get("editable_scope") or worker.get("editable_scope") or "no write scope declared"}.
- Usa read_only_scope solo para inspeccion: {worker_brief.get("read_only_scope") or worker.get("read_only_scope") or "not declared"}.
- No toques protected_scope: {worker_brief.get("protected_scope") or worker.get("protected_scope") or "not declared"}.
- Si faltan datos criticos, responde partial y lista exactamente que falta.
- Mantente conciso: devuelve solo informacion accionable para que MCUM integre.

Brief MCUM:
- objective: {worker_brief.get("objective") or ""}
- expected_deliverable: {worker_brief.get("expected_deliverable") or ""}
- success_criteria: {worker_brief.get("success_criteria") or ""}
- validation_required: {worker_brief.get("validation_required") or ""}

Contexto MCUM del proyecto:
{context_slice}

Instruccion asignada:
{command}

Devuelve solo JSON valido con esta forma:
{{
  "status": "success|partial|failure",
  "summary": "resumen corto",
  "findings": ["hallazgo accionable"],
  "files_changed": ["ruta si aplica"],
  "validation": "validacion ejecutada o razon de no ejecutarla",
  "risks": ["riesgo o bloqueo"],
  "next_steps": ["paso concreto opcional"]
}}
""".strip()
    if len(prompt) <= max_prompt_chars:
        return prompt
    return prompt[: max(0, max_prompt_chars - 64)].rstrip() + "\n...[prompt clipped by MCUM MiniMax worker]"


def _join_url(base_url: str, suffix: str) -> str:
    base = str(base_url or "").rstrip("/")
    tail = suffix.lstrip("/")
    return f"{base}/{tail}"


def _anthropic_messages_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=encoded, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MiniMax HTTP {exc.code}: {_clip(body_text, 1200)}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"MiniMax connection failed: {_clip(exc.reason, 600)}") from exc
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MiniMax returned non-JSON response: {_clip(text, 1200)}") from exc
    return loaded if isinstance(loaded, dict) else {"raw": loaded}


def _call_openai_compatible(
    *,
    credential: MiniMaxCredentials,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    url = _join_url(credential.base_url, "chat/completions")
    response = _post_json(
        url,
        {
            "Authorization": f"Bearer {credential.api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": credential.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "stream": False,
        },
        timeout=timeout,
    )
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    content = ""
    if choices:
        message = _as_dict(choices[0].get("message"))
        content = str(message.get("content") or "")
    return content, _as_dict(response.get("usage"))


def _call_anthropic_compatible(
    *,
    credential: MiniMaxCredentials,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    url = _anthropic_messages_url(credential.base_url)
    response = _post_json(
        url,
        {
            "x-api-key": credential.api_key,
            "Authorization": f"Bearer {credential.api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "model": credential.model,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    blocks = response.get("content") if isinstance(response.get("content"), list) else []
    parts: list[str] = []
    for block in blocks:
        item = _as_dict(block)
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item.get("text")))
    usage = _as_dict(response.get("usage"))
    return "\n".join(parts), {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def _normalize_usage(usage: dict[str, Any], *, prompt: str, content: str) -> dict[str, int]:
    def _int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    input_tokens = _int(usage.get("input_tokens") or usage.get("prompt_tokens"))
    output_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
    if input_tokens is None:
        input_tokens = max(1, len(prompt) // 4)
    if output_tokens is None:
        output_tokens = max(1, len(content or "") // 4)
    total_tokens = _int(usage.get("total_tokens")) or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _result_payload(
    *,
    status: str,
    summary: str,
    content: str = "",
    structured: dict[str, Any] | None = None,
    credential: MiniMaxCredentials | None = None,
    usage: dict[str, int] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "provider": "minimax",
        "content": content,
        "structured_result": structured or {},
        "usage": usage or {},
    }
    if credential:
        payload.update(credential.redacted_metadata())
    if error_message:
        payload["error"] = error_message
    return payload


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        raw_stdin = (sys.stdin.read() or "{}").lstrip("\ufeff")
        if raw_stdin.startswith("\xef\xbb\xbf"):
            raw_stdin = raw_stdin[3:]
        payload = _json_loads(raw_stdin)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                _result_payload(status="failure", summary="Invalid MiniMax worker payload.", error_message=str(exc)),
                ensure_ascii=False,
            )
        )
        return 2
    if not isinstance(payload, dict):
        print(json.dumps(_result_payload(status="failure", summary="MiniMax worker payload must be an object."), ensure_ascii=False))
        return 2

    policy = _as_dict(payload.get("policy"))
    credential = resolve_minimax_credentials(policy)
    if not credential:
        print(
            json.dumps(
                _result_payload(
                    status="failure",
                    summary="MiniMax credentials were not found in process, Hermes, Claude, or OpenCode configs.",
                    error_message="missing_minimax_credentials",
                ),
                ensure_ascii=False,
            )
        )
        return 2

    requested_model = str(payload.get("model") or policy.get("default_model") or credential.model or DEFAULT_MINIMAX_MODEL).strip()
    if requested_model:
        credential = MiniMaxCredentials(
            api_key=credential.api_key,
            base_url=credential.base_url,
            protocol=credential.protocol,
            model=requested_model,
            source=credential.source,
        )

    max_prompt_chars = int(policy.get("max_prompt_chars") or 9000)
    max_output_tokens = int(policy.get("max_output_tokens") or 1200)
    temperature = float(policy.get("temperature") if policy.get("temperature") is not None else 0.1)
    timeout = int(payload.get("timeout_seconds") or policy.get("timeout_seconds") or 60)
    prompt = _build_worker_prompt(payload, max_prompt_chars=max_prompt_chars)
    system_prompt = "Eres un worker subordinado a MCUM. Responde solo JSON valido y no reveles secretos."

    if str(os.environ.get("MCUM_MINIMAX_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}:
        usage = {"input_tokens": max(1, len(prompt) // 4), "output_tokens": 1, "total_tokens": max(2, len(prompt) // 4 + 1)}
        print(
            json.dumps(
                _result_payload(
                    status="success",
                    summary="MiniMax dry run completed.",
                    content='{"status":"success","summary":"MiniMax dry run completed.","findings":[],"files_changed":[],"validation":"dry_run","risks":[]}',
                    structured={"status": "success", "summary": "MiniMax dry run completed.", "validation": "dry_run"},
                    credential=credential,
                    usage=usage,
                ),
                ensure_ascii=False,
            )
        )
        return 0

    try:
        if credential.protocol == "anthropic":
            content, raw_usage = _call_anthropic_compatible(
                credential=credential,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
        else:
            content, raw_usage = _call_openai_compatible(
                credential=credential,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
    except Exception as exc:
        print(
            json.dumps(
                _result_payload(
                    status="failure",
                    summary="MiniMax worker API call failed.",
                    credential=credential,
                    error_message=str(exc),
                ),
                ensure_ascii=False,
            )
        )
        return 1

    structured = _parse_structured_content(content)
    usage = _normalize_usage(raw_usage, prompt=prompt, content=content)
    result = _result_payload(
        status=_status_from_content(structured),
        summary=_summary_from_content(content, structured),
        content=_clip(content, 5000),
        structured=structured,
        credential=credential,
        usage=usage,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
