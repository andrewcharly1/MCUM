from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from MCUM.core import skill_factory
from MCUM import workspace_session


class _CursorStub:
    def __init__(self, fetchall_responses=None, fetchone_responses=None) -> None:
        self.fetchall_responses = list(fetchall_responses or [])
        self.fetchone_responses = list(fetchone_responses or [])

    def execute(self, query: str, params=None) -> None:
        return None

    def fetchall(self):
        return self.fetchall_responses.pop(0) if self.fetchall_responses else []

    def fetchone(self):
        return self.fetchone_responses.pop(0) if self.fetchone_responses else {}


class _CursorManager:
    def __init__(self, cursor: _CursorStub) -> None:
        self.cursor = cursor

    def __enter__(self) -> _CursorStub:
        return self.cursor

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnManager:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_collect_skill_gap_signals_groups_generic_low_confidence_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "title": "Payroll reconciliation export process for April",
                    "description": "",
                    "skill_used": "kaizen",
                    "outcome": "partial",
                    "confidence_score": 0.52,
                },
                {
                    "title": "Payroll reconciliation export process for May",
                    "description": "",
                    "skill_used": "mcum-orchestrator",
                    "outcome": "failure",
                    "confidence_score": 0.61,
                },
            ],
            [
                {
                    "input_context": "Payroll reconciliation export process failed on retry",
                    "skill_name": "kaizen",
                    "final_confidence": 0.44,
                    "outcome_status": "failure",
                    "failure_reason": "fallback retrieval failed",
                }
            ],
        ]
    )
    monkeypatch.setattr(skill_factory, "discover_local_skills", lambda: [{"skill_name": "kaizen"}])
    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))

    signals = skill_factory.collect_skill_gap_signals(min_occurrences=2, low_confidence_threshold=0.72)

    assert len(signals) == 1
    assert signals[0]["occurrences"] == 3
    assert signals[0]["failure_count"] == 2
    assert signals[0]["signal_sources"] == {"project_logs": 2, "retrieval_runs": 1}
    assert signals[0]["suggested_skill_name"] == "payroll-reconciliation-export-specialist"
    assert "references" in signals[0]["resources"]
    assert signals[0]["actionability"]["recommended_action"] == "bootstrap_candidate"


def test_collect_skill_gap_signals_marks_existing_family_as_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "title": "Payroll reconciliation export process for April",
                    "description": "",
                    "skill_used": "mcum-orchestrator",
                    "outcome": "failure",
                    "confidence_score": 0.4,
                },
                {
                    "title": "Payroll reconciliation export process for May",
                    "description": "",
                    "skill_used": "mcum-orchestrator",
                    "outcome": "failure",
                    "confidence_score": 0.41,
                },
                {
                    "title": "Invoice anomaly forensic review for branch north",
                    "description": "",
                    "skill_used": "mcum-orchestrator",
                    "outcome": "failure",
                    "confidence_score": 0.31,
                },
                {
                    "title": "Invoice anomaly forensic review for branch south",
                    "description": "",
                    "skill_used": "mcum-orchestrator",
                    "outcome": "failure",
                    "confidence_score": 0.33,
                },
            ],
            [],
        ]
    )
    monkeypatch.setattr(
        skill_factory,
        "discover_local_skills",
        lambda: [{"skill_name": "payroll-reconciliation-export-specialist"}],
    )
    monkeypatch.setattr(
        skill_factory,
        "list_skill_catalog",
        lambda status=None: (
            [{"skill_name": "payroll-reconciliation-export-specialist", "status": "candidate"}]
            if status is None
            else []
        ),
    )
    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))

    signals = skill_factory.collect_skill_gap_signals(min_occurrences=2, low_confidence_threshold=0.72)

    assert len(signals) == 2
    assert signals[0]["suggested_skill_name"] == "invoice-anomaly-forensic-specialist"
    assert signals[0]["actionability"]["recommended_action"] == "bootstrap_candidate"
    covered_signal = next(
        item for item in signals if item["suggested_skill_name"].startswith("payroll-reconciliation-export-specialist")
    )
    assert covered_signal["coverage"]["family_covered"] is True
    assert covered_signal["coverage"]["candidate_family_matches"] == ["payroll-reconciliation-export-specialist"]
    assert covered_signal["actionability"]["recommended_action"] == "consolidate_existing_candidate"
    assert covered_signal["actionability"]["score"] < signals[0]["actionability"]["score"]


