"""Multi-agent registration generators for the MCUM installer.

The MCP server is one stdio bridge; these tests verify each agent's config is
rendered correctly from the single server spec and that writes are additive
(never clobber unrelated config).
"""

from __future__ import annotations

import json

from MCUM import mcum_install, platform_paths


def _spec() -> dict:
    return {
        "command": "node",
        "args": ["C:\\path\\to\\mcum_local_mcp_stdio.mjs"],
        "env": {
            "MCUM_PYTHON": "py",
            "MCUM_EMBEDDING_BACKEND": "onnx",
            "MCUM_PROJECT_PATH": "C:\\ws",
            "MCUM_PROJECT_NAME": "ws",
            "PYTHONIOENCODING": "utf-8",
        },
    }


def test_build_server_spec_has_required_shape() -> None:
    spec = mcum_install.build_server_spec(project_name="demo", embedding_backend="onnx")
    assert spec["command"] == "node"
    assert spec["args"] and spec["args"][0].endswith("mcum_local_mcp_stdio.mjs")
    for key in ("MCUM_PYTHON", "MCUM_EMBEDDING_BACKEND", "MCUM_PROJECT_PATH", "MCUM_PROJECT_NAME"):
        assert key in spec["env"]
    assert spec["env"]["MCUM_EMBEDDING_BACKEND"] == "onnx"
    assert spec["env"]["MCUM_PROJECT_NAME"] == "demo"


def test_render_claude_code_block() -> None:
    block = mcum_install.render_claude_code(_spec())
    assert block["mcpServers"]["mcum"]["command"] == "node"


def test_render_opencode_block_is_local_stdio() -> None:
    block = mcum_install.render_opencode(_spec())
    server = block["mcp"]["mcum"]
    assert server["type"] == "local"
    assert server["command"][0] == "node"
    assert server["enabled"] is True
    assert server["environment"]["MCUM_EMBEDDING_BACKEND"] == "onnx"


def test_render_codex_toml_escapes_and_sections() -> None:
    toml = mcum_install.render_codex_toml(_spec())
    assert "[mcp_servers.mcum]" in toml
    assert "[mcp_servers.mcum.env]" in toml
    # Windows backslashes must be escaped for valid TOML.
    assert "\\\\path\\\\to" in toml


def test_render_generic_is_single_launch_line() -> None:
    line = mcum_install.render_generic(_spec())
    assert "node" in line
    assert "MCUM_EMBEDDING_BACKEND=onnx" in line


def test_merge_json_preserves_unrelated_keys(tmp_path) -> None:
    target = tmp_path / "opencode.json"
    target.write_text(
        json.dumps({"$schema": "x", "skills": {"paths": [".agent/skills"]}}),
        encoding="utf-8",
    )
    mcum_install._merge_json(target, mcum_install.render_opencode(_spec()))
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["$schema"] == "x"
    assert data["skills"]["paths"] == [".agent/skills"]  # untouched
    assert data["mcp"]["mcum"]["type"] == "local"  # added


def test_workspace_override_targets_external_folder(tmp_path) -> None:
    # Portability: registering for a folder outside this repo must point both the
    # config target and the spec's MCUM_PROJECT_PATH at that external folder.
    from pathlib import Path

    ext = tmp_path / "external_project"
    ext.mkdir()
    spec = mcum_install.build_server_spec(
        workspace_path=ext, project_name="ExternalDemo", embedding_backend="onnx"
    )
    assert spec["env"]["MCUM_PROJECT_PATH"] == str(ext.resolve())
    assert spec["env"]["MCUM_PROJECT_NAME"] == "ExternalDemo"
    target = mcum_install.agent_target("claude-code", workspace=ext)
    assert target == ext / ".mcp.json"
    # The bridge path stays absolute (it lives in the MCUM install, not the ext folder).
    assert Path(spec["args"][0]).name == "mcum_local_mcp_stdio.mjs"


def test_platform_paths_detect_has_required_keys() -> None:
    info = platform_paths.detect()
    for key in ("os", "arch", "py_launcher", "pg_data_dir", "model_cache_dir",
                "pgvector_ext", "embedded_pg_pkg"):
        assert key in info
    assert info["pgvector_ext"] in {"vector.dll", "vector.so", "vector.dylib"}
    assert info["embedded_pg_pkg"].startswith("@embedded-postgres/")


def test_gen_env_writes_default_password(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcum_install, "MCUM_ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "DB_PASSWORD=change-me\n"
        "DATABASE_URL=postgresql://postgres:change-me@localhost:5432/postgres\n"
        "MCUM_EMBEDDING_CACHE_DIR=~/.cache/fastembed\n",
        encoding="utf-8",
    )
    env = mcum_install.gen_env(password="admin1234")
    text = env.read_text(encoding="utf-8")
    assert "DB_PASSWORD=admin1234" in text
    assert "postgres:admin1234@localhost" in text
    assert "change-me" not in text


