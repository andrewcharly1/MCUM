from __future__ import annotations

import uuid

from MCUM.core import pattern_discovery
from MCUM.db import experience_store, pattern_store, session_playbooks
from MCUM.db.connection import get_cursor, get_db
from MCUM.policy import load_pattern_policy


def _seed_inputs(skill_name: str) -> tuple[str, list[dict], dict[str, list[float]]]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT id FROM project_registry.projects ORDER BY created_at LIMIT 1")
            project = cur.fetchone()
            assert project is not None
            cur.execute(
                """
                SELECT id
                FROM core_brain.experiences
                WHERE superseded_by IS NULL
                ORDER BY created_at
                LIMIT 3
                """
            )
            experience_ids = [str(row["id"]) for row in cur.fetchall()]
    assert len(experience_ids) == 3
    project_id = str(project["id"])
    experiences = [
        {
            "id": experience_id,
            "category": "implementation_recipe",
            "title": f"QA full loop operational pattern {index}",
            "content": {"conclusion": "Close the governed pattern loop end to end."},
            "current_confidence": 0.90,
            "contradiction_penalty": 0.0,
            "conflict_refs": [],
            "project_id": project_id,
            "skill_name": skill_name,
            "task_description": f"qa full loop distinct context {index}",
            "created_at": f"2026-01-0{index}T00:00:00Z",
        }
        for index, experience_id in enumerate(experience_ids, start=1)
    ]
    embeddings = {experience_id: [1.0, 0.0, 0.0] for experience_id in experience_ids}
    return project_id, experiences, embeddings


def _cleanup(
    *,
    playbook_id: str | None,
    pattern_id: str | None,
    candidate_id: str | None,
    discovery_run_id: str | None,
) -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if playbook_id:
                cur.execute("DELETE FROM core_brain.session_playbooks WHERE id = %s", (playbook_id,))
            if pattern_id:
                cur.execute("DELETE FROM core_brain.patterns WHERE id = %s", (pattern_id,))
            if candidate_id:
                cur.execute("DELETE FROM core_brain.pattern_candidates WHERE id = %s", (candidate_id,))
            if discovery_run_id:
                cur.execute(
                    "DELETE FROM core_brain.pattern_discovery_runs WHERE id = %s",
                    (discovery_run_id,),
                )


def test_full_loop_end_to_end() -> None:
    suffix = uuid.uuid4().hex[:8]
    skill_name = f"qa-full-loop-{suffix}"
    project_id, experiences, embeddings = _seed_inputs(skill_name)
    policy = load_pattern_policy()
    discovery_run_id = pattern_store.start_discovery_run(
        scope_type="project",
        project_id=project_id,
        mode=str(policy["mode"]),
        policy_version=str(policy["_meta"]["version"]),
        algorithm_version=str(policy["clustering"]["algorithm_version"]),
        embedding_model=str(policy["embedding"]["model_name"]),
    )
    candidate_id = None
    pattern_id = None
    playbook_id = None
    try:
        candidates, findings = pattern_discovery.build_pattern_candidates(
            experiences=experiences,
            embeddings=embeddings,
            policy=policy,
            project_id=project_id,
        )
        assert findings["groups_analyzed"] == 1
        assert len(candidates) == 1
        assert candidates[0]["status"] == "review"
        candidate_id = pattern_store.upsert_pattern_candidate(
            candidate=candidates[0],
            evidence=candidates[0]["evidence"],
            centroid_embedding=candidates[0]["centroid_embedding"],
            discovery_run_id=discovery_run_id,
        )
        pattern_store.finish_discovery_run(
            discovery_run_id,
            status="success",
            metrics={
                "experiences_scanned": 3,
                "groups_analyzed": 1,
                "candidates_observed": 1,
                "candidates_review_ready": 1,
            },
        )

        draft = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id,
            reviewed_by="qa-full-loop",
        )
        pattern_id = draft["pattern_id"]
        activated = pattern_store.activate_pattern(
            pattern_id=pattern_id,
            reviewed_by="qa-full-loop",
            quality_gates=policy["quality_gates"],
        )
        assert activated["status"] == "active"

        active_patterns = experience_store.get_active_patterns(
            query_text="qa full loop operational pattern",
            project_id=project_id,
            skill_name=skill_name,
            limit=3,
        )
        assert pattern_id in {str(item["id"]) for item in active_patterns}

        playbook_id = session_playbooks.save_session_playbook(
            project_id=project_id,
            skill_name=skill_name,
            task_description=f"qa full loop task {suffix}",
            title=f"QA full loop playbook {suffix}",
            output_summary="Governed pattern loop completed and validated.",
            validation_summary="Pattern retrieved, playbook linked, usage recorded.",
            outcome="success",
            confidence_score=0.95,
            pattern_ids=[pattern_id],
        )
        playbooks = session_playbooks.retrieve_session_playbooks(
            f"qa full loop task {suffix}",
            project_id=project_id,
            skill_name=skill_name,
            min_similarity=0.0,
            limit=10,
            active_pattern_ids=[pattern_id],
        )["playbooks"]
        linked = next(item for item in playbooks if str(item["id"]) == playbook_id)
        assert linked["pattern_ids"] == [pattern_id]
        assert linked["pattern_alignment_score"] == 1.0

        usage = pattern_store.record_pattern_usage_events(
            pattern_ids=[pattern_id],
            project_id=project_id,
            session_id=f"qa-session-{suffix}",
            log_id=None,
            outcome="success",
            user_feedback=1,
        )
        assert usage["events_recorded"] == 1
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    "SELECT status, usage_count, utility_score, health_state "
                    "FROM core_brain.patterns WHERE id = %s",
                    (pattern_id,),
                )
                pattern = dict(cur.fetchone() or {})
        assert pattern["status"] == "active"
        assert int(pattern["usage_count"]) == 1
        assert float(pattern["utility_score"]) > 0.0
        assert pattern["health_state"] != "degraded"
    finally:
        _cleanup(
            playbook_id=playbook_id,
            pattern_id=pattern_id,
            candidate_id=candidate_id,
            discovery_run_id=discovery_run_id,
        )
