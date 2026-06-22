from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from MCUM.core import pattern_discovery
from MCUM.db import pattern_store, session_playbooks
from MCUM.policy import load_pattern_policy


class CursorScript:
    def __init__(self, *, one: list[Any] | None = None, all_rows: list[list[Any]] | None = None) -> None:
        self.one = list(one or [])
        self.all_rows = list(all_rows or [])
        self.executed: list[tuple[str, Any]] = []
        self.executemany_calls: list[tuple[str, Any]] = []
        self.rowcount = 3

    def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    def executemany(self, query: str, params: Any) -> None:
        self.executemany_calls.append((query, params))

    def fetchone(self) -> Any:
        return self.one.pop(0) if self.one else None

    def fetchall(self) -> list[Any]:
        return self.all_rows.pop(0) if self.all_rows else []


class Manager:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _wire_cursor(monkeypatch: pytest.MonkeyPatch, module: Any, cursor: CursorScript) -> None:
    monkeypatch.setattr(module, "get_db", lambda: Manager(SimpleNamespace()))
    monkeypatch.setattr(module, "get_cursor", lambda conn: Manager(cursor))


def _exp(index: int, *, project: str = "p1", skill: str = "s1") -> dict[str, Any]:
    return {
        "id": f"e{index}",
        "category": "implementation_recipe",
        "title": f"Repeat validated operation {index}",
        "content": {"conclusion": "Repeat validated operation safely."},
        "applicability": {"when": "needed"},
        "task_description": f"distinct context {index}",
        "current_confidence": 0.9,
        "contradiction_penalty": 0.0,
        "conflict_refs": [],
        "project_id": project,
        "skill_name": skill,
        "created_at": str(index),
    }


def test_pattern_discovery_pure_helper_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pattern_discovery._json_value(None, {"x": 1}) == {"x": 1}
    assert pattern_discovery._json_value({"x": 2}, {}) == {"x": 2}
    assert pattern_discovery._json_value("[1, 2]", []) == [1, 2]
    assert pattern_discovery._json_value("{bad", []) == []
    assert pattern_discovery._json_value(3, "fallback") == "fallback"
    assert pattern_discovery._normalize_vector([0.0, 0.0]) == [0.0, 0.0]
    with pytest.raises(ValueError, match="dimension mismatch"):
        pattern_discovery._cosine([1.0], [1.0, 0.0])
    assert pattern_discovery._centroid([]) == []
    with pytest.raises(ValueError, match="same dimension"):
        pattern_discovery._centroid([[1.0], [1.0, 0.0]])
    assert pattern_discovery._connected_components([[1.0, 0.0], [1.0, 0.0]], 0.9) == [[0, 1]]
    assert pattern_discovery._candidate_label("failure_pattern", "s1", []) == "s1: failure pattern"

    fake_model = SimpleNamespace(
        encode=lambda texts, **kwargs: [SimpleNamespace(tolist=lambda: [1.0, 0.0]) for _ in texts]
    )
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=lambda model_name: fake_model),
    )
    assert pattern_discovery._encode_semantic_texts(["x"], model_name="m", batch_size=1) == [[1.0, 0.0]]


