from __future__ import annotations

import json
from types import SimpleNamespace

from MCUM.core.session_manager import OrchestratorSession, TaskResult
from MCUM.db import experience_store


def _vector(value: float) -> list[float]:
    return [value] * experience_store.EMBEDDING_DIM


class _CursorStub:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []
        self.executed: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def fetchone(self) -> dict | None:
        if not self._rows:
            return None
        return self._rows[0]


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


def test_get_active_patterns_prefers_query_relevant_pattern(monkeypatch) -> None:
    candidates = [
        {
            "id": "pat-1",
            "name": "Wrapper hardening",
            "description": "Protect Windows wrapper output and session closing.",
            "category": "implementation_recipe",
            "status": "active",
            "promotion_criteria_met": True,
            "experience_count": 4,
            "avg_score": 0.92,
            "context_diversity": 3,
            "deprecated_at": None,
            "deprecated_reason": None,
            "replacement_pattern_id": None,
            "created_at": None,
            "updated_at": None,
            "evidence_ids": ["exp-1"],
            "evidence_projects": ["project-1"],
            "evidence_skills": ["mcum-orchestrator"],
        },
        {
            "id": "pat-2",
            "name": "Database cleanup",
            "description": "Maintain archived snapshots.",
            "category": "architecture_pattern",
            "status": "active",
            "promotion_criteria_met": True,
            "experience_count": 5,
            "avg_score": 0.97,
            "context_diversity": 4,
            "deprecated_at": None,
            "deprecated_reason": None,
            "replacement_pattern_id": None,
            "created_at": None,
            "updated_at": None,
            "evidence_ids": ["exp-2"],
            "evidence_projects": ["project-1"],
            "evidence_skills": ["mcum-orchestrator"],
        },
    ]
    monkeypatch.setattr(experience_store, "_fetch_active_pattern_candidates", lambda **kwargs: list(candidates))

    patterns = experience_store.get_active_patterns(
        query_text="wrapper hardening for windows output",
        project_id="project-1",
        skill_name="mcum-orchestrator",
        limit=2,
    )

    assert [item["id"] for item in patterns] == ["pat-1", "pat-2"]
    assert patterns[0]["_combined_score"] > patterns[1]["_combined_score"]


def test_get_recent_feedback_signals_parses_roles(monkeypatch) -> None:
    rows = [
        {
            "id": "run-1",
            "session_id": "session-1",
            "skill_name": "mcum-orchestrator",
            "input_context": "fix wrapper output",
            "experiences_retrieved": ["exp-1"],
            "patterns_retrieved": ["pat-1"],
            "retrieval_scores": json.dumps(
                [
                    {"id": "exp-1", "role": "experience"},
                    {"id": "pat-1", "role": "active_pattern"},
                    {"id": "fp-1", "role": "failure_pattern"},
                    {"id": "cf-1", "role": "conflict_case"},
                ]
            ),
            "outcome_status": "failure",
            "outcome_description": "unicode wrapper bug",
            "user_feedback": -1,
            "failure_reason": "console encoding",
            "project_id": "project-1",
            "created_at": None,
        }
    ]
    cursor = _CursorStub(rows)
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    feedback = experience_store.get_recent_feedback_signals(
        query_text="wrapper bug",
        project_id="project-1",
        skill_name="mcum-orchestrator",
        limit=5,
    )

    assert feedback["negative_experience_ids"] == ["exp-1"]
    assert feedback["negative_pattern_ids"] == ["cf-1", "fp-1", "pat-1"]
    assert feedback["summary"]["negative_n"] == 1
    assert feedback["signals"][0]["failure_reason"] == "console encoding"