def test_bootstrap_candidate_skill_creates_local_skill_from_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upserts: list[dict] = []
    monkeypatch.setattr(skill_factory, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(skill_factory, "get_skill_record", lambda skill_name: None)
    monkeypatch.setattr(skill_factory, "_run_skill_creator_init", lambda *args, **kwargs: (True, "init ok"))
    monkeypatch.setattr(skill_factory, "_run_skill_creator_validate", lambda path: (True, "Skill is valid!"))
    monkeypatch.setattr(skill_factory, "sync_skill_catalog", lambda: {"skills_synced": 1})
    monkeypatch.setattr(skill_factory, "upsert_skill_record", lambda **kwargs: upserts.append(kwargs) or kwargs)

    signal = {
        "suggested_skill_name": "payroll-reconciliation-export-specialist",
        "description": "Specialized workflow for recurring payroll reconciliation export tasks.",
        "sample_tasks": [
            "Payroll reconciliation export process for April",
            "Payroll reconciliation export process for May",
        ],
        "resources": ["references", "scripts"],
        "occurrences": 2,
        "avg_confidence": 0.56,
        "failure_count": 1,
        "skills_seen": ["kaizen", "mcum-orchestrator"],
    }

    result = skill_factory.bootstrap_candidate_skill(signal)
    skill_dir = tmp_path / "payroll-reconciliation-export-specialist"

    assert result["created"] is True
    assert result["status"] == "candidate"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "references" / "signals.md").exists()
    assert upserts[0]["status"] == "candidate"
    assert upserts[0]["source"] == "generated"


