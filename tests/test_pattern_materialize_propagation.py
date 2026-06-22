"""
Fase 1 - Tests de propagacion de columnas en materialize_candidate_to_draft.

Verifican que aceptar un candidato review-ready crea una fila en
core_brain.patterns con todas las columnas que activate_pattern necesita
para evaluar quality gates (no NULLs).

Cubre el bug historico donde support_count, context_diversity,
contradiction_count, avg_score quedaban NULL en la fila del pattern
y activate_pattern los leiia como 0 disparando ValueError al gatear.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from MCUM.db import pattern_store
from MCUM.db.connection import get_cursor, get_db
from MCUM.policy import load_pattern_policy


def _pick_real_experience_id() -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT id FROM core_brain.experiences "
                "WHERE category::text IN "
                "('architecture_pattern','implementation_recipe','testing_strategy',"
                "'prompting_heuristic','failure_pattern','evaluation_policy',"
                "'stack_decision') LIMIT 1"
            )
            row = cur.fetchone()
            assert row is not None, "No hay experiences elegibles para seed_experience_id"
            return str(row["id"])


def _insert_test_candidate(
    *,
    quality_ready: bool = True,
    support_count: int = 5,
    context_diversity: int = 5,
    distinct_project_count: int = 2,
    cohesion_score: float = 0.92,
    contradiction_count: int = 0,
    avg_confidence: float = 0.88,
    quality_score: float = 0.91,
    skill_name: str = "qa-mcum-phase1",
    category: str = "architecture_pattern",
    status: str = "review",
    seed_experience_id: str | None = None,
) -> str:
    if seed_experience_id is None:
        seed_experience_id = _pick_real_experience_id()
    candidate_id = str(uuid.uuid4())
    candidate_key = f"test:phase1:{candidate_id}"
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.pattern_candidates (
                    id, candidate_key, category, skill_name, scope_type,
                    scope_project_id, label, summary, status, support_count,
                    distinct_project_count, context_diversity, cohesion_score,
                    contradiction_count, avg_confidence, quality_score,
                    quality_ready, seed_experience_id, discovery_run_id,
                    embedding_model, algorithm_version, metadata
                ) VALUES (
                    %s, %s, %s, %s, 'skill',
                    NULL, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, NULL,
                    'all-MiniLM-L6-v2', 'semantic-components-v2', '{}'::jsonb
                )
                """,
                (
                    candidate_id,
                    candidate_key,
                    category,
                    skill_name,
                    f"qa label {candidate_id[:8]}",
                    f"qa summary {candidate_id[:8]}",
                    status,
                    support_count,
                    distinct_project_count,
                    context_diversity,
                    cohesion_score,
                    contradiction_count,
                    avg_confidence,
                    quality_score,
                    quality_ready,
                    seed_experience_id,
                ),
            )
    return candidate_id


def _cleanup(candidate_id: str, pattern_id: str | None = None) -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if pattern_id:
                cur.execute(
                    "DELETE FROM core_brain.pattern_evidence WHERE pattern_id = %s",
                    (pattern_id,),
                )
                cur.execute(
                    "DELETE FROM core_brain.pattern_embeddings WHERE pattern_id = %s",
                    (pattern_id,),
                )
                cur.execute(
                    "DELETE FROM core_brain.patterns WHERE id = %s", (pattern_id,)
                )
            cur.execute(
                "DELETE FROM core_brain.pattern_candidate_evidence WHERE candidate_id = %s",
                (candidate_id,),
            )
            cur.execute(
                "DELETE FROM core_brain.pattern_embeddings WHERE candidate_id = %s",
                (candidate_id,),
            )
            cur.execute(
                "DELETE FROM core_brain.pattern_candidates WHERE id = %s",
                (candidate_id,),
            )