def test_pattern_discovery_candidate_skip_and_conflict_branches() -> None:
    candidates, findings = pattern_discovery.build_pattern_candidates(
        experiences=[_exp(1), _exp(2)],
        embeddings={"e1": [1.0, 0.0], "e2": [1.0, 0.0]},
        policy=load_pattern_policy(),
    )
    assert candidates == []
    assert findings["groups_analyzed"] == 1

    policy = load_pattern_policy()
    policy["clustering"]["max_group_size"] = 3
    too_large = [_exp(i) for i in range(4)]
    candidates, findings = pattern_discovery.build_pattern_candidates(
        experiences=too_large,
        embeddings={item["id"]: [1.0, 0.0] for item in too_large},
        policy=policy,
    )
    assert candidates == []
    assert findings["groups_skipped_too_large"][0]["size"] == 4

    policy = load_pattern_policy()
    policy["quality_gates"]["project_diversity_mode"] = "projects_or_skills_or_contexts"
    items = [_exp(1, skill="a"), _exp(2, skill="a"), _exp(3, skill="a")]
    items[0]["conflict_refs"] = ["conflict"]
    candidates, _ = pattern_discovery.build_pattern_candidates(
        experiences=[{"id": "missing"}] + items,
        embeddings={item["id"]: [1.0, 0.0] for item in items},
        policy=policy,
    )
    assert candidates[0]["contradiction_count"] == 1
    assert candidates[0]["quality_ready"] is False

    disconnected = [_exp(i) for i in range(1, 4)]
    candidates, _ = pattern_discovery.build_pattern_candidates(
        experiences=disconnected,
        embeddings={"e1": [1.0, 0.0], "e2": [0.0, 1.0], "e3": [-1.0, 0.0]},
        policy=load_pattern_policy(),
    )
    assert candidates == []

    policy = load_pattern_policy()
    policy["clustering"]["pair_similarity_threshold"] = 0.2
    policy["clustering"]["centroid_similarity_threshold"] = 0.9
    candidates, _ = pattern_discovery.build_pattern_candidates(
        experiences=disconnected,
        embeddings={"e1": [1.0, 0.0], "e2": [0.8, 0.6], "e3": [0.8, -0.6]},
        policy=policy,
    )
    assert candidates == []


def test_run_pattern_discovery_blocked_cache_and_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = load_pattern_policy()
    policy["embedding"]["backend"] = "invalid"
    finished: list[dict[str, Any]] = []
    monkeypatch.setattr(pattern_discovery.pattern_store, "start_discovery_run", lambda **kwargs: "run")
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "finish_discovery_run",
        lambda run_id, **kwargs: finished.append(kwargs) or True,
    )
    blocked = pattern_discovery.run_pattern_discovery(policy=policy)
    assert blocked["status"] == "blocked"
    assert finished[-1]["status"] == "blocked"

    experiences = [_exp(i) for i in range(1, 4)]
    policy = load_pattern_policy()
    policy["embedding"]["dimension"] = 2
    monkeypatch.setattr(pattern_discovery.pattern_store, "fetch_eligible_experiences", lambda **kwargs: experiences)
    monkeypatch.setattr(
        pattern_discovery.pattern_store,
        "get_cached_experience_embeddings",
        lambda **kwargs: {
            "e1": {
                "source_hash": pattern_discovery._source_hash(pattern_discovery._experience_text(experiences[0])),
                "embedding": json.dumps([1.0, 0.0]),
            }
        },
    )
    result = pattern_discovery.run_pattern_discovery(
        policy=policy,
        write_candidates=False,
        encode_texts=lambda texts, **kwargs: [[1.0, 0.0] for _ in texts],
    )
    assert result["status"] == "success"
    assert result["embeddings_reused"] == 1
    assert result["embeddings_generated"] == 2

    bad_count = pattern_discovery.run_pattern_discovery(
        policy=policy,
        write_candidates=False,
        encode_texts=lambda texts, **kwargs: [],
    )
    assert bad_count["status"] == "failure"
    assert "unexpected number" in bad_count["error"]

    wrong_dimension = pattern_discovery.run_pattern_discovery(
        policy=policy,
        write_candidates=False,
        encode_texts=lambda texts, **kwargs: [[1.0] for _ in texts],
    )
    assert wrong_dimension["status"] == "failure"
    assert "dimension must be" in wrong_dimension["error"]

    finished.clear()
    failed_with_run = pattern_discovery.run_pattern_discovery(
        policy=policy,
        write_candidates=True,
        encode_texts=lambda texts, **kwargs: [],
    )
    assert failed_with_run["status"] == "failure"
    assert finished[-1]["status"] == "failure"


