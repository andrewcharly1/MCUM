"""
PostgreSQL persistence and retrieval helpers for MCUM native code_graph.

The graph is stored in PostgreSQL so PostgREST can expose read-only views/RPCs
and MCUM can retrieve compact code context without reading whole projects.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
import uuid

from .connection import get_cursor, get_db


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def ensure_code_graph_schema() -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            cur.execute("CREATE SCHEMA IF NOT EXISTS code_graph")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.graphs (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    project_path TEXT NOT NULL,
                    project_name TEXT,
                    graph_version INT NOT NULL DEFAULT 1,
                    extractor_version TEXT NOT NULL DEFAULT 'mcum-code-graph-v1',
                    status TEXT NOT NULL DEFAULT 'active',
                    mode TEXT NOT NULL DEFAULT 'incremental',
                    source_hash TEXT,
                    files_total INT NOT NULL DEFAULT 0,
                    files_indexed INT NOT NULL DEFAULT 0,
                    files_skipped INT NOT NULL DEFAULT 0,
                    nodes_total INT NOT NULL DEFAULT 0,
                    edges_total INT NOT NULL DEFAULT 0,
                    tokens_indexed_estimate BIGINT NOT NULL DEFAULT 0,
                    tokens_context_saved_estimate BIGINT NOT NULL DEFAULT 0,
                    error_message TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (project_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.files (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    graph_id UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    absolute_path TEXT,
                    language TEXT NOT NULL DEFAULT 'text',
                    file_hash TEXT NOT NULL,
                    bytes_size BIGINT NOT NULL DEFAULT 0,
                    line_count INT NOT NULL DEFAULT 0,
                    token_estimate INT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    indexed_at TIMESTAMPTZ DEFAULT NOW(),
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    UNIQUE (graph_id, relative_path)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.nodes (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    graph_id UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
                    file_id UUID REFERENCES code_graph.files(id) ON DELETE CASCADE,
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    node_kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    signature TEXT,
                    line_start INT,
                    line_end INT,
                    doc_excerpt TEXT,
                    search_text TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (graph_id, qualified_name, file_id, line_start)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.edges (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    graph_id UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    source_node_id UUID REFERENCES code_graph.nodes(id) ON DELETE CASCADE,
                    target_node_id UUID REFERENCES code_graph.nodes(id) ON DELETE CASCADE,
                    source_ref TEXT,
                    target_ref TEXT NOT NULL,
                    edge_kind TEXT NOT NULL,
                    confidence FLOAT NOT NULL DEFAULT 0.70,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.index_runs (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    graph_id UUID REFERENCES code_graph.graphs(id) ON DELETE SET NULL,
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL DEFAULT 'incremental',
                    status TEXT NOT NULL DEFAULT 'running',
                    files_scanned INT NOT NULL DEFAULT 0,
                    files_indexed INT NOT NULL DEFAULT 0,
                    files_skipped INT NOT NULL DEFAULT 0,
                    nodes_indexed INT NOT NULL DEFAULT 0,
                    edges_indexed INT NOT NULL DEFAULT 0,
                    tokens_indexed_estimate BIGINT NOT NULL DEFAULT 0,
                    tokens_saved_estimate BIGINT NOT NULL DEFAULT 0,
                    duration_ms INT,
                    error_message TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS code_graph.experience_links (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    graph_id UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
                    experience_id UUID NOT NULL REFERENCES core_brain.experiences(id) ON DELETE CASCADE,
                    file_id UUID REFERENCES code_graph.files(id) ON DELETE SET NULL,
                    node_id UUID REFERENCES code_graph.nodes(id) ON DELETE SET NULL,
                    relative_path TEXT NOT NULL,
                    qualified_name TEXT NOT NULL DEFAULT '',
                    link_kind TEXT NOT NULL DEFAULT 'applies_to',
                    confidence FLOAT NOT NULL DEFAULT 0.80,
                    file_hash TEXT,
                    graph_version INT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (experience_id, relative_path, qualified_name, link_kind)
                )
                """
            )
            cur.execute(
                """
                DO $$ BEGIN
                    ALTER TABLE code_graph.edges DROP CONSTRAINT IF EXISTS edges_target_node_id_fkey;
                    ALTER TABLE code_graph.edges
                        ADD CONSTRAINT edges_target_node_id_fkey
                        FOREIGN KEY (target_node_id)
                        REFERENCES code_graph.nodes(id)
                        ON DELETE SET NULL;
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_graphs_project ON code_graph.graphs (project_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_files_graph_path ON code_graph.files (graph_id, relative_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_graph_kind ON code_graph.nodes (graph_id, node_kind, qualified_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_search ON code_graph.nodes USING GIN (to_tsvector('simple', search_text))")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_edges_graph_kind ON code_graph.edges (graph_id, edge_kind)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_experience_links_project ON code_graph.experience_links (project_id, relative_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_experience_links_node ON code_graph.experience_links (node_id)")
            cur.execute(
                """
                CREATE OR REPLACE VIEW code_graph.v_files AS
                SELECT
                    f.id, f.graph_id, f.project_id, g.project_name,
                    f.relative_path, f.absolute_path, f.language, f.file_hash,
                    f.bytes_size, f.line_count, f.token_estimate, f.status,
                    f.indexed_at, f.metadata
                FROM code_graph.files f
                JOIN code_graph.graphs g ON g.id = f.graph_id
                WHERE f.status = 'active'
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW code_graph.v_nodes AS
                SELECT
                    n.id, n.graph_id, n.project_id, g.project_name,
                    f.relative_path, f.language, n.node_kind, n.name,
                    n.qualified_name, n.signature, n.line_start, n.line_end,
                    n.doc_excerpt, n.metadata, n.created_at, n.updated_at
                FROM code_graph.nodes n
                JOIN code_graph.graphs g ON g.id = n.graph_id
                LEFT JOIN code_graph.files f ON f.id = n.file_id
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW code_graph.v_edges AS
                SELECT
                    e.id, e.graph_id, e.project_id, g.project_name,
                    e.edge_kind, e.source_ref,
                    src.qualified_name AS source_qualified_name,
                    e.target_ref,
                    dst.qualified_name AS target_qualified_name,
                    e.confidence, e.metadata, e.created_at
                FROM code_graph.edges e
                JOIN code_graph.graphs g ON g.id = e.graph_id
                LEFT JOIN code_graph.nodes src ON src.id = e.source_node_id
                LEFT JOIN code_graph.nodes dst ON dst.id = e.target_node_id
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW code_graph.v_experience_links AS
                SELECT
                    l.id, l.project_id, g.project_name, l.graph_id,
                    l.experience_id, e.title AS experience_title, e.category,
                    l.file_id, l.node_id, l.relative_path, l.qualified_name,
                    l.link_kind, l.confidence, l.file_hash, l.graph_version,
                    l.metadata, l.created_at, l.updated_at
                FROM code_graph.experience_links l
                JOIN code_graph.graphs g ON g.id = l.graph_id
                JOIN core_brain.experiences e ON e.id = l.experience_id
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION code_graph.context_pack(
                    p_project_id UUID,
                    p_query TEXT,
                    p_limit INT DEFAULT 12,
                    p_depth INT DEFAULT 1
                )
                RETURNS TABLE (
                    node_id UUID,
                    relative_path TEXT,
                    language TEXT,
                    node_kind TEXT,
                    qualified_name TEXT,
                    signature TEXT,
                    line_start INT,
                    line_end INT,
                    score FLOAT,
                    inbound_edges INT,
                    outbound_edges INT,
                    context_summary TEXT
                )
                LANGUAGE sql
                STABLE
                AS $$
                WITH active_graph AS (
                    SELECT id
                    FROM code_graph.graphs
                    WHERE project_id = p_project_id
                      AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                ),
                raw_query AS (
                    SELECT LOWER(COALESCE(NULLIF(p_query, ''), 'code')) AS query_text
                ),
                query_tokens AS (
                    SELECT regexp_replace(token, '[^a-z0-9_]+', '', 'g') AS token
                    FROM raw_query, regexp_split_to_table(query_text, '\\s+') AS token
                    WHERE length(regexp_replace(token, '[^a-z0-9_]+', '', 'g')) >= 2
                    LIMIT 12
                ),
                query_terms AS (
                    SELECT
                        (SELECT query_text FROM raw_query) AS query_text,
                        COALESCE(
                            to_tsquery('simple', NULLIF(string_agg(token || ':*', ' | '), '')),
                            plainto_tsquery('simple', 'code')
                        ) AS tsq,
                        ARRAY(SELECT '%' || token || '%' FROM query_tokens) AS like_queries
                    FROM query_tokens
                ),
                candidate_nodes AS (
                    SELECT
                        n.id AS node_id,
                        f.relative_path,
                        f.language,
                        n.node_kind,
                        n.qualified_name,
                        n.signature,
                        n.line_start,
                        n.line_end,
                        ts_rank_cd(to_tsvector('simple', COALESCE(n.search_text, '')), q.tsq) AS lexical_score,
                        (
                            SELECT COUNT(*)::FLOAT
                            FROM unnest(q.like_queries) AS token_match(pattern)
                            WHERE LOWER(COALESCE(n.search_text, '')) LIKE token_match.pattern
                        ) / GREATEST(1, cardinality(q.like_queries)) AS token_coverage,
                        CASE
                            WHEN LOWER(n.qualified_name) = q.query_text THEN 0.75
                            WHEN LOWER(n.qualified_name) LIKE '%.' || q.query_text THEN 0.65
                            WHEN LOWER(n.qualified_name) LIKE ANY(q.like_queries) THEN 0.35
                            WHEN LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries) THEN 0.25
                            ELSE 0
                        END AS path_score
                    FROM code_graph.nodes n
                    JOIN active_graph ag ON ag.id = n.graph_id
                    LEFT JOIN code_graph.files f ON f.id = n.file_id
                    CROSS JOIN query_terms q
                    WHERE to_tsvector('simple', COALESCE(n.search_text, '')) @@ q.tsq
                       OR LOWER(n.qualified_name) LIKE ANY(q.like_queries)
                       OR LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries)
                ),
                inbound_counts AS (
                    SELECT target_node_id AS node_id, COUNT(*)::INT AS inbound_edges
                    FROM code_graph.edges
                    WHERE target_node_id IS NOT NULL
                    GROUP BY target_node_id
                ),
                outbound_counts AS (
                    SELECT source_node_id AS node_id, COUNT(*)::INT AS outbound_edges
                    FROM code_graph.edges
                    WHERE source_node_id IS NOT NULL
                    GROUP BY source_node_id
                )
                SELECT
                    cn.node_id,
                    cn.relative_path,
                    cn.language,
                    cn.node_kind,
                    cn.qualified_name,
                    cn.signature,
                    cn.line_start,
                    cn.line_end,
                    (
                        LEAST(0.20, cn.lexical_score)
                        + (cn.token_coverage * 0.60)
                        + cn.path_score
                        + LEAST(0.15, (COALESCE(ic.inbound_edges, 0) + COALESCE(oc.outbound_edges, 0)) * 0.01)
                    )::FLOAT AS score,
                    COALESCE(ic.inbound_edges, 0)::INT AS inbound_edges,
                    COALESCE(oc.outbound_edges, 0)::INT AS outbound_edges,
                    CONCAT(
                        cn.node_kind, ' ', cn.qualified_name,
                        CASE WHEN cn.relative_path IS NOT NULL THEN ' @ ' || cn.relative_path ELSE '' END,
                        CASE WHEN cn.line_start IS NOT NULL THEN ':' || cn.line_start::TEXT ELSE '' END,
                        CASE WHEN cn.signature IS NOT NULL AND cn.signature <> '' THEN ' | ' || cn.signature ELSE '' END
                    ) AS context_summary
                FROM candidate_nodes cn
                LEFT JOIN inbound_counts ic ON ic.node_id = cn.node_id
                LEFT JOIN outbound_counts oc ON oc.node_id = cn.node_id
                ORDER BY score DESC, cn.relative_path ASC, cn.line_start ASC
                LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 12), 50));
                $$
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION code_graph.context_pack_filtered(
                    p_project_id UUID,
                    p_query TEXT,
                    p_limit INT DEFAULT 12,
                    p_depth INT DEFAULT 1,
                    p_languages TEXT[] DEFAULT NULL,
                    p_exclude_languages TEXT[] DEFAULT NULL,
                    p_path_prefix TEXT DEFAULT NULL,
                    p_node_kinds TEXT[] DEFAULT NULL
                )
                RETURNS TABLE (
                    node_id UUID,
                    relative_path TEXT,
                    language TEXT,
                    node_kind TEXT,
                    qualified_name TEXT,
                    signature TEXT,
                    line_start INT,
                    line_end INT,
                    score FLOAT,
                    inbound_edges INT,
                    outbound_edges INT,
                    context_summary TEXT
                )
                LANGUAGE sql
                STABLE
                AS $$
                SELECT cp.*
                FROM code_graph.context_pack(p_project_id, p_query, 50, p_depth) cp
                WHERE (
                        p_languages IS NULL
                        OR cardinality(p_languages) = 0
                        OR LOWER(COALESCE(cp.language, '')) = ANY(p_languages)
                    )
                  AND (
                        p_exclude_languages IS NULL
                        OR cardinality(p_exclude_languages) = 0
                        OR NOT (LOWER(COALESCE(cp.language, '')) = ANY(p_exclude_languages))
                    )
                  AND (
                        NULLIF(TRIM(BOTH '/' FROM COALESCE(p_path_prefix, '')), '') IS NULL
                        OR LOWER(COALESCE(cp.relative_path, '')) LIKE LOWER(TRIM(BOTH '/' FROM p_path_prefix)) || '%%'
                    )
                  AND (
                        p_node_kinds IS NULL
                        OR cardinality(p_node_kinds) = 0
                        OR LOWER(cp.node_kind) = ANY(p_node_kinds)
                    )
                ORDER BY cp.score DESC, cp.relative_path ASC, cp.line_start ASC
                LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 12), 50));
                $$
                """
            )


def get_code_graph_manifest(*, project_id: str, ensure_schema: bool = True) -> dict[str, Any]:
    """Return the active graph and its file manifest for incremental comparison."""
    if ensure_schema:
        ensure_code_graph_schema()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, graph_version, project_path, project_name, status, extractor_version
                FROM code_graph.graphs
                WHERE project_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (project_id,),
            )
            graph = dict(cur.fetchone() or {})
            if not graph:
                return {"graph": None, "files": {}}
            cur.execute(
                """
                SELECT relative_path, absolute_path, language, file_hash,
                       bytes_size, line_count, token_estimate, status, metadata
                FROM code_graph.files
                WHERE graph_id = %s AND status = 'active'
                ORDER BY relative_path
                """,
                (graph["id"],),
            )
            files = {str(row["relative_path"]): dict(row) for row in cur.fetchall()}
            return {"graph": graph, "files": files}


def mark_code_graph_stale(*, project_id: str, error_message: str | None = None) -> None:
    ensure_code_graph_schema()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE code_graph.graphs
                SET status = 'stale', error_message = %s, updated_at = NOW()
                WHERE project_id = %s
                """,
                (str(error_message or "")[:2000] or None, project_id),
            )


