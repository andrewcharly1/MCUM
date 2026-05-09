"""
MCUM schema installer.

The only schema source of truth is db/schema.sql.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
SCHEMA_FILE = ROOT / "db" / "schema.sql"


def load_env() -> None:
    if load_dotenv is not None and ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE)


def get_database_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://postgres@localhost:5432/postgres",
    )


def read_schema() -> str:
    if not SCHEMA_FILE.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_FILE}")
    return SCHEMA_FILE.read_text(encoding="utf-8")


def get_psql_binary() -> str:
    psql = shutil.which("psql")
    if not psql:
        raise FileNotFoundError("psql is required and was not found in PATH")
    return psql


def build_psql_env() -> dict[str, str]:
    parsed = urlparse(get_database_url())
    env = os.environ.copy()
    env["PGHOST"] = parsed.hostname or os.getenv("DB_HOST", "localhost")
    env["PGPORT"] = str(parsed.port or os.getenv("DB_PORT", "5432"))
    env["PGDATABASE"] = (parsed.path or "/postgres").lstrip("/") or os.getenv("DB_NAME", "postgres")
    env["PGUSER"] = parsed.username or os.getenv("DB_USER", "postgres")
    env["PGPASSWORD"] = parsed.password or os.getenv("DB_PASSWORD", "")
    env["PGCLIENTENCODING"] = "UTF8"
    return env


def install_schema() -> None:
    read_schema()
    result = subprocess.run(
        [
            get_psql_binary(),
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(SCHEMA_FILE),
        ],
        env=build_psql_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise RuntimeError(stderr or stdout or "psql failed to install schema")
    if result.stdout.strip():
        print(result.stdout.strip())


def main() -> int:
    load_env()
    try:
        install_schema()
        print(f"Schema installed successfully from {SCHEMA_FILE}")
        return 0
    except Exception as exc:
        print(f"Schema installation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