def _fetch_pattern(pattern_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT * FROM core_brain.patterns WHERE id = %s", (pattern_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def test_materialize_propagates_quality_columns() -> None:
    """Aceptar un candidato review-ready crea un pattern con todas las
    columnas que activate_pattern necesita, no NULLs."""
    candidate_id = _insert_test_candidate(quality_ready=True)
    pattern_id = None
    try:
        result = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        assert result["status"] == "draft_materialized"
        pattern_id = result["pattern_id"]

        pat = _fetch_pattern(pattern_id)
        assert pat is not None
        assert pat["status"] == "draft"
        assert pat["experience_count"] == 5
        assert pat["support_count"] == 5
        assert pat["context_diversity"] == 5
        assert pat["cohesion_score"] == pytest.approx(0.92, rel=1e-3)
        assert pat["contradiction_count"] == 0
        assert pat["avg_score"] == pytest.approx(0.88, rel=1e-3)
        assert pat["scope_skill_name"] == "qa-mcum-phase1"
        assert pat["category"] == "architecture_pattern"
        assert pat["promotion_criteria_met"] is False
        assert pat["health_state"] == "observing"
    finally:
        _cleanup(candidate_id, pattern_id)


def test_activate_pattern_succeeds_after_materialize() -> None:
    """End-to-end: candidate review-ready -> draft -> active, sin reabrir
    quality gates. Esto es exactamente lo que estaba bloqueado antes."""
    candidate_id = _insert_test_candidate(quality_ready=True)
    pattern_id = None
    try:
        draft = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        pattern_id = draft["pattern_id"]

        activated = pattern_store.activate_pattern(
            pattern_id=pattern_id,
            reviewed_by="qa-phase1",
            quality_gates=load_pattern_policy()["quality_gates"],
        )
        assert activated["status"] == "active"

        pat = _fetch_pattern(pattern_id)
        assert pat["status"] == "active"
        assert pat["promotion_criteria_met"] is True
        assert pat["health_state"] == "observing"
    finally:
        _cleanup(candidate_id, pattern_id)


def test_activate_pattern_fails_when_support_below_gate() -> None:
    """Un pattern con support_count=2 no debe activarse (gate explicito)."""
    candidate_id = _insert_test_candidate(
        quality_ready=True, support_count=2, quality_score=0.85
    )
    pattern_id = None
    try:
        draft = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        pattern_id = draft["pattern_id"]

        with pytest.raises(ValueError, match="support_count"):
            pattern_store.activate_pattern(
                pattern_id=pattern_id,
                reviewed_by="qa-phase1",
                quality_gates=load_pattern_policy()["quality_gates"],
            )
    finally:
        _cleanup(candidate_id, pattern_id)


def test_materialize_rejects_non_quality_ready_candidate() -> None:
    """Candidatos shadow (quality_ready=False) deben seguir siendo
    rechazados por materialize_candidate_to_draft - sin cambio de politica."""
    candidate_id = _insert_test_candidate(quality_ready=False, status="shadow")
    try:
        with pytest.raises(ValueError, match="quality gates"):
            pattern_store.materialize_candidate_to_draft(
                candidate_id=candidate_id, reviewed_by="qa-phase1"
            )
    finally:
        _cleanup(candidate_id)


def test_materialize_rejects_quality_ready_candidate_outside_review_status() -> None:
    candidate_id = _insert_test_candidate(quality_ready=True, status="shadow")
    try:
        with pytest.raises(ValueError, match="review-ready"):
            pattern_store.materialize_candidate_to_draft(
                candidate_id=candidate_id, reviewed_by="qa-phase1"
            )
    finally:
        _cleanup(candidate_id)


def test_materialize_handles_null_candidate_fields_safely() -> None:
    """Si el candidate tiene campos numericos en cero (NOT NULL en DB),
    materialize debe propagar 0/0.0 sin reventar (no revierte la transaccion)."""
    candidate_id = _insert_test_candidate(
        quality_ready=True,
        support_count=0,
        context_diversity=0,
        distinct_project_count=1,
        cohesion_score=0.0,
        contradiction_count=0,
        avg_confidence=0.0,
        quality_score=0.0,
    )
    pattern_id = None
    try:
        result = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        pattern_id = result["pattern_id"]
        pat = _fetch_pattern(pattern_id)
        assert pat["support_count"] == 0
        assert pat["context_diversity"] == 0
        assert pat["cohesion_score"] == 0.0
        assert pat["contradiction_count"] == 0
        assert pat["avg_score"] == 0.0
    finally:
        _cleanup(candidate_id, pattern_id)


def test_idempotent_re_materialize_returns_existing_pattern() -> None:
    """materialize_candidate_to_draft debe ser idempotente: si ya fue
    materializado, devuelve el mismo pattern_id sin crear uno nuevo."""
    candidate_id = _insert_test_candidate()
    pattern_id = None
    try:
        first = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        pattern_id = first["pattern_id"]
        second = pattern_store.materialize_candidate_to_draft(
            candidate_id=candidate_id, reviewed_by="qa-phase1"
        )
        assert second["status"] == "already_materialized"
        assert second["pattern_id"] == pattern_id
    finally:
        _cleanup(candidate_id, pattern_id)
