from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from MCUM import workspace_session


def _maintenance_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_path=str(tmp_path),
        project_name="MCUM",
        maintenance_name="daily_guard",
        window_hours=None,
        snapshot_window_days=7,
        force=False,
        skip_metrics_refresh=False,
        skip_kpi_snapshot=False,
        skip_skill_factory=False,
        quiet=True,
    )


def test_conservative_maintenance_cycle_is_audited_and_skips_adaptive_self_heal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _maintenance_args(tmp_path)
    task_calls: list[dict] = []
    run_calls: list[dict] = []
    record_calls: list[dict] = []

    monkeypatch.setattr(
        workspace_session,
        "load_maintenance_policy",
        lambda: {
            "maintenance_name": "daily_guard",
            "snapshot_window_days": 7,
            "self_heal": {
                "enabled": True,
                "repeat_required_for_adaptive_actions": True,
                "safe_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
                "adaptive_actions": ["run_skill_factory"],
            },
            "skill_factory": {
                "enabled": True,
                "lookback_days": 30,
                "min_occurrences": 2,
                "low_confidence_threshold": 0.72,
                "max_candidates": 1,
                "max_pending_results": 12,
                "max_monitoring_results": 5,
                "max_candidate_ratio_for_bootstrap": 3.0,
                "min_active_tests": 8,
                "min_successful_uses": 2,
                "min_success_rate": 0.75,
                "min_lifecycle_score": 0.78,
                "min_testing_uses": 2,
                "activation_score": 0.82,
                "rollback_score": 0.55,
            },
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1", "project_name": project_name or "MCUM"},
    )
    monkeypatch.setattr(
        workspace_session,
        "get_latest_maintenance_run",
        lambda **kwargs: {
            "findings": {"delta": {"reasons": ["metrics_stale"]}},
            "metrics_snapshot": {"reasons": ["metrics_stale"]},
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "detect_maintenance_delta",
        lambda **kwargs: {
            "should_run": True,
            "fresh_signal_present": True,
            "reasons": ["metrics_stale"],
            "recommended_actions": ["refresh_daily_metrics", "snapshot_project_kpis"],
            "last_activity_at": None,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "_resolve_task_brief",
        lambda current_args, command=None: {"project_path": current_args.project_path, "confirmed": True},
    )
    monkeypatch.setattr(
        workspace_session,
        "refresh_daily_metrics",
        lambda project_id=None: {"rows_refreshed": 11, "latest_day": "2026-03-27"},
    )
    monkeypatch.setattr(
        workspace_session,
        "snapshot_project_kpis",
        lambda **kwargs: [{"project_id": "project-1", "snapshot_date": "2026-03-27"}],
    )
    monkeypatch.setattr(
        workspace_session,
        "run_skill_factory_cycle",
        lambda **kwargs: run_calls.append(kwargs) or pytest.fail("adaptive self-heal should stay disabled here"),
    )
    monkeypatch.setattr(
        workspace_session,
        "_log_maintenance_task",
        lambda **kwargs: task_calls.append(kwargs) or ("log-audit-1", "session-audit-1"),
    )
    monkeypatch.setattr(
        workspace_session,
        "record_maintenance_run",
        lambda **kwargs: record_calls.append(kwargs) or "maintenance-audit-1",
    )

    exit_code = workspace_session._run_maintenance_cycle(args)
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert run_calls == []
    assert task_calls[0]["outcome"] == "success"
    assert task_calls[0]["confidence_score"] == 0.92
    assert record_calls[0]["status"] == "success"
    assert record_calls[0]["findings"]["maintenance_status"] == "success"
    assert len(record_calls[0]["actions_applied"]) == 2
    assert all(item.get("status") == "success" for item in record_calls[0]["findings"]["action_results"])
    assert "maintenance-audit-1" in captured
    assert '"status": "success"' in captured