def _node_maps(cur: Any, graph_id: str) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    cur.execute(
        """
        SELECT n.id, f.relative_path, n.qualified_name
        FROM code_graph.nodes n
        LEFT JOIN code_graph.files f ON f.id = n.file_id
        WHERE n.graph_id = %s
        ORDER BY n.created_at, n.id
        """,
        (graph_id,),
    )
    by_key: dict[tuple[str, str], str] = {}
    by_qualified: dict[str, str] = {}
    for row in cur.fetchall():
        relative_path = str(row.get("relative_path") or "")
        qualified = str(row.get("qualified_name") or "")
        node_id = str(row["id"])
        by_key.setdefault((relative_path, qualified), node_id)
        by_qualified.setdefault(qualified, node_id)
    return by_key, by_qualified


def _refresh_unresolved_edge_targets(cur: Any, graph_id: str) -> None:
    _, by_qualified = _node_maps(cur, graph_id)
    cur.execute(
        """
        SELECT id, source_ref, target_ref, source_node_id, target_node_id
        FROM code_graph.edges
        WHERE graph_id = %s AND (source_node_id IS NULL OR target_node_id IS NULL)
        """,
        (graph_id,),
    )
    for row in cur.fetchall():
        source_node_id = row.get("source_node_id") or by_qualified.get(str(row.get("source_ref") or ""))
        target_node_id = row.get("target_node_id") or by_qualified.get(str(row.get("target_ref") or ""))
        if source_node_id == row.get("source_node_id") and target_node_id == row.get("target_node_id"):
            continue
        cur.execute(
            "UPDATE code_graph.edges SET source_node_id = %s, target_node_id = %s WHERE id = %s",
            (source_node_id, target_node_id, row["id"]),
        )


