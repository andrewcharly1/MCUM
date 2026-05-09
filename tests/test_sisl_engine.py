from __future__ import annotations

from pathlib import Path

import pytest

from MCUM.sisl import adversarial_test_generator, autonomous_loop, optimizer, skill_writer, test_runner


class _CursorStub:
    def __init__(self, responses: list[list[dict]]) -> None:
        self._responses = list(responses)
        self._current: list[dict] = []

    def execute(self, query: str, params=None) -> None:
        self._current = self._responses.pop(0) if self._responses else []

    def fetchall(self) -> list[dict]:
        return list(self._current)


class _CursorManager:
    def __init__(self, cursor: _CursorStub) -> None:
        self._cursor = cursor

    def __enter__(self) -> _CursorStub:
        return self._cursor

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnManager:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_generate_adversarial_tests_uses_failures_runs_and_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _CursorStub(
        [
            [
                {
                    "id": "fp-1",
                    "title": "Wrong alias in SQL",
                    "task_description": "Semantic search on pgvector",
                    "content": {"conclusion": "Build filters with alias e."},
                    "current_confidence": 0.8,
                }
            ],
            [
                {
                    "id": "rr-1",
                    "input_context": "Run retrieval after migration",
                    "failure_reason": "Repeated stale cache state",
                    "outcome_description": "retrieval failed",
                    "decision_taken": "used cached state",
                }
            ],
            [
                {
                    "id": "exp-1",
                    "title": "Old memory",
                    "task_description": "Legacy fallback path",
                    "content": {"conclusion": "Revalidate before reuse"},
                    "initial_score": 0.9,
                    "current_confidence": 0.5,
                }
            ],
        ]
    )
    monkeypatch.setattr(adversarial_test_generator, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(adversarial_test_generator, "get_cursor", lambda conn: _CursorManager(cursor))

    tests = adversarial_test_generator.generate_adversarial_tests("mcum-orchestrator", limit=3)

    assert len(tests) == 3
    assert tests[0]["expected_source"] == "failure_pattern:fp-1"
    assert tests[1]["test_type"] == "conflict_resolution"
    assert tests[2]["expected_source"] == "degraded_experience:exp-1"


def test_evaluate_adversarial_requires_failure_signal_and_skill_doc_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        test_runner,
        "retrieve_for_task",
        lambda *args, **kwargs: {
            "experiences": [],
            "failure_patterns": [
                {
                    "id": "fp-1",
                    "content": {"conclusion": "Use alias e and refresh cache."},
                    "category": "failure_pattern",
                }
            ],
            "warnings": ["cross-project fallback"],
        },
    )
    monkeypatch.setattr(
        test_runner,
        "_load_skill_document",
        lambda skill_name: "always use alias e and refresh cache before retrying semantic retrieval",
    )

    result = test_runner._evaluate_adversarial(
        {
            "id": "adv-1",
            "skill_name": "mcum-orchestrator",
            "partition": "adversarial",
            "test_type": "conflict_resolution",
            "input_query": "Run retrieval after migration",
            "expected_result": "Use alias e and refresh cache",
            "pass_condition": "refresh cache before retrying",
        },
        {"skill_name": "mcum-orchestrator"},
    )

    assert result.passed is True
    assert result.score >= 0.55
    assert "failure_signals=1" in result.reason


def test_skill_writer_applies_and_rolls_back(tmp_path: Path) -> None:
    skill_dir = tmp_path / "sample-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    original = "# Sample Skill\n\n## Rules\n\nKeep it deterministic.\n\n## Outputs\n\nDeliver a validated result.\n"
    skill_md.write_text(original, encoding="utf-8")

    proposal = optimizer.ImprovementProposal(
        improvement_type="add_failure_warning",
        section_to_edit="Rules",
        current_text="",
        proposed_text="Warn before mutating production data.",
        confidence=0.9,
        evidence="production incident",
    )

    result = skill_writer.apply_sisl_proposals(
        "sample-skill",
        [proposal],
        skill_md_path=str(skill_md),
    )

    updated = skill_md.read_text(encoding="utf-8")
    assert result["applied"] is True
    assert skill_writer.SISL_WRITEBACK_START in updated
    assert "Warn before mutating production data." in updated
    assert updated.index("Warn before mutating production data.") < updated.index("## Outputs")
    assert result["mode"] == "structured"
    assert result["sections"][0]["mode"] == "insert_section"
    assert Path(result["backup_path"]).exists()

    rolled_back = skill_writer.rollback_sisl_writeback(str(skill_md), result["backup_path"])
    assert rolled_back is True
    assert skill_md.read_text(encoding="utf-8") == original


