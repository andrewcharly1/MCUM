from __future__ import annotations

import pytest

from MCUM.db import pgvector_util, session_playbooks


def test_to_vector_literal_formats_pgvector_input() -> None:
    literal = pgvector_util.to_vector_literal([0.5, -0.25, 0.0], dim=3)
    assert literal.startswith("[") and literal.endswith("]")
    assert literal == "[0.5,-0.25,0.0]"


def test_validate_embedding_rejects_wrong_dimension_and_non_finite() -> None:
    with pytest.raises(ValueError):
        pgvector_util.validate_embedding([0.1, 0.2], dim=3)
    with pytest.raises(ValueError):
        pgvector_util.validate_embedding([float("inf"), 0.0, 0.0], dim=3)


def test_to_sql_switches_between_vector_literal_and_json() -> None:
    vec = [0.1, 0.2, 0.3]
    assert pgvector_util.to_sql(vec, is_vector=True, dim=3) == "[0.1,0.2,0.3]"
    assert pgvector_util.to_sql(vec, is_vector=False, dim=3) == "[0.1, 0.2, 0.3]"


def test_caches_use_monotonic_clock_not_wall_clock(monkeypatch) -> None:
    # The cache TTL must not read the wall clock: tests (and callers) routinely
    # patch time.time, and the detection helpers run on the retrieval hot path.
    def _boom() -> float:
        raise AssertionError("pgvector_util must not call time.time()")

    monkeypatch.setattr(pgvector_util.time, "time", _boom)
    pgvector_util.reset_caches()
    monkeypatch.setattr(
        pgvector_util,
        "get_db",
        lambda: (_ for _ in ()).throw(RuntimeError("no db in unit test")),
    )
    # Detection swallows DB errors and returns False without touching time.time.
    assert pgvector_util.column_is_vector("core_brain", "session_playbooks") is False
    assert pgvector_util.pgvector_extension_available() is False


def test_score_playbooks_uses_sql_similarity_when_present(monkeypatch) -> None:
    # In pgvector mode the candidate similarity is computed in SQL and arrives as
    # _sql_similarity; the scorer must use it verbatim and not fall back to the
    # keyword/Python-cosine path.
    monkeypatch.setattr(
        session_playbooks,
        "_safe_embed",
        lambda text: (_ for _ in ()).throw(AssertionError("should not embed in vector mode")),
    )
    rows = [
        {
            "id": "pb-vec",
            "title": "Scraper jurisprudencia",
            "task_description": "crawler con diff",
            "output_summary": "Crawler estable con diff incremental y validacion.",
            "validation_summary": "Validado con conteo de paginas.",
            "commands": ["python scraper.py"],
            "files_touched": ["scraper.py"],
            "confidence_score": 0.8,
            "reuse_count": 2,
            "_sql_similarity": 0.83,
        }
    ]
    scored = session_playbooks._score_playbooks(
        "scraper que falla", rows, min_similarity=0.2, query_embedding=[0.0] * 384
    )
    assert len(scored) == 1
    assert scored[0]["_similarity"] == 0.83
