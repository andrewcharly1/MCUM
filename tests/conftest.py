from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


os.environ.setdefault("MCUM_PYTEST_ACTIVE", "1")
# Tests use the deterministic hash embedding backend: no model download, no
# tqdm progress threads (which poll a globally-patched time.time in some tests),
# and stable vectors. Tests that exercise semantic behaviour monkeypatch the
# encoder directly. Forced (not setdefault) so a sentence-transformers value in
# the developer's .env/shell does not leak into the suite.
os.environ["MCUM_EMBEDDING_BACKEND"] = "hash"

SKILLS_ROOT = Path(__file__).resolve().parents[2]
MCUM_ROOT = Path(__file__).resolve().parents[1]
if str(SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLS_ROOT))

_AUTO_TEST_DATABASE_URL = ""
_AUTO_TEST_DATABASE_NAME = ""


def _operational_database_url() -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(MCUM_ROOT / ".env")
    except ImportError:
        pass
    explicit = str(os.getenv("DATABASE_URL") or "").strip()
    if explicit:
        return explicit
    user = quote(str(os.getenv("DB_USER") or "postgres"), safe="")
    password = str(os.getenv("DB_PASSWORD") or "")
    if password:
        user = f"{user}:{quote(password, safe='')}"
    host = str(os.getenv("DB_HOST") or "localhost")
    port = str(os.getenv("DB_PORT") or "5432")
    database = str(os.getenv("DB_NAME") or "postgres")
    return f"postgresql://{user}@{host}:{port}/{database}"


def _derive_auto_test_database_url() -> tuple[str, str]:
    operational = urlsplit(_operational_database_url())
    database_name = f"mcum_test_{os.getpid()}"
    return (
        urlunsplit(
            (
                operational.scheme,
                operational.netloc,
                f"/{database_name}",
                operational.query,
                operational.fragment,
            )
        ),
        database_name,
    )


if not str(os.getenv("MCUM_TEST_DATABASE_URL") or "").strip():
    _AUTO_TEST_DATABASE_URL, _AUTO_TEST_DATABASE_NAME = _derive_auto_test_database_url()
    os.environ["MCUM_TEST_DATABASE_URL"] = _AUTO_TEST_DATABASE_URL


def _admin_database_url(test_url: str) -> str:
    parsed = urlsplit(test_url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/postgres", parsed.query, parsed.fragment)
    )


def _drop_auto_test_database() -> None:
    if not _AUTO_TEST_DATABASE_URL:
        return
    import psycopg
    from psycopg import sql

    with psycopg.connect(
        _admin_database_url(_AUTO_TEST_DATABASE_URL),
        autocommit=True,
    ) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(_AUTO_TEST_DATABASE_NAME)
            )
        )


def _provision_auto_test_database() -> None:
    if not _AUTO_TEST_DATABASE_URL:
        return
    import psycopg
    from psycopg import sql

    _drop_auto_test_database()
    with psycopg.connect(
        _admin_database_url(_AUTO_TEST_DATABASE_URL),
        autocommit=True,
    ) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(_AUTO_TEST_DATABASE_NAME)
            )
        )

    parsed = urlsplit(_AUTO_TEST_DATABASE_URL)
    command = shutil.which("psql")
    if not command:
        raise RuntimeError("psql is required to provision the isolated test database")
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    subprocess.run(
        [
            command,
            "-h",
            str(parsed.hostname or "localhost"),
            "-p",
            str(parsed.port or 5432),
            "-U",
            str(parsed.username or "postgres"),
            "-d",
            _AUTO_TEST_DATABASE_NAME,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(MCUM_ROOT / "db" / "schema.sql"),
        ],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with psycopg.connect(_AUTO_TEST_DATABASE_URL) as conn:
        project_id = conn.execute(
            """
            INSERT INTO project_registry.projects (project_name, project_path)
            VALUES ('MCUM isolated pytest seed', 'C:/mcum-isolated-pytest-seed')
            RETURNING id
            """
        ).fetchone()[0]
        for index in range(1, 4):
            conn.execute(
                """
                INSERT INTO core_brain.experiences (
                    category, title, content, current_confidence,
                    unique_context_count, project_id, skill_name,
                    task_description, tested_by
                ) VALUES (
                    'implementation_recipe', %s, %s::jsonb, 0.90,
                    %s, %s, 'mcum-isolated-pytest-seed', %s, 'pytest-seed'
                )
                """,
                (
                    f"MCUM isolated pytest experience {index}",
                    '{"conclusion":"Seed only for isolated PostgreSQL integration tests."}',
                    index,
                    project_id,
                    f"isolated pytest seed context {index}",
                ),
            )


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--allow-live-db",
        action="store_true",
        default=False,
        help="Allow tests marked live_db to access explicitly configured live services.",
    )


def pytest_configure(config) -> None:
    _provision_auto_test_database()
    config.addinivalue_line(
        "markers",
        "integration_db: requires the isolated MCUM_TEST_DATABASE_URL database",
    )
    config.addinivalue_line(
        "markers",
        "live_db: explicitly accesses a live service and requires --allow-live-db",
    )


def pytest_collection_modifyitems(config, items) -> None:
    import pytest

    integration_skip = pytest.mark.skip(
        reason="integration_db requires MCUM_TEST_DATABASE_URL"
    )
    live_skip = pytest.mark.skip(reason="live_db requires --allow-live-db")
    isolated_database = str(os.getenv("MCUM_TEST_DATABASE_URL") or "").strip()
    allow_live = bool(config.getoption("--allow-live-db"))

    for item in items:
        if "integration_db" in item.keywords and not isolated_database:
            item.add_marker(integration_skip)
        if "live_db" in item.keywords and not allow_live:
            item.add_marker(live_skip)


def pytest_unconfigure(config) -> None:
    _drop_auto_test_database()
