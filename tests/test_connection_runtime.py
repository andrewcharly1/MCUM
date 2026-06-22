from __future__ import annotations

import pytest

from MCUM.db import connection


def test_resolve_runtime_db_host_uses_wsl_gateway_for_localhost(
    monkeypatch,
) -> None:
    monkeypatch.setattr(connection, "_running_in_wsl", lambda: True)
    monkeypatch.setattr(connection, "_detect_wsl_host_gateway", lambda: "192.168.32.1")
    monkeypatch.delenv("DB_HOST_WSL", raising=False)

    assert connection._resolve_runtime_db_host("localhost") == "192.168.32.1"


def test_resolve_runtime_db_host_prefers_explicit_wsl_override(
    monkeypatch,
) -> None:
    monkeypatch.setattr(connection, "_running_in_wsl", lambda: True)
    monkeypatch.setattr(connection, "_detect_wsl_host_gateway", lambda: "192.168.32.1")
    monkeypatch.setenv("DB_HOST_WSL", "10.0.0.15")

    assert connection._resolve_runtime_db_host("localhost") == "10.0.0.15"


def test_rewrite_database_url_host_replaces_localhost_only() -> None:
    auth = "postgres:test-password"
    rewritten = connection._rewrite_database_url_host(
        f"postgresql://{auth}@localhost:5432/postgres",
        "192.168.32.1",
    )

    assert rewritten == f"postgresql://{auth}@192.168.32.1:5432/postgres"


def test_rewrite_database_url_host_leaves_remote_host_untouched() -> None:
    auth = "postgres:test-password"
    original = f"postgresql://{auth}@10.0.0.8:5432/postgres"

    assert connection._rewrite_database_url_host(original, "192.168.32.1") == original


def test_database_runtime_uses_isolated_test_url_when_configured() -> None:
    operational = "postgresql://postgres:secret@localhost:5432/mcum"
    isolated = "postgresql://postgres:secret@localhost:5432/mcum_test"

    selected, read_only = connection._resolve_database_runtime(
        operational,
        isolated,
        pytest_active=True,
    )

    assert selected == isolated
    assert read_only is False


def test_database_runtime_forces_operational_fallback_read_only_under_pytest() -> None:
    operational = "postgresql://postgres:secret@localhost:5432/mcum"

    selected, read_only = connection._resolve_database_runtime(
        operational,
        "",
        pytest_active=True,
    )

    assert selected == operational
    assert read_only is True


def test_database_runtime_rejects_test_url_that_matches_operational_database() -> None:
    operational = "postgresql://postgres:secret@localhost:5432/mcum"
    same_database = "postgresql://other:changed@LOCALHOST:5432/mcum"

    with pytest.raises(RuntimeError, match="must not point to the operational"):
        connection._resolve_database_runtime(
            operational,
            same_database,
            pytest_active=True,
        )


def test_database_runtime_keeps_operational_url_read_write_outside_pytest() -> None:
    operational = "postgresql://postgres:secret@localhost:5432/mcum"

    selected, read_only = connection._resolve_database_runtime(
        operational,
        "postgresql://postgres:secret@localhost:5432/mcum_test",
        pytest_active=False,
    )

    assert selected == operational
    assert read_only is False