def test_pattern_store_query_helpers_and_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = CursorScript(
        all_rows=[
            [{"id": "e1"}],
            [{"experience_id": "e1", "source_hash": "h", "embedding": [1]}],
        ]
    )
    _wire_cursor(monkeypatch, pattern_store, cursor)
    rows = pattern_store.fetch_eligible_experiences(
        included_categories=["implementation_recipe"],
        excluded_categories=[],
        min_confidence=0.5,
        exclude_synthetic=True,
        project_id="p1",
        limit=99999,
    )
    assert rows == [{"id": "e1"}]
    cached = pattern_store.get_cached_experience_embeddings(experience_ids=["e1"], model_name="m")
    assert cached["e1"]["source_hash"] == "h"
    assert pattern_store.get_cached_experience_embeddings(experience_ids=[], model_name="m") == {}
    assert pattern_store.upsert_experience_embeddings(rows=[], model_name="m") == 0
    assert pattern_store.upsert_experience_embeddings(
        rows=[{"experience_id": "e1", "source_hash": "h", "embedding": [1.0]}],
        model_name="m",
    ) == 1
    assert cursor.executemany_calls
    assert pattern_store.expire_unseen_candidates(ttl_days=0) == 3


def test_pattern_store_activation_rejections_and_empty_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pattern_store.record_pattern_usage_events(
        pattern_ids=[],
        project_id=None,
        session_id=None,
        log_id=None,
        outcome="success",
        user_feedback=None,
    )["status"] == "not_applicable"

    cursor = CursorScript(one=[None])
    _wire_cursor(monkeypatch, pattern_store, cursor)
    with pytest.raises(ValueError, match="candidate not found"):
        pattern_store.materialize_candidate_to_draft(candidate_id="missing", reviewed_by="qa")

    for row, message in [
        (None, "not found"),
        ({"status": "active"}, None),
        ({"status": "deprecated"}, "Only draft"),
        (
            {
                "status": "draft",
                "support_count": 0,
                "context_diversity": 0,
                "cohesion_score": 0.0,
                "avg_score": 0.0,
                "contradiction_count": 1,
            },
            "support_count",
        ),
    ]:
        cursor = CursorScript(one=[row])
        _wire_cursor(monkeypatch, pattern_store, cursor)
        if message is None:
            assert pattern_store.activate_pattern(
                pattern_id="p1", reviewed_by="qa", quality_gates={}
            )["status"] == "already_active"
        else:
            with pytest.raises(ValueError, match=message):
                pattern_store.activate_pattern(pattern_id="p1", reviewed_by="qa", quality_gates={})


