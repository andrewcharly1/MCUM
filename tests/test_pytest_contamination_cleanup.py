from __future__ import annotations

import pytest

from MCUM.core import pytest_contamination_cleanup as cleanup


def _row(path: str, **overrides):
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "project_path": path,
        "total_sessions": 0,
        "total_tasks_completed": 0,
        "total_improvements": 0,
        "experiences": 0,
        "retrieval_runs": 0,
        "session_playbooks": 0,
        "maintenance_runs": 0,
        "project_kpis": 0,
        "project_logs": 0,
        "spec_contracts": 2,
    }
    row.update(overrides)
    return row


def test_exact_pytest_path_with_only_cascade_dependencies_is_safe() -> None:
    report = cleanup.build_cleanup_report(
        [_row("C:/Users/dev/AppData/Local/Temp/pytest-of-dev/pytest-71/test_x0")]
    )

    assert report["safe_count"] == 1
    assert report["safe_spec_contracts"] == 2


def test_similar_but_non_pytest_path_is_rejected() -> None:
    report = cleanup.build_cleanup_report(
        [_row("C:/Users/dev/OneDrive/project/pytest-of-dev/pytest-71/test_x0")]
    )

    assert report["rejected_count"] == 1
    assert report["safe_count"] == 0


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("total_sessions", "total_sessions_nonzero"),
        ("project_logs", "project_logs_present"),
        ("retrieval_runs", "retrieval_runs_present"),
    ],
)
def test_nonzero_activity_or_no_action_dependency_protects_project(
    field: str,
    reason: str,
) -> None:
    report = cleanup.build_cleanup_report(
        [
            _row(
                "C:/Users/dev/AppData/Local/Temp/pytest-of-dev/pytest-71/test_x0",
                **{field: 1},
            )
        ]
    )

    assert report["protected_count"] == 1
    assert reason in report["protected"][0]["reasons"]


def test_apply_validation_rejects_count_drift() -> None:
    with pytest.raises(RuntimeError, match="count drift"):
        cleanup.validate_apply_request(
            {"safe_count": 4},
            expected_count=3,
            confirm_token=cleanup.CONFIRM_TOKEN,
        )


def test_apply_validation_rejects_wrong_confirmation_token() -> None:
    with pytest.raises(RuntimeError, match="confirmation token"):
        cleanup.validate_apply_request(
            {"safe_count": 4},
            expected_count=4,
            confirm_token="wrong",
        )


def test_apply_cleanup_deletes_only_safe_ids_and_verifies_remaining(monkeypatch) -> None:
    reports = iter(
        [
            {
                "safe_count": 2,
                "safe": [{"id": "id-1"}, {"id": "id-2"}],
                "protected_count": 1,
            },
            {"safe_count": 0, "protected_count": 1},
        ]
    )

    class Cursor:
        def execute(self, _sql, params=None):
            assert params == (["id-1", "id-2"],)

        def fetchall(self):
            return [{"id": "id-1"}, {"id": "id-2"}]

    class Context:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cleanup, "load_cleanup_report", lambda: next(reports))
    monkeypatch.setattr(cleanup, "get_db", lambda: Context(object()))
    monkeypatch.setattr(cleanup, "get_cursor", lambda _conn: Context(Cursor()))

    result = cleanup.apply_cleanup(
        expected_count=2,
        confirm_token=cleanup.CONFIRM_TOKEN,
    )

    assert result == {
        "status": "success",
        "deleted_projects": 2,
        "protected_projects": 1,
        "remaining_safe_projects": 0,
    }


def test_apply_cleanup_no_changes_does_not_open_write_transaction(monkeypatch) -> None:
    monkeypatch.setattr(
        cleanup,
        "load_cleanup_report",
        lambda: {"safe_count": 0, "safe": [], "protected_count": 1},
    )
    monkeypatch.setattr(
        cleanup,
        "get_db",
        lambda: pytest.fail("no write transaction expected"),
    )

    result = cleanup.apply_cleanup(
        expected_count=0,
        confirm_token=cleanup.CONFIRM_TOKEN,
    )

    assert result["status"] == "no_changes"


def test_cleanup_main_defaults_to_dry_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cleanup,
        "load_cleanup_report",
        lambda: {"status": "dry_run", "safe_count": 3},
    )

    assert cleanup.main([]) == 0
    assert '"safe_count": 3' in capsys.readouterr().out
