"""
Frontend QA planning for MCUM using Playwright MCP.

This module prepares an auditable QA contract and MCP configuration. It does
not start browsers by itself; the connected agent/client executes the MCP tools
while MCUM records the plan, evidence expectations, and artifacts.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any


DEFAULT_FRONTEND_QA_POLICY: dict[str, Any] = {
    "enabled": True,
    "provider": "playwright_mcp",
    "default_profile": "fast",
    "default_base_urls": {
        "next": "http://localhost:3000",
        "vite": "http://localhost:5173",
        "astro": "http://localhost:4321",
        "angular": "http://localhost:4200",
        "unknown": "http://localhost:3000",
    },
    "mcp": {
        "server_name": "playwright",
        "command": "npx",
        "package": "@playwright/mcp@latest",
        "use_yes_flag": True,
        "headless": True,
        "browser": "chrome",
        "caps": ["testing", "storage"],
        "isolated": True,
        "viewport_size": "1280x720",
        "output_mode": "file",
        "snapshot_mode": "full",
        "test_id_attribute": "data-testid",
        "timeout_action_ms": 5000,
        "timeout_navigation_ms": 60000,
    },
    "checks": ["render_smoke", "critical_text_visible", "console_error_scan"],
    "profiles": {
        "fast": {
            "checks": ["render_smoke", "critical_text_visible", "console_error_scan"],
            "mcp": {
                "caps": ["testing"],
                "timeout_action_ms": 3000,
                "timeout_navigation_ms": 30000,
            },
            "token_controls": {
                "avoid_screenshots_by_default": True,
                "max_screenshots": 0,
                "max_viewports": 1,
            },
        },
        "standard": {
            "checks": [
                "render_smoke",
                "accessibility_snapshot",
                "critical_text_visible",
                "console_error_scan",
                "responsive_viewport_smoke",
            ],
            "mcp": {
                "caps": ["testing", "storage"],
                "timeout_action_ms": 5000,
                "timeout_navigation_ms": 45000,
            },
            "token_controls": {
                "avoid_screenshots_by_default": False,
                "max_screenshots": 1,
                "max_viewports": 2,
            },
        },
        "strict": {
            "checks": [
                "render_smoke",
                "accessibility_snapshot",
                "critical_text_visible",
                "primary_navigation",
                "form_or_cta_interaction",
                "console_error_scan",
                "responsive_viewport_smoke",
                "auth_state_if_required",
            ],
            "mcp": {
                "caps": ["testing", "storage"],
                "timeout_action_ms": 7000,
                "timeout_navigation_ms": 60000,
            },
            "token_controls": {
                "avoid_screenshots_by_default": False,
                "max_screenshots": 8,
                "max_viewports": 2,
            },
        },
    },
    "token_controls": {
        "minimal_caps": True,
        "prefer_accessibility_snapshot": True,
        "persist_outputs_to_file": True,
        "avoid_vision_by_default": True,
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


def infer_frontend_qa_profile(
    task_text: str | None = None,
    requested_profile: str | None = None,
    *,
    execution_policy: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Select the cheapest QA profile that still matches the user's intent."""
    base_policy = _deep_merge(
        DEFAULT_FRONTEND_QA_POLICY,
        (execution_policy or {}).get("frontend_qa") or {},
    )
    profiles = dict(base_policy.get("profiles") or {})
    allowed = set(profiles) or {"fast", "standard", "strict"}
    requested = str(requested_profile or "").strip().lower()
    if requested and requested != "auto":
        if requested in allowed:
            return requested, "explicit_profile"
        return str(base_policy.get("default_profile") or "fast"), f"unknown_profile:{requested}"

    text = str(task_text or "").lower()
    strict_terms = (
        "strict",
        "estricto",
        "auditoria visual",
        "auditoría visual",
        "pixel perfect",
        "pixel-perfect",
        "captura por slide",
        "screenshots por slide",
        "pdf exportado",
        "entrega final",
        "validacion visual final",
        "validación visual final",
    )
    standard_terms = (
        "render validado",
        "validar visual",
        "validación visual",
        "validacion visual",
        "frontend",
        "interfaz",
        "panel",
        "presentacion",
        "presentación",
        "landing",
        "dashboard",
    )
    if any(term in text for term in strict_terms) and "strict" in allowed:
        return "strict", "inferred_strict_visual_final"
    if any(term in text for term in standard_terms) and "standard" in allowed:
        return "standard", "inferred_standard_visual_or_frontend"
    default_profile = str(base_policy.get("default_profile") or "fast").strip().lower()
    if default_profile in allowed:
        return default_profile, "default_profile"
    return "fast", "fallback_fast"


