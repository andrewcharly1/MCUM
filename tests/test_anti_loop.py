from __future__ import annotations

from datetime import datetime, timezone

from MCUM import anti_loop


def test_analyze_problem_loop_detects_repeated_failures(monkeypatch) -> None:
    problem_fingerprint, _, _ = anti_loop._fingerprint(
        "problem",
        "Repair wrapper safely",
        "corregir",
        "repair wrapper",
    )
    rows = [
        {
            "title": "old task",
            "description": None,
            "outcome": "failure",
            "outcome_details": "wrapper failed",
            "skill_used": "mcum-orchestrator",
            "created_at": datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
            "log_metadata": {"anti_loop": {"problem_fingerprint": problem_fingerprint}},
        },
        {
            "title": "old task 2",
            "description": None,
            "outcome": "partial",
            "outcome_details": "wrapper partial",
            "skill_used": "mcum-orchestrator",
            "created_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
            "log_metadata": {"anti_loop": {"problem_fingerprint": problem_fingerprint}},
        },
    ]
    monkeypatch.setattr(anti_loop, "_fetch_recent_task_rows", lambda *args, **kwargs: rows)

    state = anti_loop.analyze_problem_loop(
        project_id="project-1",
        task_description="Repair wrapper safely",
        task_brief={"task_type": "corregir", "objective": "repair wrapper"},
        policy={"enabled": True, "repeat_problem_threshold": 2},
    )

    assert state["repeated_problem_total"] == 2
    assert state["repeated_problem_failures"] == 2
    assert state["recommendation"] == "increase_validation_and_diverge"
    assert state["risk_level"] in {"medium", "high"}


def test_enrich_loop_state_with_strategy_warns_on_repeated_strategy_failures() -> None:
    problem_fingerprint, _, _ = anti_loop._fingerprint("problem", "repair wrapper")
    strategy_fingerprint, _, _ = anti_loop._fingerprint(
        "strategy",
        "mcum-orchestrator",
        "semantic",
        "semantic_project",
        "ejecutar",
        "same_project",
        "single_agent",
    )
    state = {
        "enabled": True,
        "problem_fingerprint": problem_fingerprint,
        "problem_signature": "repair wrapper",
        "loop_risk": 0.3,
        "recommendation": "observe_only",
        "warnings": [],
        "_recent_rows": [
            {
                "outcome": "failure",
                "skill_used": "mcum-orchestrator",
                "log_metadata": {
                    "anti_loop": {
                        "problem_fingerprint": problem_fingerprint,
                        "strategy_fingerprint": strategy_fingerprint,
                    },
                    "final_skill": "mcum-orchestrator",
                },
            },
            {
                "outcome": "partial",
                "skill_used": "mcum-orchestrator",
                "log_metadata": {
                    "anti_loop": {
                        "problem_fingerprint": problem_fingerprint,
                        "strategy_fingerprint": strategy_fingerprint,
                    },
                    "final_skill": "mcum-orchestrator",
                },
            },
            {
                "outcome": "success",
                "skill_used": "validator-skill",
                "log_metadata": {
                    "anti_loop": {"problem_fingerprint": problem_fingerprint},
                    "final_skill": "validator-skill",
                },
            },
        ],
    }

    enriched = anti_loop.enrich_loop_state_with_strategy(
        loop_state=state,
        skill_name="mcum-orchestrator",
        dispatch_method="semantic",
        retrieval_mode="semantic_project",
        execution_mode="ejecutar",
        playbook_scope="same_project",
        orchestration={"mode": "single_agent"},
        policy={"enabled": True, "repeat_strategy_failure_threshold": 2},
    )

    assert enriched["repeated_strategy_failures"] == 2
    assert enriched["recommendation"] == "switch_strategy_before_retry"
    assert "validator-skill" in enriched["alternate_success_skills"]
    assert "_recent_rows" not in enriched


def test_finalize_loop_state_counts_recurring_error_family(monkeypatch) -> None:
    error_fingerprint, _, _ = anti_loop._fingerprint(
        "error",
        "failure",
        "UnicodeEncodeError",
        "safe print failed",
    )
    monkeypatch.setattr(
        anti_loop,
        "_fetch_recent_task_rows",
        lambda *args, **kwargs: [
            {
                "outcome": "failure",
                "outcome_details": "UnicodeEncodeError",
                "description": None,
                "skill_used": "mcum-orchestrator",
                "log_metadata": {"anti_loop": {"error_fingerprint": error_fingerprint}},
                "created_at": datetime(2026, 4, 2, 8, 0, tzinfo=timezone.utc),
            },
            {
                "outcome": "partial",
                "outcome_details": "UnicodeEncodeError",
                "description": None,
                "skill_used": "mcum-orchestrator",
                "log_metadata": {"anti_loop": {"error_fingerprint": error_fingerprint}},
                "created_at": datetime(2026, 4, 2, 7, 0, tzinfo=timezone.utc),
            },
        ],
    )

    state = anti_loop.finalize_loop_state(
        project_id="project-1",
        loop_state={"enabled": True, "loop_risk": 0.4, "recommendation": "observe_only", "warnings": []},
        result_outcome="failure",
        result_error_description="UnicodeEncodeError",
        result_validation_summary="safe print failed",
        result_metadata={},
        policy={"enabled": True, "repeat_error_threshold": 2},
    )

    assert state["repeated_error_failures"] == 2
    assert state["recommendation"] == "elevate_error_memory"
    assert state["risk_level"] in {"medium", "high"}