def test_skill_writer_creates_fallback_section_when_no_target_heading_exists(tmp_path: Path) -> None:
    skill_dir = tmp_path / "sample-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    original = "# Sample Skill\n\n## Overview\n\nKeep it deterministic.\n"
    skill_md.write_text(original, encoding="utf-8")

    proposal = optimizer.ImprovementProposal(
        improvement_type="add_not_applicable",
        section_to_edit="INPUT CONTRACT / NEED_INFO",
        current_text="",
        proposed_text="Reject requests outside the documented workflow.",
        confidence=0.88,
        evidence="negative test failures",
    )

    result = skill_writer.apply_sisl_proposals(
        "sample-skill",
        [proposal],
        skill_md_path=str(skill_md),
    )

    updated = skill_md.read_text(encoding="utf-8")
    assert result["applied"] is True
    assert "## Do Not Use When" in updated
    assert "Reject requests outside the documented workflow." in updated
    assert result["sections"][0]["mode"] == "create_section"


def test_run_sisl_cycle_rolls_back_when_candidate_regresses(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = test_runner.SkillEvalResult(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        ckl_score=0.70,
        val_pass=7,
        val_total=10,
        adv_pass=2,
        adv_total=4,
    )
    candidate = test_runner.SkillEvalResult(
        skill_name="mcum-orchestrator",
        skill_version="2.0.1",
        ckl_score=0.68,
        val_pass=7,
        val_total=10,
        adv_pass=1,
        adv_total=4,
    )
    evaluations = iter([baseline, candidate])
    rollback_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(optimizer, "analyze_and_propose", lambda *args, **kwargs: optimizer.OptimizationReport(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        previous_ckl=baseline.ckl_score,
        target_ckl=0.85,
        proposals=[
            optimizer.ImprovementProposal(
                improvement_type="add_failure_warning",
                section_to_edit="Rules",
                current_text="",
                proposed_text="Warn before semantic retry.",
                confidence=0.9,
                evidence="failed adversarial test",
            )
        ],
        failure_patterns=[],
        cold_start_flag=False,
    ))
    monkeypatch.setattr(optimizer, "save_optimization_report", lambda report, version: {"id": "report-1", "version": "2.0.1"})
    monkeypatch.setattr(
        optimizer,
        "apply_high_confidence_improvements",
        lambda report, skill_md_path=None, dry_run=True, writeback_mode="disabled": [
            {"type": "add_failure_warning", "applied": True, "path": "skill.md", "backup_path": "skill.md.bak"}
        ],
    )
    monkeypatch.setattr(optimizer, "rollback_sisl_writeback", lambda path, backup_path=None: rollback_calls.append((path, backup_path)) or True)
    monkeypatch.setattr(optimizer, "_update_skill_version_status", lambda *args, **kwargs: None)

    def fake_run_evaluation(skill_name, skill_version="1.0.0", partition=None, verbose=True):
        return next(evaluations)

    monkeypatch.setattr("MCUM.sisl.test_runner.run_evaluation", fake_run_evaluation)
    monkeypatch.setattr("MCUM.sisl.test_runner.save_eval_to_db", lambda eval_result: f"eval-{eval_result.skill_version}")

    result = optimizer.run_sisl_cycle(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        dry_run=False,
        persist_eval=True,
        writeback_mode="candidate",
        verbose=False,
    )

    assert result["gate_result"]["accepted"] is False
    assert result["applied"][0]["rolled_back"] is True
    assert rollback_calls == [("skill.md", "skill.md.bak")]


def test_run_sisl_cycle_accepts_candidate_when_adv_improves(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = test_runner.SkillEvalResult(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        ckl_score=0.70,
        val_pass=7,
        val_total=10,
        adv_pass=2,
        adv_total=4,
    )
    candidate = test_runner.SkillEvalResult(
        skill_name="mcum-orchestrator",
        skill_version="2.0.1",
        ckl_score=0.78,
        val_pass=7,
        val_total=10,
        adv_pass=4,
        adv_total=4,
    )
    evaluations = iter([baseline, candidate])
    status_updates: list[tuple[str | None, str, str | None, float | None]] = []

    monkeypatch.setattr(optimizer, "analyze_and_propose", lambda *args, **kwargs: optimizer.OptimizationReport(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        previous_ckl=baseline.ckl_score,
        target_ckl=0.85,
        proposals=[
            optimizer.ImprovementProposal(
                improvement_type="add_failure_warning",
                section_to_edit="Rules",
                current_text="",
                proposed_text="Warn before semantic retry.",
                confidence=0.9,
                evidence="failed adversarial test",
            )
        ],
        failure_patterns=[],
        cold_start_flag=False,
    ))
    monkeypatch.setattr(optimizer, "save_optimization_report", lambda report, version: {"id": "report-2", "version": "2.0.1"})
    monkeypatch.setattr(
        optimizer,
        "apply_high_confidence_improvements",
        lambda report, skill_md_path=None, dry_run=True, writeback_mode="disabled": [
            {"type": "add_failure_warning", "applied": True, "path": "skill.md", "backup_path": "skill.md.bak"}
        ],
    )
    monkeypatch.setattr(
        optimizer,
        "_update_skill_version_status",
        lambda version_id, status, note=None, ckl_score=None: status_updates.append((version_id, status, note, ckl_score)),
    )

    def fake_run_evaluation(skill_name, skill_version="1.0.0", partition=None, verbose=True):
        return next(evaluations)

    monkeypatch.setattr("MCUM.sisl.test_runner.run_evaluation", fake_run_evaluation)
    monkeypatch.setattr("MCUM.sisl.test_runner.save_eval_to_db", lambda eval_result: f"eval-{eval_result.skill_version}")

    result = optimizer.run_sisl_cycle(
        skill_name="mcum-orchestrator",
        skill_version="2.0.0",
        dry_run=False,
        persist_eval=True,
        writeback_mode="candidate",
        verbose=False,
    )

    assert result["gate_result"]["accepted"] is True
    assert result["ckl_score"] == 0.78
    assert result["applied"][0]["accepted"] is True
    assert status_updates[0][1] == "testing"


def test_run_sisl_cycle_accepts_candidate_when_ceiling_score_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = test_runner.SkillEvalResult(
        skill_name="html-dashboard-expert",
        skill_version="2.1.0",
        ckl_score=1.0,
        val_pass=5,
        val_total=5,
        adv_pass=2,
        adv_total=2,
    )
    candidate = test_runner.SkillEvalResult(
        skill_name="html-dashboard-expert",
        skill_version="2.1.1",
        ckl_score=1.0,
        val_pass=5,
        val_total=5,
        adv_pass=2,
        adv_total=2,
    )
    evaluations = iter([baseline, candidate])
    status_updates: list[tuple[str | None, str, str | None, float | None]] = []

    monkeypatch.setattr(optimizer, "analyze_and_propose", lambda *args, **kwargs: optimizer.OptimizationReport(
        skill_name="html-dashboard-expert",
        skill_version="2.1.0",
        previous_ckl=baseline.ckl_score,
        target_ckl=0.85,
        proposals=[
            optimizer.ImprovementProposal(
                improvement_type="add_failure_warning",
                section_to_edit="Directivas Deterministas",
                current_text="",
                proposed_text="Advertir antes de prometer backend en dashboards estáticos.",
                confidence=0.9,
                evidence="bootstrap dry-run produced a high-confidence guardrail",
            )
        ],
        failure_patterns=[],
        cold_start_flag=False,
    ))
    monkeypatch.setattr(optimizer, "save_optimization_report", lambda report, version: {"id": "report-3", "version": "2.1.1"})
    monkeypatch.setattr(
        optimizer,
        "apply_high_confidence_improvements",
        lambda report, skill_md_path=None, dry_run=True, writeback_mode="disabled": [
            {"type": "add_failure_warning", "applied": True, "path": "skill.md", "backup_path": "skill.md.bak"}
        ],
    )
    monkeypatch.setattr(
        optimizer,
        "_update_skill_version_status",
        lambda version_id, status, note=None, ckl_score=None: status_updates.append((version_id, status, note, ckl_score)),
    )

    def fake_run_evaluation(skill_name, skill_version="1.0.0", partition=None, verbose=True):
        return next(evaluations)

    monkeypatch.setattr("MCUM.sisl.test_runner.run_evaluation", fake_run_evaluation)
    monkeypatch.setattr("MCUM.sisl.test_runner.save_eval_to_db", lambda eval_result: f"eval-{eval_result.skill_version}")

    result = optimizer.run_sisl_cycle(
        skill_name="html-dashboard-expert",
        skill_version="2.1.0",
        dry_run=False,
        persist_eval=True,
        writeback_mode="candidate",
        verbose=False,
    )

    assert result["gate_result"]["accepted"] is True
    assert result["gate_result"]["note"] == "Candidate writeback preserved ceiling-level evaluation while codifying guardrails."
    assert result["applied"][0]["accepted"] is True
    assert status_updates[0][1] == "testing"


def test_analyze_and_propose_filters_failure_patterns_to_same_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_result = test_runner.SkillEvalResult(
        skill_name="html-dashboard-expert",
        skill_version="2.1.0",
        ckl_score=1.0,
        val_pass=5,
        val_total=5,
        adv_pass=2,
        adv_total=2,
    )
    monkeypatch.setattr(
        optimizer,
        "get_failure_patterns",
        lambda **kwargs: [
            {
                "id": "fp-own",
                "skill_name": "html-dashboard-expert",
                "title": "Own warning",
                "task_description": "Static dashboard without backend",
                "content": {"conclusion": "Do not promise backend."},
                "current_confidence": 0.74,
            },
            {
                "id": "fp-foreign",
                "skill_name": "ui-ux-pro-max",
                "title": "Foreign warning",
                "task_description": "Glassmorphism layout",
                "content": {"conclusion": "Different skill evidence."},
                "current_confidence": 0.81,
            },
        ],
    )

    report = optimizer.analyze_and_propose("html-dashboard-expert", eval_result)

    failure_warning_titles = [
        proposal.proposed_text
        for proposal in report.proposals
        if proposal.improvement_type == "add_failure_warning"
    ]
    assert len(failure_warning_titles) == 1
    assert "Own warning" in failure_warning_titles[0]
    assert all("Foreign warning" not in text for text in failure_warning_titles)


def test_resolve_writeback_mode_respects_targets_and_override() -> None:
    policy = {
        "autonomous_writeback": "candidate",
        "autonomous_writeback_targets": ["html-dashboard-expert"],
        "autonomous_writeback_exclude": ["blocked-skill"],
    }

    assert autonomous_loop.resolve_writeback_mode("html-dashboard-expert", policy) == "candidate"
    assert autonomous_loop.resolve_writeback_mode("mcum-orchestrator", policy) == "disabled"
    assert autonomous_loop.resolve_writeback_mode("blocked-skill", policy) == "disabled"
    assert autonomous_loop.resolve_writeback_mode("mcum-orchestrator", policy, requested_mode="candidate") == "candidate"


def test_run_autonomous_improvement_disables_writeback_for_non_targeted_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        autonomous_loop,
        "load_execution_policy",
        lambda: {
            "autonomous_writeback": "candidate",
            "autonomous_writeback_targets": ["html-dashboard-expert"],
            "autonomous_writeback_exclude": [],
            "sisl_mode": "db_only",
        },
    )
    monkeypatch.setattr(
        autonomous_loop,
        "get_skill_loop_stats",
        lambda skill_name: {
            "experience_count": 4,
            "test_count": 8,
            "retrieval_run_count": 3,
            "last_sisl_at": None,
        },
    )
    monkeypatch.setattr(autonomous_loop, "get_current_skill_version", lambda skill_name: "2.5.0")
    monkeypatch.setattr(
        autonomous_loop,
        "run_sisl_cycle",
        lambda **kwargs: captured.update(kwargs) or {
            "ckl_score": 0.81,
            "baseline_ckl_score": 0.75,
            "proposals_n": 1,
            "high_conf_n": 1,
            "applied": [],
            "eval_record_id": "eval-1",
            "candidate_eval_record_id": None,
            "report_id": "report-1",
            "gate_result": None,
        },
    )

    result = autonomous_loop.run_autonomous_improvement(
        skill_name="mcum-orchestrator",
        trigger="manual_test",
        verbose=False,
    )

    assert result["skipped"] is False
    assert captured["writeback_mode"] == "disabled"