def _refresh_experience_link_targets(cur: Any, graph_id: str) -> None:
    cur.execute(
        """
        SELECT l.id, l.relative_path, l.qualified_name, g.graph_version
        FROM code_graph.experience_links l
        JOIN code_graph.graphs g ON g.id = l.graph_id
        WHERE l.graph_id = %s
        """,
        (graph_id,),
    )
    for link in cur.fetchall():
        cur.execute(
            """
            SELECT f.id AS file_id, f.file_hash,
                   COALESCE(exact_node.id, module_node.id) AS node_id
            FROM code_graph.files f
            LEFT JOIN code_graph.nodes exact_node
              ON exact_node.file_id = f.id
             AND exact_node.qualified_name = %s
            LEFT JOIN LATERAL (
                SELECT candidate.id
                FROM code_graph.nodes candidate
                WHERE candidate.file_id = f.id
                  AND candidate.node_kind IN ('module', 'sql_file')
                ORDER BY candidate.line_start NULLS LAST, candidate.id
                LIMIT 1
            ) module_node ON TRUE
            WHERE f.graph_id = %s
              AND f.relative_path = %s
              AND f.status = 'active'
            LIMIT 1
            """,
            (link.get("qualified_name") or "", graph_id, link["relative_path"]),
        )
        target = cur.fetchone()
        cur.execute(
            """
            UPDATE code_graph.experience_links
            SET file_id = %s, node_id = %s, file_hash = %s,
                graph_version = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (
                target.get("file_id") if target else None,
                target.get("node_id") if target else None,
                target.get("file_hash") if target else None,
                link["graph_version"],
                link["id"],
            ),
        )


def _upsert_graph(
    cur: Any,
    *,
    project_id: str,
    project_path: str,
    project_name: str | None,
    mode: str,
    metadata: dict[str, Any],
) -> str:
    graph_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO code_graph.graphs (
            id, project_id, project_path, project_name, mode, status,
            extractor_version, metadata, started_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'building', %s, %s, %s, %s)
        ON CONFLICT (project_id) DO UPDATE SET
            project_path = EXCLUDED.project_path,
            project_name = EXCLUDED.project_name,
            mode = EXCLUDED.mode,
            status = 'building',
            metadata = EXCLUDED.metadata,
            started_at = EXCLUDED.started_at,
            error_message = NULL,
            graph_version = code_graph.graphs.graph_version + 1,
            updated_at = EXCLUDED.updated_at
        RETURNING id
        """,
        (
            graph_id,
            project_id,
            project_path,
            project_name,
            mode,
            str(metadata.get("extractor_version") or "mcum-code-graph-v1"),
            _json(metadata),
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        ),
    )
    row = cur.fetchone()
    return str(row["id"])