def test_gen_env_handles_windows_backslash_cache_dir(tmp_path, monkeypatch) -> None:
    # Regression: a Windows cache path (C:\Users\...) in the re.sub REPLACEMENT
    # used to raise re.error 'bad escape \\U'. _set_env_var must handle it.
    monkeypatch.setattr(mcum_install, "MCUM_ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "DB_PASSWORD=change-me\n"
        "DATABASE_URL=postgresql://postgres:change-me@localhost:5432/postgres\n"
        "MCUM_EMBEDDING_CACHE_DIR=~/.cache/fastembed\n",
        encoding="utf-8",
    )
    win_cache = r"C:\Users\dev\AppData\Local\mcum\cache\fastembed"
    monkeypatch.setattr(mcum_install.platform_paths, "detect", lambda: {"model_cache_dir": win_cache})
    env = mcum_install.gen_env(password="admin1234")  # must NOT raise re.error
    text = env.read_text(encoding="utf-8")
    assert win_cache in text
    assert "DB_PASSWORD=admin1234" in text
    assert "postgres:admin1234@localhost" in text


def test_is_ephemeral_install_detects_npx_cache(monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.setattr(
        mcum_install, "MCUM_ROOT",
        Path(r"C:\Users\dev\AppData\Local\npm-cache\_npx\abc\node_modules\mcum-orchestrator"),
    )
    assert mcum_install._is_ephemeral_install() is True
    monkeypatch.setattr(mcum_install, "MCUM_ROOT", Path(r"C:\Users\dev\MCUM"))
    assert mcum_install._is_ephemeral_install() is False


def test_db_reachable_returns_fast_ascii_reason_when_psycopg_missing(monkeypatch) -> None:
    # When psycopg is absent the preflight must return (False, reason) instantly,
    # never hang. (Full auth-failure path is covered by manual/integration runs.)
    import builtins

    real_import = builtins.__import__

    def _fail_psycopg(name, *a, **k):
        if name == "psycopg":
            raise ImportError("no psycopg")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fail_psycopg)
    ok, reason = mcum_install.db_reachable()
    assert ok is False
    assert reason == reason.encode("ascii", "replace").decode("ascii")  # ascii-safe


def test_find_free_port_returns_valid_port() -> None:
    port = mcum_install._find_free_port(5432)
    assert isinstance(port, int)
    assert 5432 <= port <= 5499


def test_try_docker_provision_noops_without_docker(monkeypatch) -> None:
    # Without the docker CLI it must return (False, reason) instantly, never hang.
    monkeypatch.setattr(mcum_install.shutil, "which", lambda _name: None)
    ok, detail = mcum_install._try_docker_provision()
    assert ok is False
    assert "docker" in detail.lower()


def test_set_env_var_appends_when_missing() -> None:
    out = mcum_install._set_env_var("DB_HOST=localhost\n", "DB_PASSWORD", "admin1234")
    assert "DB_PASSWORD=admin1234" in out
    assert "DB_HOST=localhost" in out


def test_gen_env_never_clobbers_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcum_install, "MCUM_ROOT", tmp_path)
    (tmp_path / ".env").write_text("DB_PASSWORD=keepme\n", encoding="utf-8")
    env = mcum_install.gen_env(password="admin1234")
    assert "keepme" in env.read_text(encoding="utf-8")  # untouched


def test_select_agents_auto_dedupes_antigravity(monkeypatch) -> None:
    monkeypatch.setattr(
        mcum_install,
        "detect_installed_agents",
        lambda: {"claude-code": "x", "antigravity": "y", "codex": "z"},
    )
    agents = mcum_install._select_agents(False, True, None)
    # claude-code + antigravity share .mcp.json -> antigravity dropped.
    assert "claude-code" in agents and "codex" in agents
    assert "antigravity" not in agents


def test_select_agents_all_and_single() -> None:
    assert set(mcum_install._select_agents(True, False, None)) == set(mcum_install.SUPPORTED_AGENTS)
    assert mcum_install._select_agents(False, False, "codex") == ["codex"]


def test_append_toml_block_is_idempotent(tmp_path) -> None:
    target = tmp_path / "config.toml"
    block = mcum_install.render_codex_toml(_spec())
    assert mcum_install._append_toml_block(target, block) is True
    assert mcum_install._append_toml_block(target, block) is False  # not duplicated
    assert target.read_text(encoding="utf-8").count("[mcp_servers.mcum]") == 1