def test_evaluate_candidate_promotion_promotes_when_gate_is_met(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "candidate-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: candidate-skill\ndescription: candidate skill\n---\n",
        encoding="utf-8",
    )
    status_updates: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        skill_factory,
        "get_skill_record",
        lambda skill_name: {"skill_name": skill_name, "skill_path": str(skill_dir)},
    )
    monkeypatch.setattr(skill_factory, "_run_skill_creator_validate", lambda path: (True, "Skill is valid!"))
    monkeypatch.setattr(
        skill_factory,
        "collect_skill_performance_metrics",
        lambda **kwargs: {
            "candidate-skill": {
                "active_tests": 8,
                "successes": 2,
                "final_uses": 2,
                "success_rate": 1.0,
                "lifecycle_score": 0.87,
            }
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "update_skill_status",
        lambda skill_name, status, metadata_update=None: status_updates.append(
            (skill_name, status, metadata_update or {})
        )
        or True,
    )

    result = skill_factory.evaluate_candidate_promotion("candidate-skill")

    assert result["promoted"] is True
    assert result["status"] == "active"
    assert result["lifecycle_score"] == 0.87
    assert status_updates[0][0] == "candidate-skill"
    assert status_updates[0][1] == "active"


def test_evaluate_candidate_promotion_retires_stale_candidate_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "stale-candidate"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: stale-candidate\ndescription: stale candidate\n---\n",
        encoding="utf-8",
    )
    retire_calls: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        skill_factory,
        "get_skill_record",
        lambda skill_name: {
            "skill_name": skill_name,
            "skill_path": str(skill_dir),
            "discovered_at": "2025-01-01T00:00:00+00:00",
            "last_synced_at": "2025-01-02T00:00:00+00:00",
            "last_used_at": None,
            "last_improved_at": None,
            "experience_count": 0,
            "project_count": 0,
            "avg_confidence": 0.2,
            "metadata": {"missing_on_disk": False},
        },
    )
    monkeypatch.setattr(skill_factory, "_run_skill_creator_validate", lambda path: (True, "Skill is valid!"))
    monkeypatch.setattr(
        skill_factory,
        "collect_skill_performance_metrics",
        lambda **kwargs: {
            "stale-candidate": {
                "active_tests": 0,
                "successes": 0,
                "final_uses": 0,
                "success_rate": 0.0,
                "lifecycle_score": 0.12,
            }
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "retire_skill_record",
        lambda skill_name, reason, metadata_update=None, retired_at=None: retire_calls.append(
            (skill_name, reason, metadata_update or {})
        )
        or True,
    )

    result = skill_factory.evaluate_candidate_promotion(
        "stale-candidate",
        retire_after_days=30,
        retire_lifecycle_score=0.35,
    )

    assert result["retired"] is True
    assert result["status"] == "deprecated"
    assert result["reason"] == "retired_stale_without_evidence"
    assert retire_calls[0][0] == "stale-candidate"
    assert retire_calls[0][1] == "retired_stale_without_evidence"
    assert retire_calls[0][2]["retirement"]["retire_after_days"] == 30


def test_consolidate_candidate_families_deprecates_duplicate_family_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_updates: list[tuple[str, str, dict]] = []
    metadata_merges: list[tuple[str, dict]] = []

    candidate_records = [
        {
            "skill_name": "alpha-skill-specialist",
            "metadata": {"validation": {"passed": True}},
            "avg_confidence": 0.2,
            "experience_count": 0,
            "active_test_count": 0,
            "discovered_at": "2026-03-20T10:00:00+00:00",
        },
        {
            "skill_name": "alpha-skill-specialist-2",
            "metadata": {"validation": {"passed": True}},
            "avg_confidence": 0.1,
            "experience_count": 0,
            "active_test_count": 0,
            "discovered_at": "2026-03-21T10:00:00+00:00",
        },
        {
            "skill_name": "beta-skill-specialist",
            "metadata": {"validation": {"passed": True}},
            "avg_confidence": 0.4,
            "experience_count": 1,
            "active_test_count": 0,
            "discovered_at": "2026-03-22T10:00:00+00:00",
        },
    ]
    monkeypatch.setattr(
        skill_factory,
        "update_skill_status",
        lambda skill_name, status, metadata_update=None: status_updates.append(
            (skill_name, status, metadata_update or {})
        )
        or True,
    )
    monkeypatch.setattr(
        skill_factory,
        "merge_skill_metadata",
        lambda skill_name, metadata_update: metadata_merges.append((skill_name, metadata_update)) or True,
    )

    result = skill_factory.consolidate_candidate_families(candidate_records=candidate_records)

    assert result["families_seen"] == 2
    assert result["families_with_duplicates"] == 1
    assert len(result["consolidated"]) == 1
    assert result["consolidated"][0]["canonical_skill"] == "alpha-skill-specialist"
    assert result["consolidated"][0]["duplicate_skill"] == "alpha-skill-specialist-2"
    assert status_updates[0][0] == "alpha-skill-specialist-2"
    assert status_updates[0][1] == "deprecated"
    assert status_updates[0][2]["merged_into"] == "alpha-skill-specialist"
    assert metadata_merges[0][0] == "alpha-skill-specialist"
    assert metadata_merges[0][1]["family_aliases"] == ["alpha-skill-specialist-2"]


def test_run_skill_factory_cycle_tracks_retired_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_factory, "sync_skill_catalog", lambda: {"skills_synced": 1})
    monkeypatch.setattr(skill_factory, "apply_dispatch_hints", lambda **kwargs: [])
    monkeypatch.setattr(
        skill_factory,
        "list_skill_catalog",
        lambda status=None: (
            [
                {"skill_name": "candidate-a", "metadata": {"missing_on_disk": False}},
                {"skill_name": "candidate-b", "metadata": {"missing_on_disk": False}},
            ]
            if status == "candidate"
            else [{"skill_name": "active-a", "metadata": {"missing_on_disk": False}}]
            if status == "active"
            else []
        ),
    )
    monkeypatch.setattr(
        skill_factory,
        "evaluate_candidate_promotion",
        lambda skill_name, **kwargs: (
            {
                "skill_name": skill_name,
                "promoted": False,
                "retired": True,
                "status": "deprecated",
                "reason": "retired_stale_without_evidence",
                "active_tests": 0,
                "successful_uses": 0,
                "success_rate": 0.0,
                "lifecycle_score": 0.1,
            }
            if skill_name == "candidate-a"
            else {
                "skill_name": skill_name,
                "promoted": False,
                "status": "candidate",
                "reason": "promotion_gate_not_met",
                "active_tests": 0,
                "successful_uses": 0,
                "success_rate": 0.0,
                "lifecycle_score": 0.25,
            }
        ),
    )
    monkeypatch.setattr(
        skill_factory,
        "review_testing_skill_versions",
        lambda **kwargs: {"activated": [], "rolled_back": [], "monitoring": []},
    )
    monkeypatch.setattr(skill_factory, "collect_skill_gap_signals", lambda **kwargs: [])
    monkeypatch.setattr(skill_factory, "log_entry", lambda **kwargs: "log-1")

    result = skill_factory.run_skill_factory_cycle(
        project_id="project-1",
        auto_bootstrap=False,
        max_pending_results=1,
        max_monitoring_results=1,
    )

    assert len(result["retired"]) == 1
    assert result["candidate_lifecycle"]["retired"] == 1
    assert result["candidate_lifecycle"]["pending"] == 1
    assert result["catalog_pressure"]["candidate_count"] == 2
    assert result["catalog_pressure"]["active_count"] == 1
    assert result["journal"]["counts"]["retirements"] == 1
    assert result["journal"]["counts"]["planned"] == 1
    assert any(event["action"] == "candidate_retirement" for event in result["journal"]["applied"])
    assert any(event["action"] == "candidate_review_pending" for event in result["journal"]["planned"])
    assert "candidate-a" in result["journal"]["touched_skills"]


