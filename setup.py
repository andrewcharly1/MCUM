"""
MCUM setup entrypoint.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


MCUM_ROOT = Path(__file__).resolve().parent
REQ_FILE = MCUM_ROOT / "requirements.txt"
ENV_FILE = MCUM_ROOT / ".env"
INSTALL_SCHEMA_SCRIPT = MCUM_ROOT / "install_schema.py"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET} {msg}")


def err(msg: str) -> None:
    print(f"  {RED}ERR{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")


def step(msg: str) -> None:
    print(f"\n{BOLD}{'-' * 50}{RESET}")
    print(f"{BOLD}{msg}{RESET}")


def run_checked(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    if result.stdout.strip():
        print(result.stdout.strip())


def check_python_version() -> None:
    step("Step 1 - Python")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ is required")
    ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def install_dependencies() -> None:
    step("Step 2 - Dependencies")
    if not REQ_FILE.exists():
        raise FileNotFoundError(f"Missing requirements file: {REQ_FILE}")
    run_checked([sys.executable, "-m", "pip", "install", "-r", str(REQ_FILE)])
    ok("Dependencies installed")


def check_env_file() -> None:
    step("Step 3 - Environment")
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"Missing .env file: {ENV_FILE}")
    ok(f"Environment file found: {ENV_FILE}")


def install_schema() -> None:
    step("Step 4 - Schema")
    run_checked([sys.executable, str(INSTALL_SCHEMA_SCRIPT)])
    ok("Schema installed")


def verify_connection() -> None:
    step("Step 5 - Health check")
    sys.path.insert(0, str(MCUM_ROOT))
    from db.connection import health_check

    status = health_check()
    if not status["connected"]:
        raise RuntimeError(status["error"] or "database connection failed")

    ok("Database connection OK")
    ok(f"core_brain installed: {status['schemas']['core_brain']}")
    ok(f"project_registry installed: {status['schemas']['project_registry']}")
    if status.get("pool_active"):
        ok("Connection pool active")
    else:
        warn("Connection pool not active (psycopg_pool may not be installed)")
    if status["pgvector"]:
        ok("pgvector available")
        if status.get("pgvector_installed"):
            ok("pgvector extension installed")
            from db.experience_store import _is_pgvector_enabled
            if _is_pgvector_enabled():
                ok("Embedding column migrated to vector(384)")
            else:
                warn("pgvector installed but embedding column is still JSONB — re-run schema to migrate")
        else:
            warn("pgvector available but not installed — run CREATE EXTENSION vector")
    else:
        warn("pgvector not available; JSONB embeddings remain enabled")


def backfill_embeddings() -> None:
    step("Step 6 - Embedding backfill")
    sys.path.insert(0, str(MCUM_ROOT))
    from db.embedder import warmup_model
    from db.experience_store import compute_and_store_missing_embeddings

    model_name = warmup_model()
    ok(f"Embedding model ready: {model_name}")
    migrated = compute_and_store_missing_embeddings()
    from db.experience_store import _is_pgvector_enabled
    storage_mode = "vector(384)" if _is_pgvector_enabled() else "JSONB"
    ok(f"Embeddings backfilled: {migrated} (storage: {storage_mode})")


def main() -> int:
    try:
        check_python_version()
        install_dependencies()
        check_env_file()
        install_schema()
        verify_connection()
        backfill_embeddings()
        print(f"\n{BOLD}MCUM setup completed successfully.{RESET}")
        return 0
    except Exception as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
