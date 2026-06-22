"""
Shared pgvector helpers for MCUM PostgreSQL-native embedding storage.

MCUM stores every embedding in PostgreSQL. When the `vector` extension is
installed, embedding columns are migrated from JSONB to `vector(384)` and
similarity is computed in SQL via the `<=>` (cosine distance) operator backed
by an HNSW index. When the extension is absent, the same columns stay JSONB
and callers fall back to Python-side cosine. These helpers centralize the
detection and formatting so every store behaves identically.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from .connection import get_cursor, get_db

EMBEDDING_DIM = 384
_TTL_SEC = 300.0

# Cache: pgvector extension presence (process-wide).
_EXT_CACHE: dict[str, Any] = {"value": None, "ts": 0.0}
# Cache: per-column "is this column a vector type" check.
_COL_CACHE: dict[tuple[str, str, str], tuple[bool, float]] = {}


def _scalar(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def pgvector_extension_available(force_refresh: bool = False) -> bool:
    """True if the PostgreSQL `vector` extension is installed (cached)."""
    now = time.monotonic()
    if (
        not force_refresh
        and _EXT_CACHE["value"] is not None
        and (now - _EXT_CACHE["ts"]) < _TTL_SEC
    ):
        return bool(_EXT_CACHE["value"])
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector') AS present"
                )
                value = bool(_scalar(cur.fetchone()))
    except Exception:
        value = False
    _EXT_CACHE["value"] = value
    _EXT_CACHE["ts"] = now
    return value


def column_is_vector(
    schema: str,
    table: str,
    column: str = "embedding",
    *,
    force_refresh: bool = False,
) -> bool:
    """True if a given column is stored as a pgvector `vector` type (cached)."""
    key = (schema, table, column)
    now = time.monotonic()
    cached = _COL_CACHE.get(key)
    if not force_refresh and cached is not None and (now - cached[1]) < _TTL_SEC:
        return cached[0]
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND column_name = %s
                    """,
                    (schema, table, column),
                )
                row = cur.fetchone()
                data_type = _scalar(row)
                value = data_type == "USER-DEFINED"
    except Exception:
        value = False
    _COL_CACHE[key] = (value, now)
    return value


def reset_caches() -> None:
    """Clear cached detection state (used by tests and after migrations)."""
    _EXT_CACHE["value"] = None
    _EXT_CACHE["ts"] = 0.0
    _COL_CACHE.clear()


def validate_embedding(embedding: Any, *, dim: int = EMBEDDING_DIM) -> list[float]:
    """Validate an embedding is a finite numeric vector of the expected dim."""
    try:
        values = list(embedding)
    except TypeError as exc:
        raise ValueError("embedding must be an iterable of numeric values") from exc
    if len(values) != dim:
        raise ValueError(f"embedding must have exactly {dim} dimensions, got {len(values)}")
    normalized: list[float] = []
    for index, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"embedding[{index}] is not numeric: {value!r}") from exc
        if not math.isfinite(number):
            raise ValueError(f"embedding[{index}] must be finite, got {number!r}")
        normalized.append(number)
    return normalized


def to_vector_literal(embedding: Any, *, dim: int = EMBEDDING_DIM) -> str:
    """Format an embedding as a pgvector literal: '[0.1,0.2,...]'."""
    normalized = validate_embedding(embedding, dim=dim)
    return "[" + ",".join(repr(x) for x in normalized) + "]"


def to_sql(embedding: Any, *, is_vector: bool, dim: int = EMBEDDING_DIM) -> str:
    """Format an embedding for SQL storage based on the active column mode."""
    if is_vector:
        return to_vector_literal(embedding, dim=dim)
    return json.dumps(validate_embedding(embedding, dim=dim))