def _insert_index_payload(
    cur: Any,
    *,
    graph_id: str,
    project_id: str,
    files: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    file_ids: dict[str, str] = {}
    for item in files:
        file_id = str(uuid.uuid4())
        relative_path = str(item.get("relative_path") or "")
        file_ids[relative_path] = file_id
        cur.execute(
            """
            INSERT INTO code_graph.files (
                id, graph_id, project_id, relative_path, absolute_path, language,
                file_hash, bytes_size, line_count, token_estimate, status, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                file_id,
                graph_id,
                project_id,
                relative_path,
                item.get("absolute_path"),
                item.get("language") or "text",
                item.get("file_hash") or "",
                int(item.get("bytes_size") or 0),
                int(item.get("line_count") or 0),
                int(item.get("token_estimate") or 0),
                item.get("status") or "active",
                _json(item.get("metadata") or {}),
            ),
        )

    for item in nodes:
        relative_path = str(item.get("relative_path") or "")
        qualified = str(item.get("qualified_name") or item.get("name") or "")
        search_text = item.get("search_text") or " ".join(
            str(value or "")
            for value in (
                item.get("node_kind"),
                item.get("name"),
                qualified,
                item.get("signature"),
                relative_path,
                item.get("doc_excerpt"),
            )
        )
        cur.execute(
            """
            INSERT INTO code_graph.nodes (
                id, graph_id, file_id, project_id, node_kind, name,
                qualified_name, signature, line_start, line_end,
                doc_excerpt, search_text, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (graph_id, qualified_name, file_id, line_start)
            DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                graph_id,
                file_ids.get(relative_path),
                project_id,
                item.get("node_kind") or "symbol",
                item.get("name") or qualified,
                qualified,
                item.get("signature"),
                item.get("line_start"),
                item.get("line_end"),
                item.get("doc_excerpt"),
                search_text,
                _json(item.get("metadata") or {}),
            ),
        )

    node_ids_by_key, node_ids_by_qualified = _node_maps(cur, graph_id)
    for item in edges:
        source_ref = str(item.get("source_ref") or "")
        target_ref = str(item.get("target_ref") or "")
        source_path = str(item.get("source_path") or "")
        target_path = str(item.get("target_path") or "")
        source_node_id = node_ids_by_key.get((source_path, source_ref)) or node_ids_by_qualified.get(source_ref)
        target_node_id = node_ids_by_key.get((target_path, target_ref)) or node_ids_by_qualified.get(target_ref)
        edge_metadata = dict(item.get("metadata") or {})
        edge_metadata.update({"source_path": source_path or None, "target_path": target_path or None})
        cur.execute(
            """
            INSERT INTO code_graph.edges (
                id, graph_id, project_id, source_node_id, target_node_id,
                source_ref, target_ref, edge_kind, confidence, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                graph_id,
                project_id,
                source_node_id,
                target_node_id,
                source_ref or None,
                target_ref,
                item.get("edge_kind") or "references",
                float(item.get("confidence") or 0.70),
                _json(edge_metadata),
            ),
        )


def persist_index_result(
    *,
    project_id: str,
    project_path: str,
    project_name: str | None,
    mode: str,
    index_result: dict[str, Any],
    ensure_schema: bool = True,
) -> dict[str, Any]:
    if ensure_schema:
        ensure_code_graph_schema()
    started = time.perf_counter()
    files = list(index_result.get("files") or [])
    nodes = list(index_result.get("nodes") or [])
    edges = list(index_result.get("edges") or [])
    stats = dict(index_result.get("stats") or {})
    delta = dict(index_result.get("delta") or {})
    metadata = {**dict(index_result.get("metadata") or {}), "delta": delta}

    if mode == "incremental" and not bool(delta.get("has_changes", True)):
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE code_graph.graphs
                    SET status = 'active', error_message = NULL, updated_at = NOW()
                    WHERE project_id = %s
                    RETURNING id
                    """,
                    (project_id,),
                )
                graph = dict(cur.fetchone() or {})
        return {
            "graph_id": str(graph.get("id") or ""),
            "index_run_id": None,
            "status": "no_changes",
            "files_indexed": 0,
            "files_skipped": int(stats.get("files_skipped") or 0),
            "nodes_indexed": 0,
            "edges_indexed": 0,
            "tokens_indexed_estimate": int(stats.get("tokens_indexed_estimate") or 0),
            "tokens_saved_estimate": int(stats.get("tokens_saved_estimate") or 0),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "delta": delta,
        }

    with get_db() as conn:
        with get_cursor(conn) as cur:
            graph_id = _upsert_graph(
                cur,
                project_id=project_id,
                project_path=project_path,
                project_name=project_name,
                mode=mode,
                metadata=metadata,
            )
            run_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO code_graph.index_runs (
                    id, graph_id, project_id, mode, status, files_scanned,
                    files_indexed, files_skipped, nodes_indexed, edges_indexed,
                    tokens_indexed_estimate, tokens_saved_estimate, metadata
                ) VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    graph_id,
                    project_id,
                    mode,
                    int(stats.get("files_scanned") or len(files)),
                    int(stats.get("files_indexed") or len(files)),
                    int(stats.get("files_skipped") or 0),
                    len(nodes),
                    len(edges),
                    int(stats.get("tokens_indexed_estimate") or 0),
                    int(stats.get("tokens_saved_estimate") or 0),
                    _json(metadata),
                ),
            )

            if mode == "full":
                cur.execute("DELETE FROM code_graph.edges WHERE graph_id = %s", (graph_id,))
                cur.execute("DELETE FROM code_graph.files WHERE graph_id = %s", (graph_id,))
            else:
                affected_paths = sorted(
                    set(delta.get("changed_paths") or [])
                    | set(delta.get("deleted_paths") or [])
                )
                if affected_paths:
                    cur.execute(
                        "DELETE FROM code_graph.files WHERE graph_id = %s AND relative_path = ANY(%s)",
                        (graph_id, affected_paths),
                    )

            _insert_index_payload(
                cur,
                graph_id=graph_id,
                project_id=project_id,
                files=files,
                nodes=nodes,
                edges=edges,
            )
            _refresh_unresolved_edge_targets(cur, graph_id)
            _refresh_experience_link_targets(cur, graph_id)

            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM code_graph.files WHERE graph_id = %s AND status = 'active') AS files_total,
                    (SELECT COUNT(*) FROM code_graph.nodes WHERE graph_id = %s) AS nodes_total,
                    (SELECT COUNT(*) FROM code_graph.edges WHERE graph_id = %s) AS edges_total
                """,
                (graph_id, graph_id, graph_id),
            )
            totals = dict(cur.fetchone() or {})
            duration_ms = int((time.perf_counter() - started) * 1000)
            now = datetime.now(timezone.utc)
            cur.execute(
                """
                UPDATE code_graph.graphs
                SET status = 'active',
                    extractor_version = %s,
                    files_total = %s,
                    files_indexed = %s,
                    files_skipped = %s,
                    nodes_total = %s,
                    edges_total = %s,
                    tokens_indexed_estimate = %s,
                    tokens_context_saved_estimate = %s,
                    finished_at = %s,
                    updated_at = %s,
                    error_message = NULL
                WHERE id = %s
                """,
                (
                    str(metadata.get("extractor_version") or "mcum-code-graph-v1"),
                    int(stats.get("files_scanned") or totals.get("files_total") or 0),
                    int(totals.get("files_total") or 0),
                    int(stats.get("files_skipped") or 0),
                    int(totals.get("nodes_total") or 0),
                    int(totals.get("edges_total") or 0),
                    int(stats.get("tokens_project_estimate") or stats.get("tokens_indexed_estimate") or 0),
                    int(stats.get("tokens_saved_estimate") or 0),
                    now,
                    now,
                    graph_id,
                ),
            )
            cur.execute(
                """
                UPDATE code_graph.index_runs
                SET status = 'success', duration_ms = %s, finished_at = %s
                WHERE id = %s
                """,
                (duration_ms, now, run_id),
            )
            return {
                "graph_id": graph_id,
                "index_run_id": run_id,
                "status": "success",
                "files_indexed": len(files),
                "files_total": int(totals.get("files_total") or 0),
                "files_skipped": int(stats.get("files_skipped") or 0),
                "nodes_indexed": len(nodes),
                "nodes_total": int(totals.get("nodes_total") or 0),
                "edges_indexed": len(edges),
                "edges_total": int(totals.get("edges_total") or 0),
                "tokens_indexed_estimate": int(stats.get("tokens_indexed_estimate") or 0),
                "tokens_saved_estimate": int(stats.get("tokens_saved_estimate") or 0),
                "duration_ms": duration_ms,
                "delta": delta,
            }


def _relative_project_path(path: str, project_path: str) -> str | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(project_path) / candidate
        return candidate.resolve(strict=False).relative_to(Path(project_path).resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return None


def link_experience_to_code_graph(
    *,
    experience_id: str,
    project_id: str,
    paths: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    link_kind: str = "modified",
    confidence: float = 0.90,
    ensure_schema: bool = True,
) -> dict[str, Any]:
    """Link an experience to stable graph paths and the best matching symbols."""
    if ensure_schema:
        ensure_code_graph_schema()
    linked = 0
    requested: list[tuple[str, int | None]] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, project_path, graph_version
                FROM code_graph.graphs
                WHERE project_id = %s AND status IN ('active', 'stale')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (project_id,),
            )
            graph = cur.fetchone()
            if not graph:
                return {"linked": 0, "status": "not_indexed"}
            for path in paths or []:
                relative = _relative_project_path(path, graph["project_path"])
                if relative:
                    requested.append((relative, None))
            for ref in evidence_refs or []:
                if not isinstance(ref, dict):
                    continue
                relative = _relative_project_path(str(ref.get("path") or ref.get("file") or ""), graph["project_path"])
                if relative:
                    requested.append((relative, int(ref.get("line_start") or 0) or None))

            seen: set[tuple[str, int | None]] = set()
            for relative_path, line_start in requested:
                if (relative_path, line_start) in seen:
                    continue
                seen.add((relative_path, line_start))
                cur.execute(
                    """
                    SELECT f.id AS file_id, f.file_hash,
                           n.id AS node_id, COALESCE(n.qualified_name, '') AS qualified_name
                    FROM code_graph.files f
                    LEFT JOIN LATERAL (
                        SELECT candidate.id, candidate.qualified_name
                        FROM code_graph.nodes candidate
                        WHERE candidate.file_id = f.id
                        ORDER BY
                            CASE
                                WHEN %s::INT IS NOT NULL
                                 AND candidate.line_start <= %s::INT
                                 AND COALESCE(candidate.line_end, candidate.line_start) >= %s::INT
                                THEN 0
                                WHEN candidate.node_kind IN ('module', 'sql_file') THEN 1
                                ELSE 2
                            END,
                            candidate.line_start NULLS LAST,
                            candidate.id
                        LIMIT 1
                    ) n ON TRUE
                    WHERE f.graph_id = %s AND f.relative_path = %s AND f.status = 'active'
                    LIMIT 1
                    """,
                    (line_start, line_start, line_start, graph["id"], relative_path),
                )
                target = cur.fetchone()
                if not target:
                    continue
                cur.execute(
                    """
                    INSERT INTO code_graph.experience_links (
                        project_id, graph_id, experience_id, file_id, node_id,
                        relative_path, qualified_name, link_kind, confidence,
                        file_hash, graph_version, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (experience_id, relative_path, qualified_name, link_kind)
                    DO UPDATE SET
                        file_id = EXCLUDED.file_id,
                        node_id = EXCLUDED.node_id,
                        confidence = GREATEST(code_graph.experience_links.confidence, EXCLUDED.confidence),
                        file_hash = EXCLUDED.file_hash,
                        graph_version = EXCLUDED.graph_version,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    (
                        project_id,
                        graph["id"],
                        experience_id,
                        target["file_id"],
                        target.get("node_id"),
                        relative_path,
                        target.get("qualified_name") or "",
                        link_kind,
                        max(0.0, min(float(confidence), 1.0)),
                        target.get("file_hash"),
                        graph["graph_version"],
                        _json({"line_start": line_start}),
                    ),
                )
                linked += 1
    return {"linked": linked, "status": "success", "paths_considered": len(requested)}