def test_retrieve_for_task_applies_patterns_and_feedback(monkeypatch) -> None:
    monkeypatch.setattr(
        experience_store,
        "semantic_search",
        lambda *args, **kwargs: [
            {"id": "exp-keep", "title": "Keep", "content": {"conclusion": "keep"}, "conflict_refs": []},
            {"id": "exp-drop", "title": "Drop", "content": {"conclusion": "drop"}, "conflict_refs": []},
        ],
    )
    monkeypatch.setattr(experience_store, "search_by_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        experience_store,
        "get_failure_patterns",
        lambda *args, **kwargs: [
            {"id": "fp-1", "title": "Failure", "content": {"conclusion": "failure"}, "conflict_refs": []}
        ],
    )
    monkeypatch.setattr(
        experience_store,
        "get_active_patterns",
        lambda *args, **kwargs: [
            {
                "id": "pat-1",
                "name": "Wrapper hardening",
                "description": "Protect Windows wrapper output and session closing.",
                "category": "implementation_recipe",
                "status": "active",
                "experience_count": 4,
                "avg_score": 0.91,
                "context_diversity": 3,
                "evidence_ids": ["exp-keep"],
                "evidence_projects": ["project-1"],
                "evidence_skills": ["mcum-orchestrator"],
                "_combined_score": 0.88,
            }
        ],
    )
    monkeypatch.setattr(
        experience_store,
        "get_recent_feedback_signals",
        lambda *args, **kwargs: {
            "signals": [
                {
                    "id": "run-1",
                    "user_feedback": -1,
                    "outcome_status": "failure",
                    "outcome_description": "bad run",
                    "decision_taken": "selected_skill=mcum-orchestrator",
                    "failure_reason": "console",
                    "created_at": None,
                    "experience_ids": ["exp-drop"],
                    "pattern_ids": ["pat-1"],
                    "failure_pattern_ids": ["fp-1"],
                    "conflict_ids": [],
                }
            ],
            "positive_experience_ids": ["exp-keep"],
            "negative_experience_ids": ["exp-drop"],
            "positive_pattern_ids": ["pat-1"],
            "negative_pattern_ids": [],
            "summary": {"signals_n": 1, "positive_n": 0, "negative_n": 1},
        },
    )
    monkeypatch.setattr(experience_store, "_estimate_retrieval_item_tokens", lambda item: 5)

    result = experience_store.retrieve_for_task(
        "fix wrapper output",
        skill_context="mcum-orchestrator",
        project_id="project-1",
        policy={
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "top_relevant_slots": 2,
            "conflict_slot": 0,
            "failure_pattern_slot": 1,
            "pattern_slot": 1,
            "feedback_signal_slot": 5,
            "max_token_budget": 100,
        },
    )

    assert [item["id"] for item in result["experiences"]] == ["exp-keep"]
    assert [item["id"] for item in result["active_patterns"]] == ["pat-1"]
    assert [item["id"] for item in result["failure_patterns"]] == ["fp-1"]
    assert result["feedback_signals"]["summary"]["signals_n"] == 1
    assert any("Human feedback active" in warning for warning in result["warnings"])


def test_record_retrieval_run_records_pattern_roles(monkeypatch) -> None:
    cursor = _CursorStub([{ "id": "run-1" }])
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    run_id = experience_store.record_retrieval_run(
        session_id="session-1",
        project_id="project-1",
        skill_name="mcum-orchestrator",
        input_context="fix wrapper output",
        retrieval_result={
            "experiences": [{"id": "exp-1", "category": "implementation_recipe"}],
            "failure_patterns": [{"id": "fp-1", "category": "failure_pattern"}],
            "conflict_cases": [{"id": "cf-1", "category": "implementation_recipe"}],
            "active_patterns": [{"id": "pat-1", "category": "architecture_pattern"}],
            "policy_applied": {"pattern_slot": 1},
        },
        decision_taken="selected_skill=mcum-orchestrator",
        final_confidence=0.93,
    )

    assert run_id == "run-1"
    _, params = cursor.executed[-1]
    assert params[4] == ["exp-1"]
    assert params[5] == ["pat-1"]
    scores = json.loads(params[6])
    assert {entry["role"] for entry in scores} == {
        "experience",
        "failure_pattern",
        "conflict_case",
        "active_pattern",
    }


