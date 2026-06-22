from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

from MCUM import workspace_session
from MCUM.db import pattern_store
from MCUM.db.connection import get_cursor, get_db


def _insert_candidate(*, quality_ready: bool = True, status: str = "review") -> str:
    candidate_id = str(uuid.uuid4())
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.pattern_candidates (
                    id, candidate_key, category, skill_name, scope_type,
                    label, summary, status, support_count,
                    distinct_project_count, context_diversity, cohesion_score,
                    contradiction_count, avg_confidence, quality_score,
                    quality_ready, metadata
                ) VALUES (
                    %s, %s, 'implementation_recipe', 'qa-mcum-phase4', 'skill',
                    %s, %s, %s, 5,
                    2, 5, 0.92,
                    0, 0.88, 0.91,
                    %s, %s::jsonb
                )
                """,
                (
                    candidate_id,
                    f"test:phase4:{candidate_id}",
                    f"QA Phase 4 {candidate_id[:8]}",
                    "Review-ready candidate for phase 4 validation.",
                    status,
                    quality_ready,
                    json.dumps(
                        {
                            "quality_gates": {
                                "support": True,
                                "context_diversity": True,
                                "project_diversity": True,
                                "cohesion": True,
                                "open_conflicts": True,
                            }
                        }
                    ),
                ),
            )
    return candidate_id


def _attach_real_evidence(candidate_id: str) -> dict[str, Any]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT id, title FROM core_brain.experiences ORDER BY created_at DESC LIMIT 1")
            experience = dict(cur.fetchone() or {})
            assert experience
            cur.execute(
                """
                INSERT INTO core_brain.pattern_candidate_evidence (
                    candidate_id, experience_id, evidence_role, similarity, weight
                ) VALUES (%s, %s, 'support', 0.95, 0.90)
                """,
                (candidate_id, experience["id"]),
            )
    return experience


def _cleanup_candidates(candidate_ids: list[str]) -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "DELETE FROM core_brain.pattern_candidate_evidence WHERE candidate_id = ANY(%s::uuid[])",
                (candidate_ids,),
            )
            cur.execute(
                "DELETE FROM core_brain.pattern_candidates WHERE id = ANY(%s::uuid[])",
                (candidate_ids,),
            )


def test_pattern_review_lists_only_quality_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        workspace_session,
        "get_activation_backlog",
        lambda **kwargs: {
            "available": True,
            "count": 1,
            "oldest_age_days": 2,
            "items": [{"id": "ready-1"}],
            "listed_count": 1,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "list_review_ready_candidates",
        lambda **kwargs: [{"id": "ready-1", "status": "review"}],
    )

    exit_code = workspace_session._run_pattern_review(
        SimpleNamespace(project_scope=False, limit=10, max_age_days=90)
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["count"] == 1
    assert payload["listed_count"] == 1
    assert payload["candidates"][0]["id"] == "ready-1"
    assert "pattern-accept" in payload["candidates"][0]["suggested_command"]


def test_review_ready_candidates_include_top_evidence_and_gate_summary() -> None:
    candidate_id = _insert_candidate()
    experience = _attach_real_evidence(candidate_id)
    try:
        candidates = pattern_store.list_review_ready_candidates(limit=100)
        candidate = next(item for item in candidates if str(item["id"]) == candidate_id)

        assert candidate["top_evidence"][0]["title"] == experience["title"]
        assert candidate["top_evidence"][0]["evidence_role"] == "support"
        assert candidate["review_summary"]["failed_gates"] == []
        assert "support" in candidate["review_summary"]["passed_gates"]
        assert candidate["review_summary"]["risks"] == []
    finally:
        _cleanup_candidates([candidate_id])


def test_activation_backlog_count_is_not_truncated_by_item_limit() -> None:
    before = pattern_store.get_activation_backlog(limit=1)
    candidate_ids = [_insert_candidate() for _ in range(3)]
    try:
        after = pattern_store.get_activation_backlog(limit=1)
        assert after["count"] == before["count"] + 3
        assert after["listed_count"] == 1
        assert len(after["items"]) == 1
    finally:
        _cleanup_candidates(candidate_ids)


def test_maintenance_reports_activation_backlog(monkeypatch) -> None:
    monkeypatch.setattr(workspace_session, "load_pattern_policy", lambda: {"mode": "shadow"})
    monkeypatch.setattr(
        workspace_session,
        "run_pattern_discovery",
        lambda **kwargs: {
            "status": "success",
            "mode": "shadow",
            "discovery_run_id": "run-1",
            "experiences_scanned": 10,
            "embeddings_generated": 0,
            "embeddings_reused": 10,
            "groups_analyzed": 2,
            "candidates_observed": 1,
            "candidates_review_ready": 1,
        },
    )
    monkeypatch.setattr(
        workspace_session,
        "get_activation_backlog",
        lambda **kwargs: {
            "available": True,
            "count": 1,
            "oldest_age_days": 2,
            "items": [{"id": "candidate-1"}],
            "listed_count": 1,
        },
    )

    result = workspace_session._execute_maintenance_action(
        action_name="analyze_pattern_candidates",
        project_id="project-1",
        maintenance_policy={
            "pattern_intelligence": {
                "activation_backlog_limit": 10,
                "activation_backlog_max_age_days": 30,
            }
        },
        args=SimpleNamespace(maintenance_name="daily_guard"),
    )

    backlog = result["findings"]["activation_backlog"]
    assert backlog["count"] == 1
    assert backlog["oldest_age_days"] == 2
    assert result["audit"]["manual_review_required"] is True
