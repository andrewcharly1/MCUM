"""Semantic helpers for governed concept retrieval."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from . import pgvector_util
from .connection import get_cursor, get_db
from .embedder import MODEL_NAME, cosine_similarity, embed, embed_batch


def _concept_embedding_is_vector() -> bool:
    return pgvector_util.column_is_vector("knowledge_library", "concept_embeddings")


ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = ROOT / "core" / "knowledge_library_taxonomy.py"


def _load_taxonomy_module():
    spec = importlib.util.spec_from_file_location(
        "mcum_live_knowledge_library_taxonomy_semantic",
        TAXONOMY_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load taxonomy helpers from {TAXONOMY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_taxonomy_module = _load_taxonomy_module()
iter_concept_definitions = _taxonomy_module.iter_concept_definitions
normalize_text = _taxonomy_module.normalize_text


def ensure_semantic_tables() -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT to_regclass('knowledge_library.concept_embeddings') AS regclass_name")
            row = cur.fetchone() or {}
            if not row.get("regclass_name"):
                cur.execute(
                    """
                    CREATE TABLE knowledge_library.concept_embeddings (
                        concept_id UUID PRIMARY KEY REFERENCES knowledge_library.concepts(id) ON DELETE CASCADE,
                        model_name TEXT NOT NULL,
                        text_repr TEXT NOT NULL,
                        embedding JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kl_concept_embeddings_model
                    ON knowledge_library.concept_embeddings (model_name, updated_at DESC)
                """
            )


def _concept_methodology_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for definition in iter_concept_definitions():
        mapping[str(definition["slug"])] = str(definition["methodology_slug"])
    return mapping


def _concept_text_repr(row: dict[str, Any]) -> str:
    aliases = row.get("aliases")
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except json.JSONDecodeError:
            aliases = [aliases]
    alias_items = [normalize_text(item) for item in (aliases or []) if normalize_text(item)]
    methodology_slug = str(row.get("methodology_slug") or "").strip()
    parts = [
        row.get("concept_name"),
        row.get("concept_slug"),
        methodology_slug,
        " ".join(alias_items),
        row.get("description"),
        row.get("notes"),
    ]
    return " | ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def sync_concept_embeddings(*, force: bool = False) -> dict[str, int]:
    ensure_semantic_tables()
    methodology_by_slug = _concept_methodology_map()

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    c.id::text AS concept_id,
                    c.concept_slug,
                    c.concept_name,
                    c.description,
                    c.aliases,
                    c.notes,
                    ce.model_name,
                    ce.text_repr
                FROM knowledge_library.concepts c
                LEFT JOIN knowledge_library.concept_embeddings ce
                  ON ce.concept_id = c.id
                ORDER BY c.concept_slug ASC
                """
            )
            rows = list(cur.fetchall())

            to_embed: list[dict[str, Any]] = []
            for row in rows:
                enriched = dict(row)
                enriched["methodology_slug"] = methodology_by_slug.get(str(row.get("concept_slug") or ""))
                text_repr = _concept_text_repr(enriched)
                current_model = str(row.get("model_name") or "")
                current_text = str(row.get("text_repr") or "")
                if force or current_model != MODEL_NAME or current_text != text_repr:
                    enriched["text_repr"] = text_repr
                    to_embed.append(enriched)

            if not to_embed:
                return {"concepts_seen": len(rows), "embedded": 0}

            embeddings = embed_batch([row["text_repr"] for row in to_embed])
            is_vector = _concept_embedding_is_vector()
            cast = "::vector" if is_vector else "::jsonb"
            for row, vector in zip(to_embed, embeddings):
                embedding_value = (
                    pgvector_util.to_vector_literal(vector)
                    if is_vector
                    else json.dumps(vector)
                )
                cur.execute(
                    f"""
                    INSERT INTO knowledge_library.concept_embeddings (
                        concept_id,
                        model_name,
                        text_repr,
                        embedding,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s{cast}, NOW())
                    ON CONFLICT (concept_id)
                    DO UPDATE SET
                        model_name = EXCLUDED.model_name,
                        text_repr = EXCLUDED.text_repr,
                        embedding = EXCLUDED.embedding,
                        updated_at = NOW()
                    """,
                    (
                        row["concept_id"],
                        MODEL_NAME,
                        row["text_repr"],
                        embedding_value,
                    ),
                )

    return {"concepts_seen": len(rows), "embedded": len(to_embed)}


def rank_concepts_semantically(
    query_text: str,
    *,
    methodology_slugs: list[str] | None = None,
    top_k: int = 6,
    min_score: float = 0.36,
) -> list[dict[str, Any]]:
    ensure_semantic_tables()
    methodology_filter = {str(item) for item in (methodology_slugs or []) if str(item).strip()}
    methodology_by_slug = _concept_methodology_map()

    query_embedding = embed(query_text)
    is_vector = _concept_embedding_is_vector()
    query_literal = pgvector_util.to_vector_literal(query_embedding) if is_vector else None

    if is_vector:
        # pgvector path: cosine similarity computed in SQL via the HNSW-backed
        # `<=>` operator; the raw vector is never pulled into Python.
        score_expr = "(1 - (ce.embedding <=> %s::vector)) AS sql_score"
        embedding_expr = "NULL::text AS embedding"
        select_params: tuple = (query_literal, MODEL_NAME)
    else:
        score_expr = "NULL::float AS sql_score"
        embedding_expr = "ce.embedding"
        select_params = (MODEL_NAME,)

    query_sql = f"""
        SELECT
            c.id::text AS concept_id,
            c.concept_slug,
            c.concept_name,
            c.concept_type::text AS concept_type,
            c.description,
            c.aliases,
            c.authority_tier::text AS authority_tier,
            ce.model_name,
            {embedding_expr},
            {score_expr},
            ce.text_repr
        FROM knowledge_library.concepts c
        JOIN knowledge_library.concept_embeddings ce
          ON ce.concept_id = c.id
        WHERE ce.model_name = %s
        ORDER BY c.concept_slug ASC
    """

    def _run_query() -> list[dict[str, Any]]:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(query_sql, select_params)
                return list(cur.fetchall())

    rows = _run_query()
    if not rows:
        sync_concept_embeddings()
        rows = _run_query()

    if not rows:
        return []

    ranked: list[dict[str, Any]] = []
    for row in rows:
        concept_slug = str(row.get("concept_slug") or "")
        methodology_slug = methodology_by_slug.get(concept_slug)
        if methodology_filter and methodology_slug not in methodology_filter:
            continue
        if is_vector:
            sql_score = row.get("sql_score")
            if sql_score is None:
                continue
            score = float(sql_score)
        else:
            raw_embedding = row.get("embedding")
            if isinstance(raw_embedding, str):
                raw_embedding = json.loads(raw_embedding)
            elif not isinstance(raw_embedding, list):
                raw_embedding = list(raw_embedding) if raw_embedding is not None else None
            if not isinstance(raw_embedding, list):
                continue
            score = cosine_similarity(query_embedding, raw_embedding)
        if score < min_score:
            continue
        ranked.append(
            {
                "concept_id": row.get("concept_id"),
                "concept_slug": concept_slug,
                "concept_name": row.get("concept_name"),
                "concept_type": row.get("concept_type"),
                "description": row.get("description"),
                "authority_tier": row.get("authority_tier"),
                "methodology_slug": methodology_slug,
                "semantic_score": round(float(score), 4),
            }
        )

    ranked.sort(key=lambda item: item["semantic_score"], reverse=True)
    return ranked[:top_k]