def normalize_frontend_qa_policy(
    execution_policy: dict[str, Any] | None,
    qa_profile: str | None = None,
) -> dict[str, Any]:
    merged = _deep_merge(
        DEFAULT_FRONTEND_QA_POLICY,
        (execution_policy or {}).get("frontend_qa") or {},
    )
    profiles = dict(merged.get("profiles") or {})
    profile = str(qa_profile or merged.get("default_profile") or "fast").strip().lower()
    if profile not in profiles:
        profile = str(merged.get("default_profile") or "fast").strip().lower()
    if profile in profiles:
        merged = _deep_merge(merged, dict(profiles.get(profile) or {}))
    merged["profile"] = profile
    return merged


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        raw = package_json.get(key) or {}
        if isinstance(raw, dict):
            merged.update({str(name): str(version) for name, version in raw.items()})
    return merged


def _framework_from_package(package_json: dict[str, Any]) -> str:
    deps = _dependencies(package_json)
    scripts = " ".join(str(value) for value in dict(package_json.get("scripts") or {}).values()).lower()
    names = {name.lower() for name in deps}
    if "next" in names or "next " in scripts:
        return "next"
    if "vite" in names or "vite" in scripts:
        return "vite"
    if "@angular/core" in names or "ng serve" in scripts:
        return "angular"
    if "astro" in names or "astro" in scripts:
        return "astro"
    if "svelte" in names or "sveltekit" in scripts:
        return "svelte"
    if "vue" in names:
        return "vue"
    if "react" in names:
        return "react"
    return "unknown"