def test_run_skill_factory_cycle_blocks_bootstrap_when_candidate_pressure_is_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_calls: list[dict] = []

    monkeypatch.setattr(skill_factory, "sync_skill_catalog", lambda: {"skills_synced": 1})
    monkeypatch.setattr(skill_factory, "apply_dispatch_hints", lambda **kwargs: [])
    monkeypatch.setattr(
        skill_factory,
        "list_skill_catalog",
        lambda status=None: (
            [
                {"skill_name": "candidate-a", "metadata": {"missing_on_disk": False}},
                {"skill_name": "candidate-b", "metadata": {"missing_on_disk": False}},
            ]
            if status == "candidate"
            else [{"skill_name": "active-a", "metadata": {"missing_on_disk": False}}]
            if status == "active"
            else []
        ),
    )
    monkeypatch.setattr(
        skill_factory,
        "consolidate_candidate_families",
        lambda **kwargs: {
            "families_seen": 2,
            "families_with_duplicates": 1,
            "consolidated": [{"canonical_skill": "candidate-a", "duplicate_skill": "candidate-b"}],
            "skipped": [],
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "evaluate_candidate_promotion",
        lambda skill_name, **kwargs: {
            "skill_name": skill_name,
            "promoted": False,
            "status": "candidate",
            "reason": "promotion_gate_not_met",
            "active_tests": 0,
            "successful_uses": 0,
            "success_rate": 0.0,
            "lifecycle_score": 0.25,
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "review_testing_skill_versions",
        lambda **kwargs: {
            "activated": [],
            "rolled_back": [],
            "monitoring": [
                {"skill_name": "testing-a"},
                {"skill_name": "testing-b"},
            ],
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "collect_skill_gap_signals",
        lambda **kwargs: [
            {
                "suggested_skill_name": "new-specialist",
                "description": "new specialist",
                "sample_tasks": ["task a", "task b"],
            }
        ],
    )
    monkeypatch.setattr(
        skill_factory,
        "bootstrap_candidate_skill",
        lambda signal: bootstrap_calls.append(signal) or {"created": True, "skill_name": "new-specialist"},
    )
    monkeypatch.setattr(skill_factory, "log_entry", lambda **kwargs: "log-1")

    result = skill_factory.run_skill_factory_cycle(
        project_id="project-1",
        auto_bootstrap=True,
        max_candidates=1,
        max_candidate_ratio_for_bootstrap=1.0,
        max_pending_results=1,
        max_monitoring_results=1,
        consolidate_candidate_duplicates=True,
    )

    assert bootstrap_calls == []
    assert result["created"] == []
    assert result["pending_total"] == 2
    assert result["pending_truncated"] == 1
    assert result["testing_reviews"]["monitoring_total"] == 2
    assert result["testing_reviews"]["monitoring_truncated"] == 1
    assert result["catalog_pressure"]["candidate_active_ratio"] == 2.0
    assert result["catalog_pressure"]["auto_bootstrap_requested"] is True
    assert result["catalog_pressure"]["auto_bootstrap_applied"] is False
    assert len(result["candidate_consolidation"]["consolidated"]) == 1
    assert result["journal"]["counts"]["consolidations"] == 1
    assert result["journal"]["counts"]["planned"] == 3
    assert any(event["action"] == "candidate_consolidation" for event in result["journal"]["applied"])
    assert any(event["action"] == "bootstrap_candidate_planned" for event in result["journal"]["planned"])


def test_workspace_skill_factory_cli_prints_cycle_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(
        project_path=str(tmp_path),
        project_name="MCUM",
        promote_only=False,
        max_candidates=1,
        min_occurrences=2,
        low_confidence_threshold=0.72,
        min_active_tests=8,
        min_successful_uses=2,
        min_success_rate=0.75,
    )

    monkeypatch.setattr(
        workspace_session,
        "get_or_create_project",
        lambda project_path, project_name=None: {"id": "project-1"},
    )
    monkeypatch.setattr(
        workspace_session,
        "run_skill_factory_cycle",
        lambda **kwargs: {"created": [{"skill_name": "candidate-skill"}], "promoted": [], "signals": [], "pending": []},
    )

    exit_code = workspace_session._run_skill_factory(args)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "candidate-skill" in output


def test_apply_dispatch_hints_merges_successful_forced_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "skill_used": "html-dashboard-expert",
                    "session_id": "session-1",
                    "task_description": "Dashboard ejecutivo de flota minera",
                    "dispatch_method": "forced_by_user",
                    "auto_dispatch": {
                        "skill_name": "ui-ux-pro-max",
                        "match_method": "semantic",
                        "triggered_by": "semantic_score=0.82",
                    },
                },
                {
                    "skill_used": "html-dashboard-expert",
                    "session_id": "session-2",
                    "task_description": "Dashboard ejecutivo de flota minera semanal",
                    "dispatch_method": "forced_by_user",
                    "auto_dispatch": {
                        "skill_name": "ui-ux-pro-max",
                        "match_method": "semantic",
                        "triggered_by": "semantic_score=0.84",
                    },
                },
            ],
            [
                {
                    "skill_used": "html-dashboard-expert",
                    "outcome": "success",
                    "confidence_score": 0.95,
                    "session_id": "session-1",
                },
                {
                    "skill_used": "html-dashboard-expert",
                    "outcome": "success",
                    "confidence_score": 0.96,
                    "session_id": "session-2",
                },
            ],
        ]
    )
    merges: list[tuple[str, dict]] = []

    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        skill_factory,
        "get_skill_record",
        lambda skill_name: {
            "skill_name": skill_name,
            "metadata": {"dispatch_hints": {"triggers": ["html"]}} if skill_name == "html-dashboard-expert" else {},
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "merge_skill_metadata",
        lambda skill_name, metadata_update: merges.append((skill_name, metadata_update)) or True,
    )

    applied = skill_factory.apply_dispatch_hints(min_occurrences=2)

    by_skill = {item["skill_name"]: item for item in applied}
    assert by_skill["html-dashboard-expert"]["priority_delta"] == 1
    assert "dashboard" in by_skill["html-dashboard-expert"]["triggers"]
    assert by_skill["ui-ux-pro-max"]["priority_delta"] == -1
    assert "dashboard" in by_skill["ui-ux-pro-max"]["anti"]
    merged_by_skill = {skill_name: payload for skill_name, payload in merges}
    assert "dashboard" in merged_by_skill["html-dashboard-expert"]["dispatch_hints"]["triggers"]
    assert "dashboard" in merged_by_skill["ui-ux-pro-max"]["dispatch_hints"]["anti"]


