"""
MCUM universal installer & multi-agent registrar.

MCUM is a standard stdio MCP server (30 tools) backed entirely by PostgreSQL +
pgvector. Any MCP-capable agent can drive it; this CLI derives all paths from
its own location (nothing hardcoded) and writes the correct config for each
agent so installation is one command per agent.

Usage:
    py mcum_install.py doctor                 # check deps / DB / model
    py mcum_install.py list-agents            # supported agents + config targets
    py mcum_install.py register --agent claude-code [--scope project|user]
    py mcum_install.py register --agent codex
    py mcum_install.py register --agent opencode
    py mcum_install.py register --all         # every detected agent
    py mcum_install.py print --agent <name>   # print snippet, write nothing
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:  # platform-aware paths (works both as module and script)
    from . import platform_paths  # type: ignore
except ImportError:  # pragma: no cover - direct-script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import platform_paths  # type: ignore

MCUM_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = MCUM_ROOT.parents[2] if len(MCUM_ROOT.parents) >= 3 else MCUM_ROOT.parent
BRIDGE = MCUM_ROOT / "integrations" / "antigravity" / "mcum_local_mcp_stdio.mjs"
SERVER_KEY = "mcum"

# Make `from MCUM...` resolve even when the install folder is not named "MCUM"
# (npm/npx cache dirs, pip installs). Register an alias package pointing here.
if MCUM_ROOT.name != "MCUM" and "MCUM" not in sys.modules:
    import types as _types

    _mcum_alias = _types.ModuleType("MCUM")
    _mcum_alias.__path__ = [str(MCUM_ROOT)]
    sys.modules["MCUM"] = _mcum_alias
    if str(MCUM_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(MCUM_ROOT.parent))

SUPPORTED_AGENTS = ("claude-code", "codex", "opencode", "antigravity", "openclaw", "generic")


# ─────────────────────────────────────────────────────────────────────────────
# Server spec — single source of truth, rendered into each agent's format.
# ─────────────────────────────────────────────────────────────────────────────
def default_python() -> str:
    return "py" if platform.system() == "Windows" else "python3"


def build_server_spec(
    *,
    mcum_root: Path = MCUM_ROOT,
    workspace_path: Path = WORKSPACE_ROOT,
    project_name: str | None = None,
    embedding_backend: str = "onnx",
    cache_dir: str | None = None,
    python_exe: str | None = None,
) -> dict:
    """Canonical stdio MCP launch spec for the MCUM bridge."""
    bridge = (mcum_root / "integrations" / "antigravity" / "mcum_local_mcp_stdio.mjs").resolve()
    name = project_name or workspace_path.name or "workspace"
    cache = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "fastembed")
    return {
        "command": "node",
        "args": [str(bridge)],
        "env": {
            "MCUM_PYTHON": python_exe or default_python(),
            "MCUM_EMBEDDING_BACKEND": embedding_backend,
            "MCUM_EMBEDDING_CACHE_DIR": cache,
            "MCUM_PROJECT_PATH": str(workspace_path.resolve()),
            "MCUM_PROJECT_NAME": name,
            "PYTHONIOENCODING": "utf-8",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-agent renderers (pure: spec -> config fragment). Testable in isolation.
# ─────────────────────────────────────────────────────────────────────────────
def render_claude_code(spec: dict) -> dict:
    """Claude Code / Antigravity `.mcp.json` style mcpServers block."""
    return {"mcpServers": {SERVER_KEY: spec}}


def render_opencode(spec: dict) -> dict:
    """OpenCode `opencode.json` `mcp` block (local stdio server)."""
    return {
        "mcp": {
            SERVER_KEY: {
                "type": "local",
                "command": [spec["command"], *spec["args"]],
                "environment": dict(spec["env"]),
                "enabled": True,
            }
        }
    }


def render_codex_toml(spec: dict) -> str:
    """Codex `~/.codex/config.toml` `[mcp_servers.mcum]` block."""
    def _toml_str(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [f"[mcp_servers.{SERVER_KEY}]"]
    lines.append(f"command = {_toml_str(spec['command'])}")
    args = ", ".join(_toml_str(a) for a in spec["args"])
    lines.append(f"args = [{args}]")
    lines.append("")
    lines.append(f"[mcp_servers.{SERVER_KEY}.env]")
    for key, value in spec["env"].items():
        lines.append(f"{key} = {_toml_str(str(value))}")
    return "\n".join(lines) + "\n"


def render_generic(spec: dict) -> str:
    """Human-readable stdio launch line for any other MCP client."""
    env_pairs = " ".join(f"{k}={v}" for k, v in spec["env"].items())
    return f"{env_pairs} {spec['command']} {' '.join(spec['args'])}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Config write targets + safe merge.
# ─────────────────────────────────────────────────────────────────────────────
def _codex_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".codex" / "config.toml"


def agent_target(agent: str, scope: str = "project", workspace: Path = WORKSPACE_ROOT) -> Path | None:
    if agent in {"claude-code", "antigravity"}:
        return (workspace / ".mcp.json") if scope == "project" else (
            Path(os.path.expanduser("~")) / ".claude.json"
        )
    if agent == "opencode":
        return workspace / "opencode.json"
    if agent == "codex":
        return _codex_config_path()
    return None  # openclaw uses a python bridge; generic prints only


def _merge_json(target: Path, fragment: dict) -> None:
    existing: dict = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
    for top_key, block in fragment.items():
        node = existing.setdefault(top_key, {})
        if isinstance(node, dict):
            node.update(block)
        else:
            existing[top_key] = block
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def _append_toml_block(target: Path, block: str) -> bool:
    """Append the codex block if not already present. Returns True if written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if f"[mcp_servers.{SERVER_KEY}]" in existing:
        return False
    sep = "" if existing.endswith("\n") or not existing else "\n"
    target.write_text(existing + sep + "\n" + block, encoding="utf-8")
    return True


