from __future__ import annotations

import json
from pathlib import Path

from MCUM.core.frontend_qa import (
    build_frontend_qa_plan,
    detect_frontend_project,
    infer_frontend_qa_profile,
    write_frontend_qa_config,
)


def _write_package_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_detect_frontend_project_and_build_playwright_mcp_config(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "frontend-demo",
            "scripts": {
                "dev": "vite --host 0.0.0.0 --port 8787",
                "build": "vite build",
            },
            "dependencies": {
                "@vitejs/plugin-react": "latest",
                "vite": "latest",
                "react": "latest",
            },
        },
    )

    detection = detect_frontend_project(str(tmp_path))
    plan = build_frontend_qa_plan(str(tmp_path), target_agent="codex", qa_profile="standard")
    config = plan["mcp_config"]["mcpServers"]["playwright"]
    args = config["args"]

    assert detection["found"] is True
    assert detection["framework"] == "vite"
    assert detection["default_base_url"] == "http://localhost:8787"
    assert plan["status"] == "ready"
    assert plan["qa_profile"] == "standard"
    assert plan["base_url"] == "http://localhost:8787"
    assert config["command"] == "npx"
    assert "@playwright/mcp@latest" in args
    assert "--headless" in args
    assert "--caps=testing,storage" in args
    assert "--isolated" in args
    assert "--output-mode" in args
    assert "file" in args
    assert "--test-id-attribute" in args
    assert "data-testid" in args
    assert "codex_toml" in plan["install_notes"]
    assert "console_error_scan" in plan["checks"]
    assert "preflight" in plan
    assert plan["execution_readiness"] in {
        "ready",
        "blocked",
        "needs_browser_install",
        "mcp_ready_local_package_missing",
    }


def test_write_frontend_qa_config_creates_mcum_artifact(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "next-demo",
            "scripts": {"dev": "next dev"},
            "dependencies": {"next": "latest", "react": "latest"},
        },
    )
    plan = build_frontend_qa_plan(str(tmp_path), base_url="http://localhost:3001", target_agent="antigravity")

    config_path = write_frontend_qa_config(plan)
    saved = json.loads(Path(config_path).read_text(encoding="utf-8"))

    assert Path(config_path).name == "playwright-mcp.json"
    assert Path(config_path).parent.name == ".mcum"
    assert saved == plan["mcp_config"]
    assert Path(plan["output_dir"]).exists()
    assert plan["base_url"] == "http://localhost:3001"


def test_frontend_qa_infers_standard_profile_for_frontend_checks(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "light-demo",
            "scripts": {"dev": "vite --port 5173"},
            "dependencies": {"vite": "latest", "react": "latest"},
        },
    )

    plan = build_frontend_qa_plan(str(tmp_path), target_agent="codex", task_text="QA rapido del frontend")
    args = plan["mcp_config"]["mcpServers"]["playwright"]["args"]

    assert plan["qa_profile"] == "standard"
    assert plan["profile_reason"] == "inferred_standard_visual_or_frontend"
    assert len(plan["checks"]) == 5
    assert plan["token_controls"]["max_screenshots"] == 1
    assert "--caps=testing,storage" in args


def test_frontend_qa_fast_profile_is_explicitly_cheapest(tmp_path: Path) -> None:
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "fast-demo",
            "scripts": {"dev": "vite --port 5173"},
            "dependencies": {"vite": "latest"},
        },
    )

    plan = build_frontend_qa_plan(str(tmp_path), qa_profile="fast")
    args = plan["mcp_config"]["mcpServers"]["playwright"]["args"]

    assert plan["qa_profile"] == "fast"
    assert plan["checks"] == ["render_smoke", "critical_text_visible", "console_error_scan"]
    assert "--caps=testing" in args
    assert "--caps=testing,storage" not in args
    assert plan["token_controls"]["max_screenshots"] == 0
    assert "No tomes screenshots salvo que haya fallo reproducible" in plan["qa_prompt"]


def test_frontend_qa_infers_strict_only_for_final_visual_validation() -> None:
    profile, reason = infer_frontend_qa_profile("validacion visual final con PDF exportado", "auto")

    assert profile == "strict"
    assert reason == "inferred_strict_visual_final"


def test_frontend_qa_plan_handles_missing_frontend_without_crashing(tmp_path: Path) -> None:
    plan = build_frontend_qa_plan(str(tmp_path), target_agent="generic")

    assert plan["status"] == "needs_frontend_detection"
    assert plan["detection"]["found"] is False
    assert plan["mcp_config"]["mcpServers"]["playwright"]["command"] == "npx"
    assert "Run preflight recommendations before strict/browser-heavy QA." in plan["prerequisites"]