def test_collect_dispatch_hints_requires_repeated_successful_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "skill_used": "html-dashboard-expert",
                    "session_id": "session-1",
                    "task_description": "Dashboard ejecutivo de flota minera",
                    "dispatch_method": "forced_by_user",
                    "auto_dispatch": {
                        "skill_name": "ui-ux-pro-max",
                        "match_method": "semantic",
                        "triggered_by": "semantic_score=0.82",
                    },
                }
            ],
            [
                {
                    "skill_used": "html-dashboard-expert",
                    "outcome": "success",
                    "confidence_score": 0.95,
                    "session_id": "session-1",
                }
            ],
        ]
    )
    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))

    hints = skill_factory.collect_dispatch_hints(min_occurrences=2)

    assert hints == {}


def test_collect_dispatch_hints_learns_from_repeated_implicit_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _CursorStub(
        fetchall_responses=[
            [],
            [],
            [
                {
                    "title": "Crear dashboard ejecutivo con glassmorphism para flota minera",
                    "skill_used": "html-dashboard-expert",
                    "outcome": "success",
                    "confidence_score": 0.91,
                    "task_description": "Crear dashboard ejecutivo con glassmorphism para flota minera",
                    "selected_skill": "ui-ux-pro-max",
                    "dispatch_method": "semantic",
                    "skill_correction": {
                        "implicit": True,
                        "source": "workspace_session_final_skill_override",
                    },
                },
                {
                    "title": "Crear dashboard ejecutivo con glassmorphism semanal",
                    "skill_used": "html-dashboard-expert",
                    "outcome": "success",
                    "confidence_score": 0.92,
                    "task_description": "Crear dashboard ejecutivo con glassmorphism semanal",
                    "selected_skill": "ui-ux-pro-max",
                    "dispatch_method": "semantic",
                    "skill_correction": {
                        "implicit": True,
                        "source": "workspace_session_final_skill_override",
                    },
                },
            ],
        ]
    )
    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))

    hints = skill_factory.collect_dispatch_hints(min_occurrences=2)

    assert hints["html-dashboard-expert"]["successful_implicit_corrections"] == 2
    assert hints["html-dashboard-expert"]["priority_delta"] == 1
    assert "dashboard" in hints["html-dashboard-expert"]["triggers"]
    assert hints["ui-ux-pro-max"]["overridden_implicitly"] == 2
    assert hints["ui-ux-pro-max"]["priority_delta"] == -1
    assert "glassmorphism" in hints["ui-ux-pro-max"]["anti"]