def register_agent(agent: str, spec: dict, scope: str = "project", workspace: Path = WORKSPACE_ROOT) -> dict:
    if agent in {"claude-code", "antigravity"}:
        target = agent_target(agent, scope, workspace)
        _merge_json(target, render_claude_code(spec))
        return {"agent": agent, "written": str(target), "status": "ok"}
    if agent == "opencode":
        target = agent_target(agent, workspace=workspace)
        _merge_json(target, render_opencode(spec))
        return {"agent": agent, "written": str(target), "status": "ok"}
    if agent == "codex":
        target = agent_target(agent, workspace=workspace)
        wrote = _append_toml_block(target, render_codex_toml(spec))
        return {
            "agent": agent,
            "written": str(target),
            "status": "ok" if wrote else "already_present",
        }
    if agent == "openclaw":
        bridge = MCUM_ROOT / "integrations" / "openclaw" / "openclaw_bridge.py"
        return {
            "agent": agent,
            "written": None,
            "status": "manual",
            "hint": f"OpenClaw calls the python bridge directly: {spec['env']['MCUM_PYTHON']} {bridge}",
        }
    # generic
    return {"agent": agent, "written": None, "status": "manual", "snippet": render_generic(spec)}


# ─────────────────────────────────────────────────────────────────────────────
# doctor
# ─────────────────────────────────────────────────────────────────────────────
def _spec_value(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def doctor() -> int:
    print("MCUM doctor\n" + "-" * 40)
    ok = True
    print(f"MCUM root        : {MCUM_ROOT}")
    print(f"workspace root   : {WORKSPACE_ROOT}")
    print(f"bridge present   : {BRIDGE.exists()}")
    ok = ok and BRIDGE.exists()
    node = shutil.which("node")
    print(f"node             : {node or 'NOT FOUND'}")
    ok = ok and bool(node)
    for mod, required in (("psycopg", True), ("pgvector", True), ("fastembed", False),
                          ("sentence_transformers", False)):
        present = _spec_value(mod)
        tag = "" if present else ("  <-- required" if required else "  (optional)")
        print(f"py: {mod:22} {'ok' if present else 'missing'}{tag}")
        ok = ok and (present or not required)
    # DB reachability — single fast attempt (no pool, no retries, no log spam).
    db_ok, db_reason = db_reachable()
    if db_ok:
        print("PostgreSQL       : reachable")
    else:
        print(f"PostgreSQL       : NOT reachable ({db_reason})")
        ok = False
    print("-" * 40)
    print("READY" if ok else "INCOMPLETE - see items above")
    if not db_ok:
        _print_db_help(db_reason)
    return 0 if ok else 1


# ─────────────────────────────────────────────────────────────────────────────
# Agent auto-detection + .env generation + seed (public `init` flow).
# ─────────────────────────────────────────────────────────────────────────────
def detect_installed_agents() -> dict:
    """Return {agent: reason} for agents actually present on this system."""
    home = Path(os.path.expanduser("~"))
    found: dict[str, str] = {}
    if shutil.which("claude") or (home / ".claude.json").exists() or (home / ".claude").is_dir():
        found["claude-code"] = "claude CLI / ~/.claude"
    if shutil.which("codex") or (home / ".codex").is_dir():
        found["codex"] = "codex CLI / ~/.codex"
    if shutil.which("opencode") or (WORKSPACE_ROOT / "opencode.json").exists():
        found["opencode"] = "opencode CLI / opencode.json"
    antigravity_markers = (
        ".antigravity",
        "AppData/Local/Antigravity",
        "AppData/Roaming/Antigravity",
        "Library/Application Support/Antigravity",
        ".config/Antigravity",
    )
    if shutil.which("antigravity") or any((home / m).exists() for m in antigravity_markers):
        found["antigravity"] = "antigravity install"
    if shutil.which("openclaw") or shutil.which("openclaw.exe"):
        found["openclaw"] = "openclaw CLI"
    return found


def _set_env_var(text: str, key: str, value: str) -> str:
    """Set or append KEY=value in a .env body.

    Uses a callable replacement so backslashes in `value` (Windows paths like
    C:\\Users\\...) are NOT interpreted as regex escapes — a plain re.sub repl
    string would raise 'bad escape \\U'.
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        return pattern.sub(lambda _m: f"{key}={value}", text)
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}{key}={value}\n"


def gen_env(password: str = "admin1234", *, force: bool = False) -> Path:
    """Generate `.env` from `.env.example` with a safe default password.

    Never clobbers an existing `.env` unless force=True (protects a real local
    config). The public release ships only `.env.example`; first `init` writes
    `.env` with DB_PASSWORD=admin1234 for the user to change later.
    """
    env_path = MCUM_ROOT / ".env"
    if env_path.exists() and not force:
        return env_path
    example = MCUM_ROOT / ".env.example"
    text = example.read_text(encoding="utf-8") if example.exists() else (
        "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=postgres\nDB_USER=postgres\n"
        "DB_PASSWORD=admin1234\nDATABASE_URL=postgresql://postgres:admin1234@localhost:5432/postgres\n"
    )
    info = platform_paths.detect()
    text = _set_env_var(text, "DB_PASSWORD", password)
    text = _set_env_var(
        text, "DATABASE_URL", f"postgresql://postgres:{password}@localhost:5432/postgres"
    )
    text = _set_env_var(text, "MCUM_EMBEDDING_CACHE_DIR", str(info["model_cache_dir"]))
    env_path.write_text(text, encoding="utf-8")
    return env_path


def apply_seed(dry_run: bool = False) -> int:
    """Apply the minimal seed (example skills only) if present."""
    seed = MCUM_ROOT / "db" / "seed_minimal.sql"
    if not seed.exists():
        print("  (no seed_minimal.sql; skipping)")
        return 0
    print(f"  applying {seed.name}")
    if dry_run:
        return 0
    try:
        sys.path.insert(0, str(MCUM_ROOT.parent))
        from MCUM.db.connection import get_db, get_cursor

        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(seed.read_text(encoding="utf-8"))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  ! seed failed: {type(exc).__name__}: {exc}")
        return 1


def _is_ephemeral_install() -> bool:
    """True if MCUM is running from an npx/npm cache (a temporary location).

    Registering agents against such a path is a footgun: the cache is wiped, so
    the agent configs would point at a dead path. We warn and skip registration.
    """
    p = str(MCUM_ROOT).replace("\\", "/").lower()
    return "/_npx/" in p or "/npm-cache/" in p or "/_cacache/" in p


def _read_env_file(env_path: Path) -> dict:
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    return values


def db_reachable(timeout: int = 4) -> tuple[bool, str]:
    """Single fast connection attempt (no pool, no retries, no log spam).

    Returns (ok, reason). Used as a preflight so init fails fast with a clear
    message instead of a 30s PoolTimeout and dozens of repeated pool errors.
    """
    try:
        import psycopg
    except ImportError:
        return False, "psycopg not installed yet (run dependency install first)"
    values = _read_env_file(MCUM_ROOT / ".env")
    url = values.get("DATABASE_URL") or os.getenv("DATABASE_URL")
    utf8 = "-c client_encoding=UTF8"  # Windows clusters default to WIN1252.
    try:
        if url:
            conn = psycopg.connect(url, connect_timeout=timeout, options=utf8)
        else:
            conn = psycopg.connect(
                host=values.get("DB_HOST", "localhost"),
                port=values.get("DB_PORT", "5432"),
                dbname=values.get("DB_NAME", "postgres"),
                user=values.get("DB_USER", "postgres"),
                password=values.get("DB_PASSWORD", ""),
                connect_timeout=timeout,
                options=utf8,
            )
        conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        line = (str(exc).strip().splitlines() or [""])[0]
        # Sanitize to ASCII: psql/psycopg errors carry accented text that would
        # raise UnicodeEncodeError on a Windows cp1252 console.
        line = line.encode("ascii", "replace").decode("ascii")
        return False, line or type(exc).__name__


def _find_free_port(preferred: int = 5432) -> int:
    """Return a free TCP port, preferring `preferred` (avoids clashing with an
    existing PostgreSQL already bound to 5432)."""
    import socket

    for port in [preferred, *range(5433, 5500)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:  # nothing listening
                return port
    return preferred


def _try_docker_provision(password: str = "admin1234") -> tuple[bool, str]:
    """Auto-provision a pgvector PostgreSQL via docker compose so the install
    ends AUTHENTICATED with zero manual steps. Returns (ok, detail)."""
    import time

    if not shutil.which("docker"):
        return False, "docker not available"
    compose = MCUM_ROOT / "docker-compose.yml"
    if not compose.exists():
        return False, "docker-compose.yml missing"
    port = _find_free_port(5432)
    # Point .env at the provisioned DB (matching creds) BEFORE bringing it up.
    env_path = MCUM_ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    text = _set_env_var(text, "DB_PORT", str(port))
    text = _set_env_var(text, "DB_PASSWORD", password)
    text = _set_env_var(
        text, "DATABASE_URL", f"postgresql://postgres:{password}@localhost:{port}/postgres"
    )
    env_path.write_text(text, encoding="utf-8")
    child_env = {
        **os.environ,
        "MCUM_DB_PORT": str(port),
        "MCUM_DB_PASSWORD": password,
        "MCUM_DB_USER": "postgres",
        "MCUM_DB_NAME": "postgres",
    }
    print(f"  auto-provisioning PostgreSQL+pgvector via Docker on port {port} ...")
    import subprocess

    try:
        subprocess.call(
            ["docker", "compose", "-f", str(compose), "up", "-d"],
            cwd=str(MCUM_ROOT),
            env=child_env,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"docker compose failed: {exc}"
    # Wait until it accepts authenticated connections (schema auto-applies on init).
    for _ in range(30):
        ok, _reason = db_reachable(timeout=2)
        if ok:
            return True, f"docker (port {port})"
        time.sleep(2)
    return False, "docker DB did not become ready in time"


def _try_embedded_provision(workspace: Path, password: str = "admin1234") -> tuple[bool, str]:
    """Zero-prerequisite fallback: download & run a PORTABLE PostgreSQL (no install,
    no Docker, no admin), create a UTF8 database for this workspace, point .env at
    it, and flag MCUM_DB_EMBEDDED so the bridge keeps it running. JSONB mode (ONNX
    embeddings still work; only the HNSW index is absent)."""
    import subprocess
    import time

    bridge = MCUM_ROOT / "db" / "embedded_pg.mjs"
    if not bridge.exists() or not shutil.which("node"):
        return False, "node/embedded_pg.mjs unavailable"
    # Ensure the embedded-postgres dependency is installed at the package root
    # (the relocate copies without node_modules, and bootstrap only installs the
    # bridge deps — so install the root optionalDependency on demand here).
    if not (MCUM_ROOT / "node_modules" / "embedded-postgres").exists():
        npm = shutil.which("npm") or "npm"
        print("  installing embedded-postgres (one-time) ...")
        _run([npm, "install", "embedded-postgres@^18.4.0-beta.17"], cwd=MCUM_ROOT)
        if not (MCUM_ROOT / "node_modules" / "embedded-postgres").exists():
            return False, "could not install embedded-postgres"
    port = _find_free_port(5432)
    data_dir = str(MCUM_ROOT / "pgdata")
    db_name = "mcum_" + (re.sub(r"[^a-z0-9_]", "_", workspace.name.lower()) or "ws")
    child_env = {
        **os.environ,
        "MCUM_DB_PORT": str(port),
        "MCUM_DB_PASSWORD": password,
        "MCUM_PG_DATA_DIR": data_dir,
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    }
    print(f"  starting embedded PostgreSQL on port {port} (persistent, no window) ...")
    # pg_ctl start launches postgres as a detached background process and returns
    # (no hang, no window). The server PERSISTS after this installer exits.
    run_kwargs: dict = {"env": child_env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        subprocess.run(["node", str(bridge), "start"], timeout=150, **run_kwargs)
    except Exception as exc:  # noqa: BLE001
        return False, f"embedded start failed: {exc}"
    # Wait until the server (default 'postgres' db) accepts connections.
    base_url = f"postgresql://postgres:{password}@localhost:{port}/postgres"
    import psycopg

    ready = False
    for _ in range(30):
        try:
            psycopg.connect(base_url, connect_timeout=2, options="-c client_encoding=UTF8").close()
            ready = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    if not ready:
        return False, "embedded server did not accept connections"
    # Create the UTF8 database + point .env at it, and flag it embedded.
    apply_db_args(host="localhost", port=str(port), name=db_name, user="postgres",
                  password=password, create=True)
    env_path = MCUM_ROOT / ".env"
    text = env_path.read_text(encoding="utf-8")
    text = _set_env_var(text, "MCUM_DB_EMBEDDED", "1")
    text = _set_env_var(text, "MCUM_PG_DATA_DIR", data_dir)
    env_path.write_text(text, encoding="utf-8")
    ok, reason = db_reachable()
    return (ok, f"embedded (port {port}, JSONB)") if ok else (False, reason)


def _print_db_help(reason: str) -> None:
    # ASCII + bilingual (EN/ES) so it renders cleanly on any console.
    print("  ! PostgreSQL not reachable / no disponible. Detail: " + reason)
    print("  EN: MCUM is installed, but the database was NOT initialized.")
    print("  ES: MCUM quedo instalado, pero la base de datos NO se inicializo.")
    print("  Pick ONE option / Elige UNA opcion, then re-run / luego repite 'mcum init':")
    print(f"    1) Docker:        cd \"{MCUM_ROOT}\" && docker compose up -d")
    print(f"    2) PostgreSQL:    edit/edita \"{MCUM_ROOT / '.env'}\" -> DB_PASSWORD")
    print(f"    3) Embedded:      node \"{MCUM_ROOT / 'db' / 'embedded_pg.mjs'}\" start")


# ─────────────────────────────────────────────────────────────────────────────
# bootstrap — one command that installs every layer end to end.
# ─────────────────────────────────────────────────────────────────────────────
def _run(cmd: list[str], cwd: Path | None = None, dry_run: bool = False) -> int:
    import subprocess

    printable = " ".join(str(c) for c in cmd)
    print(f"  $ {printable}" + (f"   (cwd={cwd})" if cwd else ""))
    if dry_run:
        return 0
    try:
        return subprocess.call(cmd, cwd=str(cwd) if cwd else None)
    except FileNotFoundError as exc:
        print(f"    ! command not found: {exc}")
        return 1


def _select_agents(register_all: bool, register_auto: bool, agent: str | None) -> list[str]:
    if register_auto:
        detected = detect_installed_agents()
        if detected:
            print("  detected: " + ", ".join(f"{a} ({why})" for a, why in detected.items()))
            # claude-code and antigravity share .mcp.json; dedupe the target.
            agents = list(detected.keys())
            if "claude-code" in agents and "antigravity" in agents:
                agents.remove("antigravity")
            return agents
        print("  no known agents detected; defaulting to claude-code")
        return ["claude-code"]
    if register_all:
        return list(SUPPORTED_AGENTS)
    return [agent] if agent else ["claude-code"]


def bootstrap(
    *,
    agent: str | None = None,
    register_all: bool = False,
    register_auto: bool = False,
    write_env: bool = False,
    with_seed: bool = False,
    with_model: bool = False,
    skip_npm: bool = False,
    skip_deps: bool = False,
    skip_schema: bool = False,
    auto_db: bool = False,
    workspace: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Install every MCUM layer (env + pip + npm + schema + seed + register) in order."""
    ws = workspace or WORKSPACE_ROOT
    py = sys.executable or default_python()
    npm = shutil.which("npm") or "npm"
    pip_cmd = [py, "-m", "pip", "install", "-r", str(MCUM_ROOT / "requirements.txt")]
    bridge_dir = BRIDGE.parent
    print("MCUM bootstrap" + ("  (dry-run)" if dry_run else "") + "\n" + "-" * 40)
    rc = 0
    ephemeral = _is_ephemeral_install()
    if ephemeral:
        print("  NOTE: running from a temporary npx/npm cache. For a PERMANENT install use:")
        print("    Windows:     irm https://raw.githubusercontent.com/andrewcharly1/MCUM/main/install.ps1 | iex")
        print("    Linux/macOS: curl -fsSL https://raw.githubusercontent.com/andrewcharly1/MCUM/main/install.sh | sh")
        print("  Continuing, but agent registration is SKIPPED (this path would not persist).")

    if write_env:
        print("[0] Environment file (.env with default password)")
        if dry_run:
            print("  $ (generate .env from .env.example, DB_PASSWORD=admin1234 if missing)")
        else:
            env_path = gen_env()
            print(f"  {'kept existing' if env_path.exists() else 'wrote'} {env_path}")
    if not skip_deps:
        print("[1/6] Python dependencies (psycopg, pgvector, fastembed)")
        rc |= _run(pip_cmd, dry_run=dry_run)
    if not skip_npm:
        print("[2/6] Node bridge dependencies (@modelcontextprotocol/sdk)")
        rc |= _run([npm, "install"], cwd=bridge_dir, dry_run=dry_run)
    db_blocked = False
    if not skip_schema:
        print("[3/6] PostgreSQL schema (schemas + pgvector + HNSW)")
        if dry_run:
            print(f"  $ {py} {MCUM_ROOT / 'install_schema.py'}  (+ seed)")
        else:
            db_ok, db_reason = db_reachable()
            if not db_ok and auto_db:
                # Automatic, zero-prerequisite chain so init ALWAYS ends authenticated:
                #   Docker (pgvector, fast) -> embedded portable PostgreSQL (JSONB).
                prov_ok, prov_detail = _try_docker_provision()
                if not prov_ok:
                    print(f"  Docker path skipped: {prov_detail}; trying embedded PostgreSQL ...")
                    prov_ok, prov_detail = _try_embedded_provision(ws)
                if prov_ok:
                    print(f"  authenticated via {prov_detail}")
                    db_ok = True
                else:
                    print(f"  auto-provision failed: {prov_detail}")
            if db_ok:
                rc |= _run([py, str(MCUM_ROOT / "install_schema.py")], dry_run=dry_run)
                if with_seed:
                    print("      + minimal seed (example skills, no personal data)")
                    rc |= apply_seed(dry_run=dry_run)
            else:
                # Poka-yoke: fail fast with guidance instead of a 30s PoolTimeout + spam.
                _print_db_help(db_reason)
                db_blocked = True
    if with_model:
        print("[4/6] One-time embedding model download")
        env_cmd = [py, str(MCUM_ROOT / "mcum_embedding.py"), "bench", "onnx"]
        if dry_run:
            print(f"  $ MCUM_EMBEDDING_ALLOW_DOWNLOAD=1 {' '.join(env_cmd)}")
        else:
            os.environ["MCUM_EMBEDDING_ALLOW_DOWNLOAD"] = "1"
            rc |= _run(env_cmd, dry_run=dry_run)
    else:
        print("[4/6] Model download skipped (use --with-model on a fresh machine)")
    print("[5/6] Health check")
    if not dry_run:
        doctor()
    else:
        print("  $ (doctor)")
    print("[6/6] Register agent(s)")
    if ephemeral and not dry_run:
        print("  skipped (temporary npx cache - do the permanent install to register agents).")
    elif dry_run:
        agents = _select_agents(register_all, register_auto, agent)
        print(f"  $ register -> {', '.join(agents)}")
    else:
        agents = _select_agents(register_all, register_auto, agent)
        spec = build_server_spec(workspace_path=ws)
        for ag in agents:
            result = register_agent(ag, spec, workspace=ws)
            print(f"  {result['agent']:14} {result['status']}" + (f" -> {result['written']}" if result.get("written") else ""))
    print("-" * 40)
    if dry_run:
        print("Dry-run only; nothing changed. / Solo simulacion; nada cambio.")
    elif db_blocked:
        print("Almost there / Casi listo: the DATABASE step is PENDING (see options above).")
        print("ES: el paso de BASE DE DATOS quedo PENDIENTE (ver opciones arriba).")
    else:
        print("Done / Listo. Restart your agent / reinicia tu agente to load the 30 mcum_* tools.")
    return rc


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def relocate_into_workspace(workspace: Path, extra_args: list[str]) -> int:
    """Copy the (ephemeral npx) package into <workspace>/MCUM and re-run init there.

    This is what turns `npx mcum-orchestrator init` into a PERMANENT, in-workspace
    install: agents get registered against a path that persists.
    """
    import shutil
    import subprocess

    target = workspace / "MCUM"
    print(f"  installing permanently into {target} ...")
    ignore = shutil.ignore_patterns(
        "node_modules", ".git", ".env", "__pycache__", ".pytest_cache",
        "*.pyc", "pgdata", ".coverage", ".coverage.*",
    )
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(MCUM_ROOT, target, ignore=ignore)
    reexec = [
        default_python(), str(target / "mcum_install.py"), "init",
        "--installed", "--workspace", str(workspace), *extra_args,
    ]
    print(f"  $ {' '.join(reexec)}")
    return subprocess.call(reexec, cwd=str(workspace))


def apply_db_args(host=None, port=None, name=None, user=None, password=None, create=False) -> None:
    """Write provided DB creds into .env (overriding admin1234 defaults) and,
    if create=True, CREATE DATABASE <name> on the server if it is missing."""
    env_path = MCUM_ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    vals = _read_env_file(env_path)
    host = host or vals.get("DB_HOST", "localhost")
    port = port or vals.get("DB_PORT", "5432")
    name = name or vals.get("DB_NAME", "postgres")
    user = user or vals.get("DB_USER", "postgres")
    password = password if password is not None else vals.get("DB_PASSWORD", "admin1234")
    for key, value in (("DB_HOST", host), ("DB_PORT", port), ("DB_NAME", name), ("DB_USER", user), ("DB_PASSWORD", password)):
        text = _set_env_var(text, key, value)
    text = _set_env_var(text, "DATABASE_URL", f"postgresql://{user}:{password}@{host}:{port}/{name}")
    env_path.write_text(text, encoding="utf-8")
    if create:
        try:
            import psycopg

            with psycopg.connect(host=host, port=port, dbname="postgres", user=user,
                                 password=password, connect_timeout=5, autocommit=True,
                                 options="-c client_encoding=UTF8") as conn:
                exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
                if not exists:
                    # UTF8 + C locale via template0: works on UTF8 clusters AND on
                    # Windows/embedded clusters that default to WIN1252 (the MCUM
                    # schema and data contain UTF-8 text/box-drawing characters).
                    conn.execute(
                        f"CREATE DATABASE \"{name}\" WITH ENCODING 'UTF8' "
                        "TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
                    )
                    print(f"  created database '{name}' (UTF8)")
                else:
                    print(f"  database '{name}' already exists")
        except Exception as exc:  # noqa: BLE001
            line = str(exc).strip().splitlines()[0].encode("ascii", "replace").decode("ascii")
            print(f"  ! could not create database '{name}': {line}")


def interactive_db_setup(workspace: Path) -> bool:
    """Prompt for the PostgreSQL password (twice, validated) and create a
    dedicated database. Bilingual + explicit that the password is to CREATE the DB.
    Returns True if the database ends reachable. No-op if already reachable or
    if there is no interactive terminal."""
    import getpass

    ok, _reason = db_reachable()
    if ok:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False

    db_name = "mcum_" + (re.sub(r"[^a-z0-9_]", "_", workspace.name.lower()) or "ws")
    print("")
    print("  --- PostgreSQL setup / configuracion ---")
    print("  EN: MCUM needs your PostgreSQL password ONLY to CREATE its own database.")
    print("  ES: MCUM necesita tu clave de PostgreSQL SOLO para CREAR su propia base de datos.")
    print(f"  EN: a dedicated, isolated database '{db_name}' will be created.")
    print(f"  ES: se creara una base dedicada y aislada '{db_name}'.")
    for attempt in range(3):
        first = getpass.getpass("  PostgreSQL password / clave de PostgreSQL: ")
        again = getpass.getpass("  Repeat password / repite la clave: ")
        if not first:
            print("  EN: empty password. / ES: clave vacia. Reintenta.")
            continue
        if first != again:
            print("  EN: passwords do not match. / ES: las claves no coinciden. Reintenta.")
            continue
        apply_db_args(name=db_name, password=first, create=True)
        ok, reason = db_reachable()
        if ok:
            print("  EN: database ready and authenticated. / ES: base de datos lista y autenticada.")
            return True
        print(f"  EN: could not connect ({reason}). / ES: no se pudo conectar. Reintenta.")
    print("  EN: skipping DB setup after 3 attempts. / ES: se omite la BD tras 3 intentos.")
    return False


def write_governance(workspace: Path, install_dir: Path) -> None:
    """Write CLAUDE.md + AGENTS.md mandating MCUM for every task in the workspace."""
    body = f"""# Agent Instructions — {workspace}

## MCUM is MANDATORY for every task (auto-activation)

This workspace is governed by **MCUM (mcum-orchestrator)** — an audit, orchestration
and persistent-memory system. **Use it for EVERY task**, no matter how small (even a
single HTML file). Do not skip it as "overkill"; auditing everything is the point.

The MCUM MCP server `mcum` is registered for this workspace (`.mcp.json` / `opencode.json`)
and exposes 30 `mcum_*` tools backed by PostgreSQL.

### Required flow for every task
1. `mcum_prepare_intake` — normalize the objective first.
2. `mcum_search_memory` — load relevant prior experience.
3. Do the work (validate with concrete evidence).
4. `mcum_record_task_result` — persist the result BEFORE answering. Always.
5. If recording fails, say so explicitly; never report "done" without the record.

Opt-out only if the user explicitly says "skip MCUM".

Install: `{install_dir}` · DB config in `{install_dir / '.env'}`.
"""
    (workspace / "AGENTS.md").write_text(body, encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("# Project Configuration\n\n" + body, encoding="utf-8")
    print(f"  wrote CLAUDE.md + AGENTS.md in {workspace}")


def _remove_mcum_from_json(target: Path, top_key: str) -> bool:
    if not target.exists():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return False
    node = data.get(top_key)
    if isinstance(node, dict) and SERVER_KEY in node:
        node.pop(SERVER_KEY, None)
        if not node:
            data.pop(top_key, None)
        # If the file now only had our config, remove it; else rewrite.
        if not data or set(data.keys()) <= {"$schema"}:
            target.unlink()
        else:
            target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    return False


def _remove_codex_mcum() -> bool:
    cfg = _codex_config_path()
    if not cfg.exists():
        return False
    text = cfg.read_text(encoding="utf-8")
    # Drop the [mcp_servers.mcum] and [mcp_servers.mcum.env] blocks.
    out, skip = [], False
    for line in text.splitlines():
        if re.match(r"^\[mcp_servers\.mcum(\.|\])", line.strip()):
            skip = True
            continue
        if skip and line.strip().startswith("[") and not line.strip().startswith("[mcp_servers.mcum"):
            skip = False
        if not skip:
            out.append(line)
    cfg.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    return True


def uninstall(workspace: Path, *, purge_db: bool = True, dry_run: bool = False) -> int:
    """Remove THIS MCUM install: stop the embedded DB, delete its data, unregister
    agents, and delete the install + governance files for the given workspace.
    Only touches this machine/this install."""
    install = (workspace / "MCUM") if (workspace / "MCUM").exists() else MCUM_ROOT
    env = _read_env_file(install / ".env")
    print(f"MCUM uninstall (workspace={workspace}, install={install})" + ("  (dry-run)" if dry_run else ""))

    # 1. Stop the embedded PostgreSQL (if any) and purge its data dir.
    if env.get("MCUM_DB_EMBEDDED") == "1":
        bridge = install / "db" / "embedded_pg.mjs"
        pgdir = env.get("MCUM_PG_DATA_DIR", "")
        print(f"  stopping embedded PostgreSQL ({pgdir or 'default'})")
        if not dry_run and bridge.exists() and shutil.which("node"):
            child_env = {**os.environ, "MCUM_DB_PORT": env.get("DB_PORT", "5432")}
            if pgdir:
                child_env["MCUM_PG_DATA_DIR"] = pgdir
            run_kwargs = {"env": child_env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            try:
                subprocess.run(["node", str(bridge), "stop"], timeout=60, **run_kwargs)
            except Exception:  # noqa: BLE001
                pass

    # 2. Unregister agents for this workspace.
    for rel, key in ((".mcp.json", "mcpServers"), ("opencode.json", "mcp")):
        if not dry_run and _remove_mcum_from_json(workspace / rel, key):
            print(f"  unregistered mcum from {rel}")
    if not dry_run and _remove_codex_mcum():
        print("  unregistered mcum from codex config")

    # 3. Delete governance + install + DB data.
    targets = [workspace / "CLAUDE.md", workspace / "AGENTS.md"]
    if purge_db and env.get("MCUM_PG_DATA_DIR"):
        targets.append(Path(env["MCUM_PG_DATA_DIR"]))
    targets.append(install)  # the MCUM code folder (works when run from npx cache)
    for t in targets:
        print(f"  removing {t}")
        if dry_run:
            continue
        try:
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
            elif t.exists():
                t.unlink()
        except Exception as exc:  # noqa: BLE001
            print(f"    ! could not remove {t} ({exc}); delete it manually.")
    print("-" * 40)
    print("MCUM uninstalled from this workspace. / MCUM desinstalado de este workspace.")
    if install == MCUM_ROOT:
        print(f"NOTE: run from outside to also delete the folder, or: Remove-Item -Recurse -Force \"{install}\"")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCUM universal installer & multi-agent registrar.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check deps, DB, and model availability.")
    sub.add_parser("list-agents", help="List supported agents and their config targets.")
    bs = sub.add_parser("bootstrap", help="One command: install npm + pip + schema + model + register.")
    bs.add_argument("--agent", choices=SUPPORTED_AGENTS, default=None)
    bs.add_argument("--all", action="store_true", help="Register every supported agent.")
    bs.add_argument("--auto", action="store_true", help="Register only detected/installed agents.")
    bs.add_argument("--with-model", action="store_true", help="Download the embedding model (fresh machine).")
    bs.add_argument("--skip-npm", action="store_true")
    bs.add_argument("--skip-deps", action="store_true")
    bs.add_argument("--skip-schema", action="store_true")
    bs.add_argument("--dry-run", action="store_true", help="Print the plan without changing anything.")
    ini = sub.add_parser("init", help="Public one-command setup: install into workspace + DB + seed + auto-register + governance.")
    ini.add_argument("--with-model", action="store_true", help="Also download the embedding model.")
    ini.add_argument("--workspace", default=None, help="Target workspace folder (default: current dir).")
    ini.add_argument("--installed", action="store_true", help="Internal: already running from the permanent install.")
    ini.add_argument("--db-host", default=None)
    ini.add_argument("--db-port", default=None)
    ini.add_argument("--db-name", default=None, help="Use a dedicated database (e.g. mcum_dashboard).")
    ini.add_argument("--db-user", default=None)
    ini.add_argument("--db-password", default=None, help="Your PostgreSQL password (no Docker needed).")
    ini.add_argument("--create-db", action="store_true", help="CREATE the --db-name if it does not exist.")
    ini.add_argument("--db-prompt", action="store_true", help="Prompt for your PostgreSQL password instead of auto-provisioning.")
    ini.add_argument("--dry-run", action="store_true")
    un = sub.add_parser("uninstall", help="Remove this MCUM install: stop embedded DB, delete its data, unregister agents, delete install.")
    un.add_argument("--workspace", default=None, help="Workspace folder (default: current dir).")
    un.add_argument("--keep-db", action="store_true", help="Do NOT delete the embedded database data dir.")
    un.add_argument("--dry-run", action="store_true")
    reg = sub.add_parser("register", help="Write MCP config for an agent.")
    reg.add_argument("--agent", choices=SUPPORTED_AGENTS)
    reg.add_argument("--all", action="store_true", help="Register every supported agent.")
    reg.add_argument("--auto", action="store_true", help="Register only detected/installed agents.")
    reg.add_argument("--scope", choices=["project", "user"], default="project")
    reg.add_argument("--project-name", default=None)
    reg.add_argument("--backend", default="onnx")
    reg.add_argument("--workspace", default=None, help="Target project folder (default: this workspace).")
    pr = sub.add_parser("print", help="Print an agent snippet without writing.")
    pr.add_argument("--agent", choices=SUPPORTED_AGENTS, required=True)
    pr.add_argument("--project-name", default=None)
    pr.add_argument("--backend", default="onnx")
    pr.add_argument("--workspace", default=None, help="Target project folder (default: this workspace).")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor()

    if args.command == "bootstrap":
        return bootstrap(
            agent=args.agent,
            register_all=args.all,
            register_auto=args.auto,
            with_model=args.with_model,
            skip_npm=args.skip_npm,
            skip_deps=args.skip_deps,
            skip_schema=args.skip_schema,
            dry_run=args.dry_run,
        )

    if args.command == "uninstall":
        workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
        return uninstall(workspace, purge_db=not args.keep_db, dry_run=args.dry_run)

    if args.command == "init":
        workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
        # From the ephemeral npx cache, install PERMANENTLY into the workspace, then
        # re-run init there so agents register against a path that persists.
        if _is_ephemeral_install() and not args.installed and not args.dry_run:
            passthrough: list[str] = []
            for flag, val in (("--db-host", args.db_host), ("--db-port", args.db_port),
                              ("--db-name", args.db_name), ("--db-user", args.db_user),
                              ("--db-password", args.db_password)):
                if val:
                    passthrough += [flag, val]
            if args.create_db:
                passthrough.append("--create-db")
            if args.with_model:
                passthrough.append("--with-model")
            return relocate_into_workspace(workspace, passthrough)

        # Permanent run: ensure .env, apply DB creds, install, seed, register, governance.
        if not args.dry_run:
            gen_env()
            if any([args.db_host, args.db_port, args.db_name, args.db_user, args.db_password, args.create_db]):
                # Explicit credentials given as flags.
                apply_db_args(args.db_host, args.db_port, args.db_name, args.db_user,
                              args.db_password, create=args.create_db)
            elif getattr(args, "db_prompt", False):
                # Opt-in: prompt for the password (twice) to use your own PostgreSQL.
                interactive_db_setup(workspace)
            # Default: no prompt. bootstrap's auto chain provisions the DB
            # (Docker -> embedded portable PostgreSQL) with zero prerequisites.
        rc = bootstrap(
            register_auto=True,
            write_env=False,
            with_seed=True,
            auto_db=True,
            with_model=args.with_model,
            workspace=workspace,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            write_governance(workspace, MCUM_ROOT)
        return rc

    if args.command == "list-agents":
        for agent in SUPPORTED_AGENTS:
            target = agent_target(agent)
            print(f"  {agent:14} -> {target if target else '(manual / prints snippet)'}")
        return 0

    if args.command == "print":
        ws = Path(args.workspace).resolve() if args.workspace else WORKSPACE_ROOT
        spec = build_server_spec(
            workspace_path=ws, project_name=args.project_name, embedding_backend=args.backend
        )
        if args.agent in {"claude-code", "antigravity"}:
            print(json.dumps(render_claude_code(spec), indent=2))
        elif args.agent == "opencode":
            print(json.dumps(render_opencode(spec), indent=2))
        elif args.agent == "codex":
            print(render_codex_toml(spec))
        else:
            print(render_generic(spec))
        return 0

    if args.command == "register":
        ws = Path(args.workspace).resolve() if args.workspace else WORKSPACE_ROOT
        spec = build_server_spec(
            workspace_path=ws, project_name=args.project_name, embedding_backend=args.backend
        )
        if args.auto:
            agents = _select_agents(False, True, args.agent)
        else:
            agents = SUPPORTED_AGENTS if args.all else ([args.agent] if args.agent else [])
        if not agents:
            print("Specify --agent <name>, --all, or --auto.")
            return 2
        for agent in agents:
            result = register_agent(agent, spec, scope=args.scope, workspace=ws)
            line = f"  {result['agent']:14} {result['status']}"
            if result.get("written"):
                line += f" -> {result['written']}"
            if result.get("hint"):
                line += f"\n      {result['hint']}"
            if result.get("snippet"):
                line += f"\n      {result['snippet']}"
            print(line)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
