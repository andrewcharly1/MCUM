from __future__ import annotations

import json

from MCUM.db import experience_store


def _vector(value: float) -> list[float]:
    return [value] * experience_store.EMBEDDING_DIM


class _CursorStub:
    def __init__(
        self,
        rows: list[dict] | None = None,
        *,
        fetchone_results: list[dict] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._rows = rows or []
        self._fetchone_results = list(fetchone_results or [])
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def fetchone(self) -> dict:
        if not self._fetchone_results:
            return {}
        return dict(self._fetchone_results.pop(0))


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


def test_build_experience_filters_aliases_columns_consistently() -> None:
    conditions, params = experience_store._build_experience_filters(
        min_confidence=0.45,
        category="testing_strategy",
        skill_name="mcum-orchestrator",
        project_id="project-1",
        alias="e",
        require_embedding=True,
    )

    assert conditions == [
        "e.current_confidence >= %s",
        "e.superseded_by IS NULL",
        "e.embedding IS NOT NULL",
        "e.category = %s",
        "e.skill_name = %s",
        "e.project_id = %s",
    ]
    assert params == [0.45, "testing_strategy", "mcum-orchestrator", "project-1"]


def test_semantic_search_pgvector_uses_aliased_where_clause(monkeypatch) -> None:
    rows = [
        {
            "id": "exp-1",
            "category": "testing_strategy",
            "title": "Use targeted smoke tests",
            "content": json.dumps({"conclusion": "Run smoke tests on the wrapper."}),
            "applicability": json.dumps({"when": "Validating wrapper flows"}),
            "not_applicable_cases": json.dumps({"when_not": "No CLI wrapper involved"}),
            "conditions": json.dumps({"requires_pgvector": True}),
            "current_confidence": 0.9,
            "revalidation_count": 2,
            "unique_context_count": 1,
            "tested_by": "agent",
            "skill_name": "mcum-orchestrator",
            "skill_version": "1.1.0",
            "project_id": "project-1",
            "task_description": "Validate semantic retrieval",
            "conflict_refs": [],
            "similarity": 0.8,
            "created_at": None,
            "last_validated_at": None,
        }
    ]
    cursor = _CursorStub(rows)

    monkeypatch.setattr(experience_store, "_is_pgvector_enabled", lambda force_refresh=False: True)
    monkeypatch.setattr(experience_store, "_embed_query_cached", lambda _: _vector(0.2))
    monkeypatch.setattr(
        experience_store,
        "apply_memory_freshness",
        lambda items, **kwargs: items,
    )
    monkeypatch.setattr(
        experience_store,
        "_load_retrieval_policy",
        lambda policy=None: {
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "semantic_weight": 0.7,
            "confidence_weight": 0.3,
        },
    )
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    results = experience_store.semantic_search(
        query_text="wrapper tests",
        category="testing_strategy",
        skill_name="mcum-orchestrator",
        project_id="project-1",
        min_confidence=0.4,
        min_similarity=0.5,
        limit=5,
    )

    query, params = cursor.executed[-1]
    expected_where = (
        "WHERE e.current_confidence >= %s AND e.superseded_by IS NULL "
        "AND e.embedding IS NOT NULL AND e.category = %s "
        "AND e.skill_name = %s AND e.project_id = %s"
    )

    assert expected_where in query
    assert params == [
        experience_store._embedding_to_vector_literal(_vector(0.2)),
        0.4,
        "testing_strategy",
        "mcum-orchestrator",
        "project-1",
        0.5,
        5,
    ]
    assert results[0]["content"] == {"conclusion": "Run smoke tests on the wrapper."}
    assert results[0]["_similarity"] == 0.8
    assert results[0]["_combined_score"] == 0.83


def test_semantic_search_json_fallback_skips_invalid_embeddings(monkeypatch) -> None:
    rows = [
        {
            "id": "exp-strong",
            "category": "implementation_recipe",
            "title": "Strong match",
            "content": json.dumps({"conclusion": "Keep the strongest candidate."}),
            "applicability": json.dumps({"when": "JSON fallback is active"}),
            "not_applicable_cases": json.dumps({"when_not": "pgvector is enabled"}),
            "conditions": json.dumps({}),
            "current_confidence": 0.5,
            "revalidation_count": 1,
            "unique_context_count": 1,
            "tested_by": "agent",
            "skill_name": "mcum-orchestrator",
            "skill_version": "1.1.0",
            "project_id": "project-1",
            "task_description": "Fallback retrieval",
            "conflict_refs": [],
            "embedding": json.dumps(_vector(0.9)),
            "created_at": None,
            "last_validated_at": None,
        },
        {
            "id": "exp-invalid",
            "category": "implementation_recipe",
            "title": "Malformed embedding",
            "content": json.dumps({"conclusion": "This row should be skipped."}),
            "applicability": json.dumps({"when": "Never"}),
            "not_applicable_cases": json.dumps({"when_not": "Always"}),
            "conditions": json.dumps({}),
            "current_confidence": 0.95,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "tested_by": "agent",
            "skill_name": "mcum-orchestrator",
            "skill_version": "1.1.0",
            "project_id": "project-1",
            "task_description": "Fallback retrieval",
            "conflict_refs": [],
            "embedding": json.dumps([1, 2, 3]),
            "created_at": None,
            "last_validated_at": None,
        },
        {
            "id": "exp-secondary",
            "category": "implementation_recipe",
            "title": "Secondary match",
            "content": json.dumps({"conclusion": "Keep valid lower-ranked candidates too."}),
            "applicability": json.dumps({"when": "Fallback path"}),
            "not_applicable_cases": json.dumps({"when_not": "Not a fallback"}),
            "conditions": json.dumps({}),
            "current_confidence": 0.6,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "tested_by": "agent",
            "skill_name": "mcum-orchestrator",
            "skill_version": "1.1.0",
            "project_id": "project-1",
            "task_description": "Fallback retrieval",
            "conflict_refs": [],
            "embedding": json.dumps(_vector(0.8)),
            "created_at": None,
            "last_validated_at": None,
        },
        {
            "id": "exp-low-sim",
            "category": "implementation_recipe",
            "title": "Below threshold",
            "content": json.dumps({"conclusion": "This row should be filtered by similarity."}),
            "applicability": json.dumps({"when": "Never"}),
            "not_applicable_cases": json.dumps({"when_not": "Always"}),
            "conditions": json.dumps({}),
            "current_confidence": 0.99,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "tested_by": "agent",
            "skill_name": "mcum-orchestrator",
            "skill_version": "1.1.0",
            "project_id": "project-1",
            "task_description": "Fallback retrieval",
            "conflict_refs": [],
            "embedding": json.dumps(_vector(0.2)),
            "created_at": None,
            "last_validated_at": None,
        },
    ]
    cursor = _CursorStub(rows)

    monkeypatch.setattr(experience_store, "_is_pgvector_enabled", lambda force_refresh=False: False)
    monkeypatch.setattr(experience_store, "_embed_query_cached", lambda _: _vector(0.5))
    monkeypatch.setattr(
        experience_store,
        "apply_memory_freshness",
        lambda items, **kwargs: items,
    )
    monkeypatch.setattr(
        experience_store,
        "_load_retrieval_policy",
        lambda policy=None: {
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "semantic_weight": 0.6,
            "confidence_weight": 0.4,
        },
    )
    monkeypatch.setattr(experience_store, "cosine_similarity", lambda query, candidate: candidate[0])
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    results = experience_store.semantic_search(
        query_text="fallback",
        category="implementation_recipe",
        skill_name="mcum-orchestrator",
        project_id="project-1",
        min_confidence=0.3,
        min_similarity=0.5,
        limit=10,
    )

    assert [item["id"] for item in results] == ["exp-strong", "exp-secondary"]
    assert results[0]["_combined_score"] == 0.74
    assert results[1]["_combined_score"] == 0.72


def test_retrieve_for_task_enforces_token_budget(monkeypatch) -> None:
    relevant = [
        {"id": "exp-1", "title": "First", "content": {"conclusion": "first"}, "conflict_refs": []},
        {"id": "exp-2", "title": "Second", "content": {"conclusion": "second"}, "conflict_refs": []},
    ]
    failures = [
        {"id": "fp-1", "title": "Failure", "content": {"conclusion": "failure"}, "conflict_refs": []},
    ]

    monkeypatch.setattr(experience_store, "semantic_search", lambda *args, **kwargs: list(relevant))
    monkeypatch.setattr(experience_store, "search_by_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(experience_store, "get_failure_patterns", lambda *args, **kwargs: list(failures))
    monkeypatch.setattr(experience_store, "get_active_patterns", lambda *args, **kwargs: [])
    token_map = {"exp-1": 40, "exp-2": 40, "fp-1": 10}
    monkeypatch.setattr(
        experience_store,
        "_estimate_retrieval_item_tokens",
        lambda item: token_map[item["id"]],
    )

    result = experience_store.retrieve_for_task(
        "validate wrapper",
        skill_context="mcum-orchestrator",
        project_id="project-1",
        policy={
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "top_relevant_slots": 2,
            "conflict_slot": 0,
            "failure_pattern_slot": 1,
            "max_token_budget": 55,
        },
    )

    assert [item["id"] for item in result["experiences"]] == ["exp-1"]
    assert [item["id"] for item in result["failure_patterns"]] == ["fp-1"]
    assert result["tokens_used_estimate"] == 50
    assert any("Context budget truncated" in warning for warning in result["warnings"])


def test_retrieve_for_task_can_supplement_same_project_with_cross_project_learning(monkeypatch) -> None:
    same_project = [
        {"id": "exp-local", "title": "Local", "content": {"conclusion": "local"}, "conflict_refs": [], "project_id": "project-1"},
    ]
    cross_project = [
        {"id": "exp-cross", "title": "Cross", "content": {"conclusion": "cross"}, "conflict_refs": [], "project_id": "project-2"},
    ]
    same_failures = [
        {"id": "fp-local", "title": "Local risk", "content": {"conclusion": "risk"}, "conflict_refs": [], "project_id": "project-1"},
    ]
    cross_failures = [
        {"id": "fp-cross", "title": "Cross risk", "content": {"conclusion": "cross risk"}, "conflict_refs": [], "project_id": "project-2"},
    ]

    def fake_semantic_search(*args, **kwargs):
        if kwargs.get("project_id") == "project-1":
            return list(same_project)
        if kwargs.get("project_id") is None:
            return list(cross_project)
        return []

    def fake_failure_patterns(*args, **kwargs):
        if kwargs.get("project_id") == "project-1":
            return list(same_failures)
        if kwargs.get("project_id") is None:
            return list(cross_failures)
        return []

    monkeypatch.setattr(experience_store, "semantic_search", fake_semantic_search)
    monkeypatch.setattr(experience_store, "search_by_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(experience_store, "get_failure_patterns", fake_failure_patterns)
    monkeypatch.setattr(experience_store, "_estimate_retrieval_item_tokens", lambda item: 20)

    result = experience_store.retrieve_for_task(
        "mejorar wrapper",
        skill_context="mcum-orchestrator",
        project_id="project-1",
        policy={
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "top_relevant_slots": 2,
            "conflict_slot": 0,
            "failure_pattern_slot": 2,
            "max_cross_project_memories": 1,
            "cross_project_fallback_only_if_no_project_hits": True,
            "max_token_budget": 200,
        },
        scope_learning_profile={
            "active": True,
            "scope": "blended",
            "sample_count": 4,
            "eager_cross_project": True,
            "prefer_same_project_only": False,
            "recommended_cross_project_memories": 1,
            "score_delta": 0.22,
        },
    )

    assert [item["id"] for item in result["experiences"]] == ["exp-local", "exp-cross"]
    assert [item["id"] for item in result["failure_patterns"]] == ["fp-local", "fp-cross"]
    assert result["project_scope"] == "cross_project_fallback"
    assert result["scope_learning_profile"]["eager_cross_project"] is True
    assert any("supplemented same-project memory" in warning for warning in result["warnings"])


def test_retrieve_for_task_applies_memory_governor_soft_filter(monkeypatch) -> None:
    relevant = [
        {
            "id": "exp-good",
            "title": "Validated wrapper fix",
            "content": {"conclusion": "Apply the validated wrapper fix."},
            "conflict_refs": [],
            "project_id": "project-1",
            "current_confidence": 0.92,
            "revalidation_count": 2,
            "unique_context_count": 2,
            "source_artifacts": [{"path": "workspace_session.py"}],
            "_combined_score": 0.88,
        },
        {
            "id": "exp-noisy",
            "title": "Verbose weak note",
            "content": {"note": ("noise " * 320).strip()},
            "conflict_refs": [],
            "project_id": "project-2",
            "current_confidence": 0.24,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "_combined_score": 0.65,
        },
    ]

    monkeypatch.setattr(experience_store, "semantic_search", lambda *args, **kwargs: list(relevant))
    monkeypatch.setattr(experience_store, "search_by_keywords", lambda *args, **kwargs: [])
    monkeypatch.setattr(experience_store, "get_failure_patterns", lambda *args, **kwargs: [])
    monkeypatch.setattr(experience_store, "get_active_patterns", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        experience_store,
        "get_recent_feedback_signals",
        lambda *args, **kwargs: {
            "signals": [],
            "positive_experience_ids": [],
            "negative_experience_ids": [],
            "positive_pattern_ids": [],
            "negative_pattern_ids": [],
            "summary": {"signals_n": 0},
        },
    )
    monkeypatch.setattr(experience_store, "_estimate_retrieval_item_tokens", lambda item: 20)

    result = experience_store.retrieve_for_task(
        "repair wrapper safely",
        skill_context="mcum-orchestrator",
        project_id="project-1",
        policy={
            **experience_store.DEFAULT_RETRIEVAL_POLICY,
            "top_relevant_slots": 2,
            "failure_pattern_slot": 0,
            "conflict_slot": 0,
            "pattern_slot": 0,
            "max_token_budget": 120,
            "memory_governor": {
                "enabled": True,
                "mode": "soft_filter",
                "preserve_at_least": 1,
            },
        },
    )

    assert [item["id"] for item in result["experiences"]] == ["exp-good"]
    assert result["memory_governance"]["sections"]["experiences"]["filtered_count"] == 1
    assert any("Memory governor filtered 1 experience" in warning for warning in result["warnings"])


def test_find_duplicate_experience_groups_returns_canonical_first(monkeypatch) -> None:
    cursor = _CursorStub(
        [
            {
                "project_id": "project-1",
                "category": "testing_strategy",
                "skill_name": "mcum-orchestrator",
                "normalized_title": "validated wrapper fix",
                "normalized_task_description": "repair wrapper",
                "normalized_conclusion": "Apply the validated wrapper fix.",
                "group_size": 3,
                "ids": ["canon-1", "dup-1", "dup-2"],
            }
        ]
    )
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    groups = experience_store.find_duplicate_experience_groups(project_id="project-1")

    assert len(groups) == 1
    assert groups[0]["canonical_id"] == "canon-1"
    assert groups[0]["duplicate_ids"] == ["dup-1", "dup-2"]
    assert groups[0]["group_size"] == 3


def test_consolidate_duplicate_experiences_marks_superseded(monkeypatch) -> None:
    cursor = _CursorStub(rowcount=2)
    monkeypatch.setattr(
        experience_store,
        "find_duplicate_experience_groups",
        lambda **kwargs: [
            {
                "project_id": "project-1",
                "category": "testing_strategy",
                "skill_name": "mcum-orchestrator",
                "normalized_title": "validated wrapper fix",
                "group_size": 3,
                "canonical_id": "canon-1",
                "duplicate_ids": ["dup-1", "dup-2"],
            }
        ],
    )
    monkeypatch.setattr(experience_store, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(experience_store, "get_cursor", lambda conn: _CursorManager(cursor))

    result = experience_store.consolidate_duplicate_experiences(project_id="project-1")

    assert result["groups_merged"] == 1
    assert result["experiences_superseded"] == 2
    assert result["samples"][0]["canonical_id"] == "canon-1"
    query, params = cursor.executed[-1]
    assert "SET superseded_by = %s" in query
    assert params[0] == "canon-1"
    assert params[-1] == ["dup-1", "dup-2"]
