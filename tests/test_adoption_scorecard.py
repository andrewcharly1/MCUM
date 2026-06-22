from __future__ import annotations

from MCUM.core import adoption_scorecard


def test_assessment_uses_applied_playbooks_and_real_pattern_usage() -> None:
    assessment = adoption_scorecard.assess_adoption(
        {
            "playbooks": {"total": 100, "never_reused": 60},
            "patterns": {"eligible_for_health": 0, "observing": 1},
            "skills": {"active_without_evidence": 2},
            "retrievals": {"total": 100, "failure": 10},
        }
    )

    assert assessment["playbook_never_reused_ratio"] == 0.6
    assert assessment["playbook_target_met"] is False
    assert assessment["patterns_observing"] == 1
    assert assessment["retrieval_failure_ratio"] == 0.1
    assert len(assessment["recommendations"]) == 4


def test_assessment_does_not_invent_failures_for_empty_metrics() -> None:
    assessment = adoption_scorecard.assess_adoption({})

    assert assessment["playbook_never_reused_ratio"] == 0.0
    assert assessment["retrieval_failure_ratio"] == 0.0
    assert assessment["recommendations"] == []


def test_build_scorecard_marks_low_sample_patterns_as_observing(monkeypatch) -> None:
    rows = iter(
        [
            {"total": 10, "never_reused": 6, "reused": 4},
            {"total": 1, "active": 1, "eligible_for_health": 0, "observing": 1, "real_usage_events": 1},
            {"active": 3, "active_without_evidence": 1},
            {"total": 20, "success": 15, "partial": 3, "failure": 2, "unknown": 0},
        ]
    )

    class Cursor:
        def execute(self, _sql):
            return None

        def fetchone(self):
            return next(rows)

    class Context:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(adoption_scorecard, "get_db", lambda: Context(object()))
    monkeypatch.setattr(adoption_scorecard, "get_cursor", lambda _conn: Context(Cursor()))

    result = adoption_scorecard.build_adoption_scorecard()

    assert result["status"] == "observing"
    assert result["anti_vanity"]["pattern_health_min_real_uses"] == 5
    assert result["assessment"]["patterns_eligible_for_health_decision"] == 0