def test_reinforce_retrieval_confidence_uses_pattern_evidence(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(
        retrieved_experiences=[],
        failure_patterns=[],
        conflict_cases=[],
        active_patterns=[{"id": "pat-1", "evidence_ids": ["exp-link"]}],
    )
    session._log = lambda message: None  # type: ignore[method-assign]

    calls: list[tuple[str, float, bool, bool]] = []

    def fake_adjust_confidence(*, experience_id, delta, revalidated=True, new_context=False):
        calls.append((experience_id, delta, revalidated, new_context))
        return True

    monkeypatch.setattr(experience_store, "adjust_confidence", fake_adjust_confidence)
    monkeypatch.setattr("MCUM.core.session_manager.adjust_confidence", fake_adjust_confidence)

    OrchestratorSession._reinforce_retrieval_confidence(session, "success", context_effectiveness={"items": []})

    assert calls == [("exp-link", 0.02, True, True)]


def test_finalize_retrieval_run_persists_user_feedback(monkeypatch) -> None:
    cursor = _CursorStub([{ "id": "run-1" }])
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    updated = experience_store.finalize_retrieval_run(
        retrieval_run_id="run-1",
        outcome_status="success",
        outcome_description="done",
        final_confidence=0.9,
        failure_reason=None,
        user_feedback=1,
    )

    assert updated is True
    _, params = cursor.executed[-1]
    assert params[4] == 1


def test_close_forwards_user_feedback_to_finalize(monkeypatch) -> None:
    session = OrchestratorSession.__new__(OrchestratorSession)
    session._ctx = SimpleNamespace(
        session_id="session-1",
        project_id="project-1",
        project_name="Project",
        task_description="fix wrapper output",
        skill_selected="mcum-orchestrator",
        dispatch_result=SimpleNamespace(match_method="semantic", triggered_by="test"),
        auto_dispatch_result=None,
        retrieved_experiences=[],
        failure_patterns=[],
        conflict_cases=[],
        active_patterns=[],
        feedback_signals={},
        retrieval_mode="semantic",
        session_start_ts=0.0,
        log_id="log-1",
        retrieval_run_id="run-1",
        warnings=[],
        task_brief={"execution_mode": "ejecutar"},
        project_scope="same_project",
        playbooks=[],
        playbook_scope="none",
        retrieval_latency_ms=0,
        skill_status="active",
        compiled_state=SimpleNamespace(
            estimated_tokens=0,
            to_metadata=lambda: {"mode": "stub"},
        ),
        retrieval_scope_learning=None,
        dispatch_learning_profile=None,
    )
    session._start_ts = 0.0
    session._closed = False
    session.session_id = "session-1"
    session.task_description = "fix wrapper output"
    session.task_brief = {"execution_mode": "ejecutar"}
    session.project_path = "C:/tmp/project"
    session.execution_policy = {"strict_mode": False}
    session.auto_improve = False
    session.force_skill = None
    session.verbose = False
    session._apply_result_policy = lambda result: result  # type: ignore[method-assign]
    session._build_orchestrated_skills = lambda result: []  # type: ignore[method-assign]
    session._build_skill_correction_metadata = lambda result, orchestrated_skills: {  # type: ignore[method-assign]
        "delegated_skills": []
    }
    session._dispatch_result_payload = lambda result: None  # type: ignore[method-assign]
    session._save_session_playbook = lambda result, log_id: None  # type: ignore[method-assign]
    session._reinforce_retrieval_confidence = lambda outcome, context_effectiveness=None: None  # type: ignore[method-assign]
    session._run_autonomous_improvement = lambda skill_name: None  # type: ignore[method-assign]
    session._run_skill_factory_cycle = lambda: None  # type: ignore[method-assign]

    finalize_calls: list[dict] = []
    monkeypatch.setattr(
        "MCUM.core.session_manager.finalize_retrieval_run",
        lambda **kwargs: finalize_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.log_entry",
        lambda **kwargs: "task-log-1",
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.log_session_end",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "MCUM.core.session_manager.mark_skill_used",
        lambda skill_name: True,
    )

    close_result = OrchestratorSession.close(
        session,
        TaskResult(
            task_description="fix wrapper output",
            skill_used="mcum-orchestrator",
            outcome="success",
            confidence_score=0.95,
            output_summary="done",
            user_feedback=-1,
        ),
    )

    assert close_result["log_id"] == "task-log-1"
    assert close_result["record_status"] == "recorded"
    assert finalize_calls[0]["user_feedback"] == -1