def _script_command(scripts: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in scripts and str(scripts[name]).strip():
            return f"npm run {name}"
    return None


def _port_from_scripts(scripts: dict[str, Any]) -> int | None:
    text = " ".join(str(value) for value in scripts.values())
    match = re.search(r"(?:--port\s+|--port=|-p\s+)(\d{2,5})", text)
    if match:
        return int(match.group(1))
    return None


def _base_url_for(framework: str, scripts: dict[str, Any], policy: dict[str, Any]) -> str:
    port = _port_from_scripts(scripts)
    if port:
        return f"http://localhost:{port}"
    defaults = dict(policy.get("default_base_urls") or {})
    if framework in defaults:
        return str(defaults[framework])
    if framework in {"react", "vue", "svelte"}:
        return str(defaults.get("vite") or "http://localhost:5173")
    return str(defaults.get("unknown") or "http://localhost:3000")


def _package_candidates(project_path: Path) -> list[Path]:
    candidates = [project_path / "package.json"]
    for subdir in ("apps/web", "apps/frontend", "frontend", "web", "app", "packages/web"):
        candidates.append(project_path / subdir / "package.json")
    return candidates


def detect_frontend_project(project_path: str, execution_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(project_path).resolve()
    policy = normalize_frontend_qa_policy(execution_policy)
    for package_path in _package_candidates(root):
        if not package_path.exists():
            continue
        package_json = _read_json(package_path)
        if not package_json:
            continue
        scripts = dict(package_json.get("scripts") or {})
        framework = _framework_from_package(package_json)
        frontend_root = package_path.parent
        return {
            "found": True,
            "project_root": str(root),
            "frontend_root": str(frontend_root),
            "package_json": str(package_path),
            "package_name": package_json.get("name"),
            "framework": framework,
            "scripts": scripts,
            "dev_command": _script_command(scripts, ("dev", "start", "serve")),
            "preview_command": _script_command(scripts, ("preview", "start")),
            "build_command": _script_command(scripts, ("build",)),
            "test_command": _script_command(scripts, ("test:e2e", "e2e", "test")),
            "default_base_url": _base_url_for(framework, scripts, policy),
        }
    return {
        "found": False,
        "project_root": str(root),
        "frontend_root": str(root),
        "framework": "unknown",
        "default_base_url": _base_url_for("unknown", {}, policy),
        "reason": "No package.json found in common frontend roots.",
    }


def playwright_mcp_output_dir(project_path: str) -> Path:
    return Path(project_path).resolve() / ".agent" / "runtime" / "playwright-mcp"


def playwright_mcp_config_path(project_path: str) -> Path:
    return Path(project_path).resolve() / ".mcum" / "playwright-mcp.json"


def _browser_cache_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _browser_cache_keywords(browser: str | None) -> tuple[str, ...]:
    selected = str(browser or "chrome").lower()
    if selected in {"chrome", "msedge"}:
        return ("chromium", "chrome", "headless_shell")
    if selected == "firefox":
        return ("firefox",)
    if selected == "webkit":
        return ("webkit",)
    return (selected,)


def preflight_playwright_environment(
    project_path: str,
    *,
    frontend_root: str | None = None,
    browser: str | None = None,
) -> dict[str, Any]:
    """Lightweight local readiness check to avoid expensive blind Playwright retries."""
    root = Path(frontend_root or project_path).resolve()
    node_path = shutil.which("node")
    npx_path = shutil.which("npx")
    local_node_modules = root / "node_modules"
    local_playwright_paths = [
        local_node_modules / "playwright" / "package.json",
        local_node_modules / "@playwright" / "test" / "package.json",
    ]
    local_playwright_installed = any(path.exists() for path in local_playwright_paths)
    cache_root = _browser_cache_root()
    keywords = _browser_cache_keywords(browser)
    browser_candidates: list[str] = []
    if cache_root.exists():
        for candidate in cache_root.iterdir():
            name = candidate.name.lower()
            if any(keyword in name for keyword in keywords):
                browser_candidates.append(str(candidate))

    missing: list[str] = []
    recommendations: list[str] = []
    if not node_path:
        missing.append("node")
        recommendations.append("Install Node.js 18+ before running Playwright MCP.")
    if not npx_path:
        missing.append("npx")
        recommendations.append("Ensure npm/npx is available in PATH.")
    if not local_playwright_installed:
        missing.append("local_playwright_package")
        recommendations.append("If local scripts use require('playwright'), run: npm install -D playwright")
    if not browser_candidates:
        selected_browser = str(browser or "chromium").lower()
        missing.append("playwright_browser")
        recommendations.append(f"Before visual/browser execution, run: npx playwright install {selected_browser}")

    if not node_path or not npx_path:
        status = "blocked"
    elif not browser_candidates:
        status = "needs_browser_install"
    elif not local_playwright_installed:
        status = "mcp_ready_local_package_missing"
    else:
        status = "ready"

    return {
        "status": status,
        "node": {"found": bool(node_path), "path": node_path},
        "npx": {"found": bool(npx_path), "path": npx_path},
        "local_playwright_package": {
            "found": local_playwright_installed,
            "checked": [str(path) for path in local_playwright_paths],
        },
        "browser_cache": {
            "root": str(cache_root),
            "keywords": list(keywords),
            "found": bool(browser_candidates),
            "candidates": browser_candidates[:5],
        },
        "missing": missing,
        "recommendations": recommendations,
    }


def _mcp_args(policy: dict[str, Any], *, project_path: str, headless: bool | None, browser: str | None) -> list[str]:
    mcp_policy = dict(policy.get("mcp") or {})
    args: list[str] = []
    if bool(mcp_policy.get("use_yes_flag", True)):
        args.append("-y")
    args.append(str(mcp_policy.get("package") or "@playwright/mcp@latest"))

    use_headless = bool(mcp_policy.get("headless", True)) if headless is None else bool(headless)
    if use_headless:
        args.append("--headless")
    selected_browser = str(browser or mcp_policy.get("browser") or "").strip()
    if selected_browser:
        args.append(f"--browser={selected_browser}")
    caps = [str(cap).strip() for cap in list(mcp_policy.get("caps") or []) if str(cap).strip()]
    if caps:
        args.append(f"--caps={','.join(caps)}")
    if bool(mcp_policy.get("isolated", True)):
        args.append("--isolated")
    output_dir = playwright_mcp_output_dir(project_path)
    args.extend(["--output-dir", str(output_dir)])
    output_mode = str(mcp_policy.get("output_mode") or "file").strip()
    if output_mode:
        args.extend(["--output-mode", output_mode])
    snapshot_mode = str(mcp_policy.get("snapshot_mode") or "").strip()
    if snapshot_mode:
        args.extend(["--snapshot-mode", snapshot_mode])
    test_id_attribute = str(mcp_policy.get("test_id_attribute") or "").strip()
    if test_id_attribute:
        args.extend(["--test-id-attribute", test_id_attribute])
    viewport_size = str(mcp_policy.get("viewport_size") or "").strip()
    if viewport_size:
        args.extend(["--viewport-size", viewport_size])
    timeout_action = mcp_policy.get("timeout_action_ms")
    if timeout_action:
        args.extend(["--timeout-action", str(int(timeout_action))])
    timeout_navigation = mcp_policy.get("timeout_navigation_ms")
    if timeout_navigation:
        args.extend(["--timeout-navigation", str(int(timeout_navigation))])
    return args


def build_playwright_mcp_config(
    project_path: str,
    *,
    execution_policy: dict[str, Any] | None = None,
    qa_profile: str | None = None,
    headless: bool | None = None,
    browser: str | None = None,
) -> dict[str, Any]:
    policy = normalize_frontend_qa_policy(execution_policy, qa_profile=qa_profile)
    mcp_policy = dict(policy.get("mcp") or {})
    server_name = str(mcp_policy.get("server_name") or "playwright")
    return {
        "mcpServers": {
            server_name: {
                "command": str(mcp_policy.get("command") or "npx"),
                "args": _mcp_args(policy, project_path=project_path, headless=headless, browser=browser),
            }
        }
    }


def build_agent_install_notes(mcp_config: dict[str, Any], preflight: dict[str, Any] | None = None) -> dict[str, Any]:
    server = dict((mcp_config.get("mcpServers") or {}).get("playwright") or {})
    command = str(server.get("command") or "npx")
    args = [str(item) for item in list(server.get("args") or [])]
    setup_commands = list((preflight or {}).get("recommendations") or [])
    return {
        "codex_toml": {
            "path": "~/.codex/config.toml",
            "snippet": '[mcp_servers.playwright]\ncommand = "npx"\nargs = '
            + json.dumps(args, ensure_ascii=True),
        },
        "codex_cli": " ".join(["codex", "mcp", "add", "playwright", command] + [f'"{arg}"' if " " in arg else arg for arg in args]),
        "antigravity_raw_config": mcp_config,
        "claude_code_cli": " ".join(["claude", "mcp", "add", "playwright", command] + args),
        "opencode_json": {
            "mcp": {
                "playwright": {
                    "type": "local",
                    "command": [command] + args,
                    "enabled": True,
                }
            }
        },
        "preflight_setup": setup_commands,
    }


def build_frontend_qa_prompt(
    base_url: str,
    checks: list[str],
    *,
    qa_profile: str,
    preflight: dict[str, Any] | None = None,
) -> str:
    checks_text = "\n".join(f"- {check}" for check in checks)
    preflight_status = str((preflight or {}).get("status") or "unknown")
    recommendations = "\n".join(f"- {item}" for item in list((preflight or {}).get("recommendations") or []))
    if not recommendations:
        recommendations = "- Sin acciones previas detectadas."
    if qa_profile == "fast":
        mode_rules = """
Modo FAST:
- No tomes screenshots salvo que haya fallo reproducible.
- Usa un solo viewport desktop 1280x720.
- Prioriza render smoke, textos criticos y errores de consola.
- No hagas revision visual humana/vision si el usuario no la pidio explicitamente.
""".strip()
    elif qa_profile == "strict":
        mode_rules = """
Modo STRICT:
- Ejecuta desktop y movil.
- Toma screenshots/evidencia por vistas criticas cuando aporte valor.
- Revisa interacciones principales, consola, accesibilidad y estado auth si aplica.
- Usa este modo solo para entregables finales o validacion visual estricta.
""".strip()
    else:
        mode_rules = """
Modo STANDARD:
- Toma como maximo 1 screenshot si aporta evidencia.
- Ejecuta desktop y un smoke movil.
- Prioriza accesibilidad, textos criticos y consola.
- No hagas revision visual exhaustiva por slide si no se pidio strict.
""".strip()
    return f"""
Usa Playwright MCP para ejecutar QA frontend perfil `{qa_profile}` sobre {base_url}.

Preflight Playwright: {preflight_status}
Acciones previas sugeridas:
{recommendations}

Reglas:
- Navega a la URL objetivo y toma un snapshot de accesibilidad.
- Usa capacidades `testing` para verificar visibilidad de textos/elementos criticos.
- Revisa errores de consola antes de cerrar.
- Si hay formularios o CTA principal, prueba una interaccion segura sin enviar datos reales.
- Guarda hallazgos, screenshots o trazas en el output-dir del MCP cuando aplique.
- Devuelve resumen, fallos reproducibles, evidencia y recomendaciones.

{mode_rules}

Checklist MCUM:
{checks_text}
""".strip()


def build_frontend_qa_plan(
    project_path: str,
    *,
    base_url: str | None = None,
    target_agent: str = "generic",
    execution_policy: dict[str, Any] | None = None,
    qa_profile: str | None = None,
    task_text: str | None = None,
    headless: bool | None = None,
    browser: str | None = None,
) -> dict[str, Any]:
    selected_profile, profile_reason = infer_frontend_qa_profile(
        task_text=task_text,
        requested_profile=qa_profile,
        execution_policy=execution_policy,
    )
    policy = normalize_frontend_qa_policy(execution_policy, qa_profile=selected_profile)
    detection = detect_frontend_project(project_path, execution_policy=execution_policy)
    resolved_base_url = str(base_url or detection.get("default_base_url"))
    mcp_config = build_playwright_mcp_config(
        str(detection.get("frontend_root") or project_path),
        execution_policy=execution_policy,
        qa_profile=selected_profile,
        headless=headless,
        browser=browser,
    )
    checks = [str(check) for check in list(policy.get("checks") or [])]
    preflight = preflight_playwright_environment(
        str(detection.get("frontend_root") or project_path),
        frontend_root=str(detection.get("frontend_root") or project_path),
        browser=browser or dict(policy.get("mcp") or {}).get("browser"),
    )
    install_notes = build_agent_install_notes(mcp_config, preflight=preflight)
    return {
        "mode": "frontend_qa",
        "provider": "playwright_mcp",
        "qa_profile": selected_profile,
        "profile_reason": profile_reason,
        "status": "ready" if detection.get("found") else "needs_frontend_detection",
        "execution_readiness": preflight.get("status"),
        "target_agent": target_agent,
        "base_url": resolved_base_url,
        "detection": detection,
        "preflight": preflight,
        "mcp_config": mcp_config,
        "mcp_config_path": str(playwright_mcp_config_path(str(detection.get("frontend_root") or project_path))),
        "output_dir": str(playwright_mcp_output_dir(str(detection.get("frontend_root") or project_path))),
        "install_notes": install_notes,
        "checks": checks,
        "qa_prompt": build_frontend_qa_prompt(
            resolved_base_url,
            checks,
            qa_profile=selected_profile,
            preflight=preflight,
        ),
        "prerequisites": [
            "Node.js 18 or newer",
            "MCP client with Playwright server configured",
            "Frontend dev server running at base_url",
            "Run preflight recommendations before strict/browser-heavy QA.",
        ],
        "token_controls": dict(policy.get("token_controls") or {}),
        "source_references": [
            "https://playwright.dev/docs/getting-started-mcp",
            "https://playwright.dev/mcp/capabilities",
            "https://playwright.dev/mcp/configuration/options",
        ],
    }


def write_frontend_qa_config(plan: dict[str, Any]) -> str:
    target = Path(str(plan.get("mcp_config_path"))).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(plan.get("mcp_config") or {}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_dir = Path(str(plan.get("output_dir"))).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return str(target)