def test_pattern_store_health_backlog_review_degradation(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = CursorScript(one=[{"table_name": None}])
    _wire_cursor(monkeypatch, pattern_store, cursor)
    assert pattern_store.get_pattern_health()["available"] is False

    cursor = CursorScript(
        one=[
            {"table_name": "core_brain.pattern_candidates"},
            {"total": 1},
            {"total": 2},
            {"id": "run"},
        ],
        all_rows=[[{"id": "candidate"}]],
    )
    _wire_cursor(monkeypatch, pattern_store, cursor)
    health = pattern_store.get_pattern_health(candidate_limit=500)
    assert health["available"] is True
    assert health["latest_discovery_run"]["id"] == "run"

    cursor = CursorScript(one=[{"table_name": None}])
    _wire_cursor(monkeypatch, pattern_store, cursor)
    assert pattern_store.get_activation_backlog()["reason"] == "pattern_intelligence_schema_not_installed"

    monkeypatch.setattr(pattern_store, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert pattern_store.get_pattern_health()["reason"] == "db down"
    assert pattern_store.get_activation_backlog()["reason"] == "db down"
    assert pattern_store.list_review_ready_candidates() == []

    summary = pattern_store._candidate_review_summary(
        {
            "metadata": {"quality_gates": {"good": True, "bad": False}},
            "contradiction_count": 1,
            "avg_confidence": 0.2,
            "cohesion_score": 0.2,
            "scope_type": "skill",
            "distinct_project_count": 1,
        }
    )
    assert summary["failed_gates"] == ["bad"]
    assert set(summary["risks"]) == {
        "open_contradictions",
        "low_avg_confidence",
        "low_cohesion",
        "single_project_evidence",
    }


def test_session_playbook_helper_edge_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert session_playbooks._normalize_json_field(None, []) == []
    assert session_playbooks._normalize_json_field("bad", []) == []
    assert session_playbooks._normalize_json_field(3, []) == 3
    assert session_playbooks._validate_embedding(None) is None
    assert session_playbooks._validate_embedding(["bad"]) is None
    assert session_playbooks._validate_embedding([1.0]) is None
    assert session_playbooks._validate_embedding([float("inf")] * session_playbooks.EMBEDDING_DIM) is None
    assert session_playbooks._safe_embed("") is None
    monkeypatch.setattr(session_playbooks, "embed", lambda text: (_ for _ in ()).throw(RuntimeError("no model")))
    assert session_playbooks._safe_embed("x") is None
    assert session_playbooks._normalize_pattern_ids([None, "", "a", "a"]) == ["a"]
    assert session_playbooks._apply_requested_pattern_alignment([{"id": "x"}], None) == [{"id": "x"}]
    assert session_playbooks._keyword_overlap_score("", "candidate") == 0.0

    empty = session_playbooks._playbook_compactness_metrics({})
    medium = session_playbooks._playbook_compactness_metrics(
        {
            "output_summary": "x" * 30,
            "validation_summary": "x" * 10,
            "commands": ["x"] * 4,
            "files_touched": ["x"] * 5,
        }
    )
    assert empty["execution_fit"] == 0.18
    assert medium["execution_fit"] == 0.72
    bloated = session_playbooks._playbook_compactness_metrics(
        {
            "output_summary": "x" * 700,
            "validation_summary": "x" * 400,
            "commands": ["x"] * 6,
            "files_touched": ["x"] * 7,
        }
    )
    assert bloated["execution_fit"] == 0.38
    assert bloated["bloat_penalty"] > 0.0


def test_session_playbook_scoring_and_cross_project_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = [1.0] * session_playbooks.EMBEDDING_DIM
    monkeypatch.setattr(session_playbooks, "_safe_embed", lambda text: vector)
    monkeypatch.setattr(session_playbooks, "cosine_similarity", lambda left, right: 0.9)
    rows = [
        {
            "id": "keep",
            "title": "matching playbook",
            "task_description": "matching task",
            "embedding": vector,
            "confidence_score": 0.8,
        },
        {
            "id": "drop",
            "title": "other",
            "task_description": "other",
            "embedding": None,
            "confidence_score": 0.8,
        },
    ]
    scored = session_playbooks._score_playbooks("matching task", rows, min_similarity=0.5)
    assert [item["id"] for item in scored] == ["keep"]

    cursor = CursorScript(all_rows=[[], [rows[0]]])
    _wire_cursor(monkeypatch, session_playbooks, cursor)
    monkeypatch.setattr(session_playbooks, "apply_memory_freshness", lambda items, **kwargs: items)
    monkeypatch.setattr(
        session_playbooks,
        "apply_memory_governor",
        lambda items, **kwargs: (items, {"warnings": []}),
    )
    monkeypatch.setattr(session_playbooks, "summarize_freshness_warnings", lambda items, **kwargs: ["freshness"])
    result = session_playbooks.retrieve_session_playbooks(
        "matching task",
        project_id="p1",
        allow_cross_project=True,
        min_similarity=0.1,
    )
    assert result["search_scope"] == "cross_project_fallback"
    assert "Session playbooks required cross-project fallback." in result["warnings"]
    assert "freshness" in result["warnings"]