def backfill_experience_code_links(
    *,
    project_id: str,
    limit: int = 500,
    ensure_schema: bool = True,
) -> dict[str, Any]:
    """Create missing graph links from experience source snapshots."""
    if ensure_schema:
        ensure_code_graph_schema()
    rows: list[dict[str, Any]] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT e.id, e.source_artifacts, e.evidence_refs
                FROM core_brain.experiences e
                WHERE e.project_id = %s
                  AND e.superseded_by IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM code_graph.experience_links l
                      WHERE l.experience_id = e.id
                  )
                ORDER BY e.last_validated_at DESC
                LIMIT %s
                """,
                (project_id, max(1, min(int(limit), 5000))),
            )
            rows = [dict(row) for row in cur.fetchall()]
    linked = 0
    experiences_linked = 0
    for row in rows:
        artifacts = row.get("source_artifacts") or []
        refs = row.get("evidence_refs") or []
        paths = [
            str(item.get("path") or "")
            for item in artifacts
            if isinstance(item, dict) and item.get("path")
        ]
        result = link_experience_to_code_graph(
            experience_id=str(row["id"]),
            project_id=project_id,
            paths=paths,
            evidence_refs=refs if isinstance(refs, list) else [],
            link_kind="applies_to",
            confidence=0.75,
            ensure_schema=False,
        )
        if int(result.get("linked") or 0):
            experiences_linked += 1
            linked += int(result["linked"])
    return {"status": "success", "experiences_scanned": len(rows), "experiences_linked": experiences_linked, "links_created": linked}


def _linked_experiences_for_hits(
    cur: Any,
    *,
    project_id: str,
    hits: list[dict[str, Any]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    node_ids = [str(hit.get("node_id")) for hit in hits if hit.get("node_id")]
    paths = [str(hit.get("relative_path")) for hit in hits if hit.get("relative_path")]
    if not node_ids and not paths:
        return []
    cur.execute(
        """
        SELECT DISTINCT ON (e.id)
            e.id, e.category, e.title, e.content, e.applicability,
            e.not_applicable_cases, e.conditions, e.current_confidence,
            e.evidence_refs, e.source_artifacts, e.project_id, e.skill_name,
            e.skill_version, e.task_description, e.last_validated_at,
            l.relative_path AS code_relative_path,
            l.qualified_name AS code_qualified_name,
            l.link_kind AS code_link_kind,
            l.confidence AS code_link_confidence
        FROM code_graph.experience_links l
        JOIN core_brain.experiences e ON e.id = l.experience_id
        WHERE l.project_id = %s
          AND e.superseded_by IS NULL
          AND (l.node_id = ANY(%s::uuid[]) OR l.relative_path = ANY(%s::text[]))
        ORDER BY e.id, l.confidence DESC, e.current_confidence DESC
        LIMIT %s
        """,
        (project_id, node_ids or [], paths or [], max(1, min(int(limit), 20))),
    )
    return [dict(row) for row in cur.fetchall()]


def infer_code_graph_filters(query: str) -> dict[str, Any]:
    """Infer a conservative single-layer filter from an explicit task query."""
    tokens = set(str(query or "").lower().replace("\\", "/").replace("-", " ").split())
    if tokens & {"architecture", "arquitectura", "fullstack", "cross-layer", "transversal", "completo"}:
        return {}
    groups = {
        "dart": {"dart", "flutter", "mobile", "movil", "conductor"},
        "go": {"golang", "go"},
        "sql": {"sql", "migration", "migrations", "rls", "database"},
        "web": {"react", "jsx", "frontend", "vite"},
    }
    matched = [name for name, keywords in groups.items() if tokens & keywords]
    if len(matched) != 1:
        return {}
    mapping = {
        "dart": ["dart"],
        "go": ["go"],
        "sql": ["sql"],
        "web": ["javascript", "typescript"],
    }
    return {"languages": mapping[matched[0]], "inferred": True}


def retrieve_code_graph_context(
    *,
    project_id: str,
    query: str,
    limit: int = 8,
    depth: int = 1,
    languages: list[str] | None = None,
    exclude_languages: list[str] | None = None,
    path_prefix: str | None = None,
    node_kinds: list[str] | None = None,
) -> dict[str, Any]:
    try:
        uuid.UUID(str(project_id))
    except (TypeError, ValueError):
        return {
            "enabled": False,
            "hits": [],
            "hits_retrieved": 0,
            "tokens_used_estimate": 0,
            "warnings": [],
            "metadata": {"query": query, "limit": limit, "depth": depth, "status": "invalid_project_id"},
        }
    hits: list[dict[str, Any]] = []
    linked_experiences: list[dict[str, Any]] = []
    filters = {
        "languages": sorted({str(item).lower() for item in (languages or []) if str(item).strip()}),
        "exclude_languages": sorted({str(item).lower() for item in (exclude_languages or []) if str(item).strip()}),
        "path_prefix": str(path_prefix or "").replace("\\", "/").strip("/"),
        "node_kinds": sorted({str(item).lower() for item in (node_kinds or []) if str(item).strip()}),
    }
    filter_clauses: list[str] = []
    filter_params: list[Any] = []
    if filters["languages"]:
        filter_clauses.append("AND LOWER(COALESCE(f.language, '')) = ANY(%s::text[])")
        filter_params.append(filters["languages"])
    if filters["exclude_languages"]:
        filter_clauses.append("AND NOT (LOWER(COALESCE(f.language, '')) = ANY(%s::text[]))")
        filter_params.append(filters["exclude_languages"])
    if filters["path_prefix"]:
        filter_clauses.append("AND LOWER(COALESCE(f.relative_path, '')) LIKE %s")
        filter_params.append(filters["path_prefix"].lower() + "%")
    if filters["node_kinds"]:
        filter_clauses.append("AND LOWER(n.node_kind) = ANY(%s::text[])")
        filter_params.append(filters["node_kinds"])
    filter_sql = "\n                       ".join(filter_clauses)
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT to_regclass('code_graph.graphs') AS table_name")
            if not (cur.fetchone() or {}).get("table_name"):
                return {
                    "enabled": False,
                    "hits": [],
                    "hits_retrieved": 0,
                    "tokens_used_estimate": 0,
                    "warnings": [],
                    "metadata": {"query": query, "limit": limit, "depth": depth, "status": "not_indexed"},
                }
            cur.execute(
                f"""
                WITH active_graph AS (
                    SELECT id
                    FROM code_graph.graphs
                    WHERE project_id = %s
                      AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                ),
                raw_query AS (
                    SELECT LOWER(COALESCE(NULLIF(%s, ''), 'code')) AS query_text
                ),
                query_tokens AS (
                    SELECT regexp_replace(token, '[^a-z0-9_]+', '', 'g') AS token
                    FROM raw_query, regexp_split_to_table(query_text, '\\s+') AS token
                    WHERE length(regexp_replace(token, '[^a-z0-9_]+', '', 'g')) >= 2
                    LIMIT 12
                ),
                query_terms AS (
                    SELECT
                        (SELECT query_text FROM raw_query) AS query_text,
                        COALESCE(
                            to_tsquery('simple', NULLIF(string_agg(token || ':*', ' | '), '')),
                            plainto_tsquery('simple', 'code')
                        ) AS tsq,
                        ARRAY(SELECT '%%' || token || '%%' FROM query_tokens) AS like_queries
                    FROM query_tokens
                ),
                candidate_nodes AS (
                    SELECT
                        n.id AS node_id,
                        f.relative_path,
                        f.language,
                        n.node_kind,
                        n.qualified_name,
                        n.signature,
                        n.line_start,
                        n.line_end,
                        ts_rank_cd(to_tsvector('simple', COALESCE(n.search_text, '')), q.tsq) AS lexical_score,
                        (
                            SELECT COUNT(*)::FLOAT
                            FROM unnest(q.like_queries) AS token_match(pattern)
                            WHERE LOWER(COALESCE(n.search_text, '')) LIKE token_match.pattern
                        ) / GREATEST(1, cardinality(q.like_queries)) AS token_coverage,
                        CASE
                            WHEN LOWER(n.qualified_name) = q.query_text THEN 0.75
                            WHEN LOWER(n.qualified_name) LIKE '%%.' || q.query_text THEN 0.65
                            WHEN LOWER(n.qualified_name) LIKE ANY(q.like_queries) THEN 0.35
                            WHEN LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries) THEN 0.25
                            ELSE 0
                        END AS path_score
                    FROM code_graph.nodes n
                    JOIN active_graph ag ON ag.id = n.graph_id
                    LEFT JOIN code_graph.files f ON f.id = n.file_id
                    CROSS JOIN query_terms q
                    WHERE (
                        to_tsvector('simple', COALESCE(n.search_text, '')) @@ q.tsq
                        OR LOWER(n.qualified_name) LIKE ANY(q.like_queries)
                        OR LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries)
                    )
                       {filter_sql}
                ),
                inbound_counts AS (
                    SELECT target_node_id AS node_id, COUNT(*)::INT AS inbound_edges
                    FROM code_graph.edges
                    WHERE target_node_id IS NOT NULL
                    GROUP BY target_node_id
                ),
                outbound_counts AS (
                    SELECT source_node_id AS node_id, COUNT(*)::INT AS outbound_edges
                    FROM code_graph.edges
                    WHERE source_node_id IS NOT NULL
                    GROUP BY source_node_id
                )
                SELECT
                    cn.node_id,
                    cn.relative_path,
                    cn.language,
                    cn.node_kind,
                    cn.qualified_name,
                    cn.signature,
                    cn.line_start,
                    cn.line_end,
                    (
                        LEAST(0.20, cn.lexical_score)
                        + (cn.token_coverage * 0.60)
                        + cn.path_score
                        + LEAST(0.15, (COALESCE(ic.inbound_edges, 0) + COALESCE(oc.outbound_edges, 0)) * 0.01)
                    )::FLOAT AS score,
                    COALESCE(ic.inbound_edges, 0)::INT AS inbound_edges,
                    COALESCE(oc.outbound_edges, 0)::INT AS outbound_edges,
                    CONCAT(
                        cn.node_kind, ' ', cn.qualified_name,
                        CASE WHEN cn.relative_path IS NOT NULL THEN ' @ ' || cn.relative_path ELSE '' END,
                        CASE WHEN cn.line_start IS NOT NULL THEN ':' || cn.line_start::TEXT ELSE '' END,
                        CASE WHEN cn.signature IS NOT NULL AND cn.signature <> '' THEN ' | ' || cn.signature ELSE '' END
                    ) AS context_summary
                FROM candidate_nodes cn
                LEFT JOIN inbound_counts ic ON ic.node_id = cn.node_id
                LEFT JOIN outbound_counts oc ON oc.node_id = cn.node_id
                ORDER BY score DESC, cn.relative_path ASC, cn.line_start ASC
                LIMIT %s
                """,
                (project_id, query, *filter_params, max(1, min(int(limit or 8), 50))),
            )
            for row in cur.fetchall():
                item = dict(row)
                item["category"] = "code_graph"
                item["title"] = item.get("context_summary") or item.get("qualified_name")
                item["content"] = {
                    "conclusion": item.get("context_summary"),
                    "context": (
                        f"{item.get('relative_path')}:{item.get('line_start') or '?'} "
                        f"kind={item.get('node_kind')} edges=in:{item.get('inbound_edges')} out:{item.get('outbound_edges')}"
                    ),
                }
                item["evidence_refs"] = [
                    {
                        "path": item.get("relative_path"),
                        "line_start": item.get("line_start"),
                        "line_end": item.get("line_end"),
                    }
                ]
                hits.append(item)
            linked_experiences = _linked_experiences_for_hits(
                cur,
                project_id=project_id,
                hits=hits,
                limit=min(5, max(1, int(limit or 8))),
            )
    return {
        "enabled": True,
        "hits": hits,
        "linked_experiences": linked_experiences,
        "hits_retrieved": len(hits),
        "tokens_used_estimate": sum(max(1, len(json.dumps(hit, default=str)) // 4) for hit in hits),
        "metadata": {"query": query, "limit": limit, "depth": depth, "filters": filters},
    }
