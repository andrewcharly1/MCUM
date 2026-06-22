from __future__ import annotations

from types import SimpleNamespace

from MCUM.core import pattern_discovery
from MCUM.policy import load_pattern_policy


def _experience(index: int, project_id: str, category: str = "failure_pattern") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "category": category,
        "title": f"Retry timeout recovery {index}",
        "content": {"conclusion": "Retry bounded operations after timeout and validate the result."},
        "applicability": {"when": "A bounded worker times out."},
        "current_confidence": 0.9,
        "unique_context_count": 1,
        "contradiction_penalty": 0.0,
        "conflict_refs": [],
        "project_id": project_id,
        "skill_name": "mcum-orchestrator",
        "task_description": f"recover timeout context {index}",
    }


def test_pattern_policy_excludes_regulatory_rules_and_enables_gated_auto_promotion() -> None:
    policy = load_pattern_policy()

    assert "regulatory_rule" in policy["eligibility"]["excluded_categories"]
    # Auto-promotion is now enabled, but it is a separate governed step that
    # re-checks every quality gate; discovery itself still runs in shadow and
    # never promotes (see test_run_pattern_discovery_stages_candidates_without_promoting).
    assert policy["auto_promote"] is True
    assert policy["mode"] == "shadow"


def test_auto_promote_skips_when_policy_disabled() -> None:
    result = pattern_discovery.auto_promote_ready_candidates(
        policy={"auto_promote": False}
    )
    assert result["status"] == "disabled"
    assert result["promoted"] == []


def test_auto_promote_activates_passing_and_leaves_failing_as_draft(monkeypatch) -> None:
    policy = {
        "auto_promote": True,
        "quality_gates": {"min_support": 3, "min_cohesion": 0.8},
        "lifecycle": {"candidate_ttl_days": 90},
    }
    candidates = [
        {"id": "cand-pass", "label": "good pattern"},
        {"id": "cand-fail", "label": "weak pattern"},
    ]
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "list_review_ready_candidates",
        lambda **kwargs: candidates,
    )
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "materialize_candidate_to_draft",
        lambda **kwargs: {"status": "draft_materialized", "pattern_id": f"pat-{kwargs['candidate_id']}"},
    )

    def _fake_activate(**kwargs):
        # The weak candidate fails a gate at activation time.
        if kwargs["pattern_id"] == "pat-cand-fail":
            raise ValueError("min_cohesion gate not met")
        return {"status": "activated", "pattern_id": kwargs["pattern_id"]}

    monkeypatch.setattr(pattern_discovery.pattern_store, "activate_pattern", _fake_activate)

    result = pattern_discovery.auto_promote_ready_candidates(policy=policy)

    assert result["status"] == "success"
    assert result["reviewed"] == 2
    assert [p["candidate_id"] for p in result["promoted"]] == ["cand-pass"]
    assert [r["candidate_id"] for r in result["rejected"]] == ["cand-fail"]
    assert "min_cohesion" in result["rejected"][0]["reason"]


def test_build_pattern_candidates_requires_quality_and_project_diversity() -> None:
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001"),
        _experience(2, "10000000-0000-0000-0000-000000000001"),
        _experience(3, "20000000-0000-0000-0000-000000000002"),
    ]
    embeddings = {
        experience["id"]: [1.0, 0.0, 0.0]
        for experience in experiences
    }

    candidates, findings = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=embeddings,
        policy=load_pattern_policy(),
    )

    assert findings["groups_analyzed"] == 1
    assert len(candidates) == 1
    assert candidates[0]["quality_ready"] is True
    assert candidates[0]["status"] == "review"
    assert candidates[0]["distinct_project_count"] == 2
    assert candidates[0]["contradiction_count"] == 0


def test_run_pattern_discovery_stages_candidates_without_promoting(monkeypatch) -> None:
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001"),
        _experience(2, "10000000-0000-0000-0000-000000000001"),
        _experience(3, "20000000-0000-0000-0000-000000000002"),
    ]
    persisted: list[dict] = []
    finished: list[dict] = []

    monkeypatch.setattr(pattern_discovery.pattern_store, "start_discovery_run", lambda **kwargs: "run-1")
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "finish_discovery_run",
        lambda discovery_run_id, **kwargs: finished.append({"id": discovery_run_id, **kwargs}) or True,
    )
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "fetch_eligible_experiences",
        lambda **kwargs: experiences,
    )
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "get_cached_experience_embeddings",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "upsert_experience_embeddings",
        lambda **kwargs: len(kwargs["rows"]),
    )
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "upsert_pattern_candidate",
        lambda **kwargs: persisted.append(kwargs) or "candidate-1",
    )
    monkeypatch.setattr(pattern_discovery.pattern_store, "expire_unseen_candidates", lambda **kwargs: 0)

    vector = [1.0] + ([0.0] * 383)
    result = pattern_discovery.run_pattern_discovery(
        policy=load_pattern_policy(),
        encode_texts=lambda texts, **kwargs: [vector for _ in texts],
    )

    assert result["status"] == "success"
    assert result["candidates_review_ready"] == 1
    assert len(persisted) == 1
    assert persisted[0]["candidate"]["status"] == "review"
    assert finished[-1]["status"] == "success"
    assert finished[-1]["findings"]["auto_promote"] is False


def test_workspace_parser_exposes_governed_pattern_commands() -> None:
    from MCUM import workspace_session

    discover = workspace_session._build_parser().parse_args(["pattern-discover", "--no-write"])
    health = workspace_session._build_parser().parse_args(["pattern-health"])
    accept = workspace_session._build_parser().parse_args(
        ["pattern-accept", "--candidate-id", "candidate-1", "--reviewed-by", "reviewer"]
    )
    activate = workspace_session._build_parser().parse_args(
        ["pattern-activate", "--pattern-id", "pattern-1", "--reviewed-by", "reviewer"]
    )

    assert discover.mode == "pattern-discover"
    assert health.mode == "pattern-health"
    assert accept.confirm is False
    assert activate.confirm is False


def test_maintenance_action_runs_pattern_discovery_in_safe_shadow_mode(monkeypatch) -> None:
    from MCUM import workspace_session

    discovery_calls: list[dict] = []
    monkeypatch.setattr(workspace_session, "load_pattern_policy", lambda: {"mode": "shadow"})
    monkeypatch.setattr(
        workspace_session,
        "run_pattern_discovery",
        lambda **kwargs: discovery_calls.append(kwargs) or {
                "status": "success",
                "mode": "shadow",
                "discovery_run_id": "run-1",
                "experiences_scanned": 10,
                "embeddings_generated": 0,
                "embeddings_reused": 10,
                "groups_analyzed": 2,
                "candidates_observed": 1,
                "candidates_review_ready": 0,
            },
    )

    result = workspace_session._execute_maintenance_action(
        action_name="analyze_pattern_candidates",
        project_id="project-1",
        maintenance_policy={},
        args=SimpleNamespace(maintenance_name="daily_guard"),
    )

    assert result["status"] == "success"
    assert result["risk"] == "low"
    assert result["audit"]["classification"] == "safe_shadow_analysis"
    assert result["audit"]["auto_promote"] is False
    assert discovery_calls[0]["project_id"] == "project-1"