def test_filter_dispatchable_skills_merges_catalog_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard"],
            "anti": [],
            "profile": "Dashboard HTML puro",
            "priority": 8,
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "dispatch_hints": {
                        "triggers": ["flota", "minera"],
                        "anti": ["landing", "portfolio"],
                        "samples": ["dashboard ejecutivo de flota minera"],
                        "priority_delta": 2,
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(registry=registry, include_candidates=False)

    assert filtered[0]["triggers"] == ["dashboard", "flota", "minera"]
    assert filtered[0]["anti"] == ["landing", "portfolio"]
    assert filtered[0]["priority"] == 10
    assert "flota minera" in filtered[0]["profile"]


def test_filter_dispatchable_skills_applies_performance_priority_boost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard"],
            "anti": [],
            "profile": "Dashboard HTML puro",
            "priority": 7,
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "performance": {
                        "lifecycle_score": 0.9,
                        "project_scores": {"project-1": 0.92},
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(
        registry=registry,
        include_candidates=False,
        project_context={"id": "project-1"},
    )

    assert filtered[0]["priority"] == 10


def test_filter_dispatchable_skills_ages_old_dispatch_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard"],
            "anti": [],
            "profile": "Dashboard HTML puro",
            "priority": 8,
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "dispatch_hints": {
                        "triggers": ["minera"],
                        "priority_delta": 2,
                        "updated_at": "2025-10-01T00:00:00+00:00",
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(registry=registry, include_candidates=False)

    assert filtered[0]["priority"] < 10
    assert filtered[0]["metadata"]["dispatch_hints"]["_freshness_state"] in {"aging", "stale"}


def test_filter_dispatchable_skills_removes_conflicting_anti_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard", "html"],
            "anti": ["react"],
            "profile": "Dashboard HTML puro",
            "priority": 8,
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "dispatch_hints": {
                        "triggers": ["glassmorphism"],
                        "anti": ["dashboard", "glassmorphism", "landing"],
                        "priority_delta": 1,
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(registry=registry, include_candidates=False)

    assert filtered[0]["triggers"] == ["dashboard", "html", "glassmorphism"]
    assert filtered[0]["anti"] == ["react", "landing"]


def test_filter_dispatchable_skills_applies_catalog_routing_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard"],
            "anti": ["react"],
            "profile": "Dashboard HTML puro",
            "priority": 8,
            "routing_source": "frontmatter",
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "routing_override": {
                        "triggers": ["panel ejecutivo"],
                        "anti": ["excel bruto"],
                        "priority": 10,
                        "profile": "Catalog governed dashboard profile",
                        "source": "catalog_override",
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(registry=registry, include_candidates=False)

    assert filtered[0]["triggers"] == ["dashboard", "panel ejecutivo"]
    assert filtered[0]["anti"] == ["react", "excel bruto"]
    assert filtered[0]["priority"] == 10
    assert filtered[0]["profile"] == "Catalog governed dashboard profile"
    assert filtered[0]["routing_source"] == "catalog_override"


def test_filter_dispatchable_skills_can_disable_routing_from_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = [
        {
            "name": "html-dashboard-expert",
            "file": "html-dashboard-expert",
            "triggers": ["dashboard"],
            "anti": [],
            "profile": "Dashboard HTML puro",
            "priority": 8,
        }
    ]
    monkeypatch.setattr(
        skill_factory,
        "get_dispatchable_skill_catalog",
        lambda: {
            "html-dashboard-expert": {
                "status": "active",
                "metadata": {
                    "routing_override": {
                        "enabled": False,
                        "source": "catalog_override",
                    }
                },
            }
        },
    )

    filtered = skill_factory.filter_dispatchable_skills(registry=registry, include_candidates=False)

    assert filtered == []


def test_review_testing_skill_versions_activates_high_scoring_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "html-dashboard-expert"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "id": "version-1",
                    "skill_name": "html-dashboard-expert",
                    "version_semver": "2.1.4",
                    "ckl_score": 1.0,
                    "created_at": "2026-03-15T13:29:47Z",
                }
            ]
        ]
    )
    version_updates: list[tuple[str, str, str | None]] = []
    catalog_updates: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        skill_factory,
        "collect_skill_performance_metrics",
        lambda **kwargs: {
            "html-dashboard-expert": {
                "active_tests": 7,
                "final_uses": 2,
                "successes": 2,
                "failures": 0,
                "success_rate": 1.0,
                "lifecycle_score": 0.89,
                "project_scores": {"project-1": 0.91},
            }
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "get_skill_record",
        lambda skill_name: {"skill_name": skill_name, "skill_path": str(skill_dir)},
    )
    monkeypatch.setattr(
        skill_factory,
        "_update_skill_version_status_record",
        lambda version_id, **kwargs: version_updates.append((version_id, kwargs["status"], kwargs.get("note"))) or True,
    )
    monkeypatch.setattr(
        skill_factory,
        "update_skill_status",
        lambda skill_name, status, metadata_update=None: catalog_updates.append(
            (skill_name, status, metadata_update or {})
        )
        or True,
    )

    result = skill_factory.review_testing_skill_versions(
        project_id="project-1",
        min_active_tests=7,
        min_uses=2,
        activation_score=0.82,
    )

    assert len(result["activated"]) == 1
    assert result["activated"][0]["skill_name"] == "html-dashboard-expert"
    assert version_updates[0][1] == "active"
    assert catalog_updates[0][1] == "active"


def test_review_testing_skill_versions_rolls_back_weak_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "ui-ux-pro-max"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (skill_dir / "SKILL.md.bak").write_text("# backup\n", encoding="utf-8")
    cursor = _CursorStub(
        fetchall_responses=[
            [
                {
                    "id": "version-2",
                    "skill_name": "ui-ux-pro-max",
                    "version_semver": "1.0.2",
                    "ckl_score": 1.0,
                    "created_at": "2026-03-15T13:48:25Z",
                }
            ]
        ]
    )
    version_updates: list[tuple[str, str, str | None]] = []
    catalog_updates: list[tuple[str, str, dict]] = []
    rollback_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(skill_factory, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(skill_factory, "get_cursor", lambda conn: _CursorManager(cursor))
    monkeypatch.setattr(
        skill_factory,
        "collect_skill_performance_metrics",
        lambda **kwargs: {
            "ui-ux-pro-max": {
                "active_tests": 7,
                "final_uses": 2,
                "successes": 0,
                "failures": 2,
                "success_rate": 0.0,
                "lifecycle_score": 0.31,
                "project_scores": {},
            }
        },
    )
    monkeypatch.setattr(
        skill_factory,
        "get_skill_record",
        lambda skill_name: {"skill_name": skill_name, "skill_path": str(skill_dir)},
    )
    monkeypatch.setattr(
        skill_factory,
        "_update_skill_version_status_record",
        lambda version_id, **kwargs: version_updates.append((version_id, kwargs["status"], kwargs.get("note"))) or True,
    )
    monkeypatch.setattr(
        skill_factory,
        "update_skill_status",
        lambda skill_name, status, metadata_update=None: catalog_updates.append(
            (skill_name, status, metadata_update or {})
        )
        or True,
    )
    monkeypatch.setattr(
        skill_factory,
        "rollback_sisl_writeback",
        lambda skill_md_path, backup_path=None: rollback_calls.append((skill_md_path, backup_path)) or True,
    )

    result = skill_factory.review_testing_skill_versions(
        min_active_tests=7,
        min_uses=2,
        rollback_score=0.55,
    )

    assert len(result["rolled_back"]) == 1
    assert result["rolled_back"][0]["skill_name"] == "ui-ux-pro-max"
    assert version_updates[0][1] == "deprecated"
    assert catalog_updates[0][1] == "degraded"
    assert rollback_calls
