"""Federated project graph projection for MCUM.

The projection links existing MCUM sources without replacing their source of
truth. It is safe to rebuild and intentionally stores compact summaries and
stable references instead of complete source bodies.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
import uuid

from .connection import get_cursor, get_db


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _compact_code_graph_sync(value: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(value or {})
    delta = dict(source.get("delta") or {})
    scan = dict(source.get("scan_stats") or {})
    return {
        key: source.get(key)
        for key in (
            "status",
            "trigger",
            "mode",
            "graph_id",
            "index_run_id",
            "files_indexed",
            "nodes_indexed",
            "edges_indexed",
            "tokens_indexed_estimate",
            "tokens_saved_estimate",
            "wall_clock_ms",
        )
        if source.get(key) is not None
    } | {
        "delta": {
            "has_changes": bool(delta.get("has_changes", False)),
            "changed_paths": list(delta.get("changed_paths") or [])[:40],
            "new_paths": list(delta.get("new_paths") or [])[:40],
            "modified_paths": list(delta.get("modified_paths") or [])[:40],
            "deleted_paths": list(delta.get("deleted_paths") or [])[:40],
            "unchanged_count": len(delta.get("unchanged_paths") or []),
        },
        "scan_stats": {
            key: scan.get(key)
            for key in (
                "files_scanned",
                "files_indexed",
                "files_skipped",
                "directories_pruned",
                "files_new",
                "files_modified",
                "files_deleted",
                "files_unchanged",
                "nodes_indexed",
                "edges_indexed",
                "tokens_project_estimate",
                "tokens_saved_estimate",
            )
            if scan.get(key) is not None
        },
    }


def ensure_unified_graph_schema() -> None:
    """Bootstrap the unified graph schema.

    This setup-only function may execute DDL. Runtime entrypoints must use
    `_require_unified_graph_schema()` so normal reads and syncs never acquire
    schema locks.
    """
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
            cur.execute("CREATE SCHEMA IF NOT EXISTS mcum_graph")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mcum_graph.entities (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    entity_type TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    source_schema TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    content_hash TEXT,
                    confidence FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
                    provenance_kind TEXT NOT NULL DEFAULT 'extracted',
                    health_state TEXT NOT NULL DEFAULT 'active',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    valid_to TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (project_id, source_schema, source_table, source_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mcum_graph.relations (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    source_entity_id UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
                    target_entity_id UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    weight FLOAT NOT NULL DEFAULT 1.0,
                    confidence FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
                    provenance_kind TEXT NOT NULL DEFAULT 'extracted',
                    evidence_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    valid_to TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (project_id, source_entity_id, target_entity_id, relation_type)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mcum_graph.snapshots (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    code_graph_version INT,
                    entity_count INT NOT NULL DEFAULT 0,
                    relation_count INT NOT NULL DEFAULT 0,
                    source_hash TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mcum_graph.context_packs (
                    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    project_id UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
                    snapshot_id UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
                    session_id TEXT,
                    agent_role TEXT NOT NULL DEFAULT 'coordinator',
                    task_query TEXT NOT NULL,
                    token_budget INT NOT NULL DEFAULT 0,
                    token_estimate INT NOT NULL DEFAULT 0,
                    envelope JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcum_graph_entities_project_type "
                "ON mcum_graph.entities (project_id, entity_type, health_state)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mcum_graph_entities_active_canonical "
                "ON mcum_graph.entities (project_id, canonical_key) WHERE valid_to IS NULL"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcum_graph_entities_search "
                "ON mcum_graph.entities USING GIN (to_tsvector('simple', title || ' ' || COALESCE(summary, '')))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcum_graph_relations_source "
                "ON mcum_graph.relations (project_id, source_entity_id, relation_type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcum_graph_relations_target "
                "ON mcum_graph.relations (project_id, target_entity_id, relation_type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcum_graph_snapshots_project "
                "ON mcum_graph.snapshots (project_id, created_at DESC)"
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW mcum_graph.v_project_health AS
                WITH entity_counts AS (
                    SELECT project_id, COUNT(*) AS entity_count
                    FROM mcum_graph.entities
                    WHERE valid_to IS NULL
                    GROUP BY project_id
                ),
                relation_counts AS (
                    SELECT project_id, COUNT(*) AS relation_count
                    FROM mcum_graph.relations
                    WHERE valid_to IS NULL
                    GROUP BY project_id
                ),
                latest_snapshots AS (
                    SELECT project_id, MAX(created_at) AS latest_snapshot_at
                    FROM mcum_graph.snapshots
                    GROUP BY project_id
                )
                SELECT
                    p.id AS project_id,
                    p.project_name,
                    p.project_path,
                    COALESCE(e.entity_count, 0) AS entity_count,
                    COALESCE(r.relation_count, 0) AS relation_count,
                    s.latest_snapshot_at
                FROM project_registry.projects p
                LEFT JOIN entity_counts e ON e.project_id = p.id
                LEFT JOIN relation_counts r ON r.project_id = p.id
                LEFT JOIN latest_snapshots s ON s.project_id = p.id
                """
            )


def _require_unified_graph_schema() -> None:
    """Fail fast when the installed schema is unavailable, without executing DDL."""
    required = (
        "mcum_graph.entities",
        "mcum_graph.relations",
        "mcum_graph.snapshots",
        "mcum_graph.context_packs",
    )
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT ARRAY[
                    to_regclass('mcum_graph.entities')::text,
                    to_regclass('mcum_graph.relations')::text,
                    to_regclass('mcum_graph.snapshots')::text,
                    to_regclass('mcum_graph.context_packs')::text
                ] AS objects
                """
            )
            objects = list((cur.fetchone() or {}).get("objects") or [])
    missing = [name for name, found in zip(required, objects) if not found]
    if missing:
        raise RuntimeError(
            "unified_graph_schema_unavailable: run install_schema.py; missing="
            + ",".join(missing)
        )


def _reconcile_projection(cur: Any, project_id: str, *, include_code: bool) -> None:
    """Close source rows that disappeared without rewriting the full projection."""
    if include_code:
        cur.execute(
            """
            UPDATE mcum_graph.entities entity
            SET valid_to = NOW(), updated_at = NOW()
            WHERE entity.project_id = %s
              AND entity.valid_to IS NULL
              AND entity.source_schema = 'code_graph'
              AND (
                  (
                      entity.source_table = 'files'
                      AND NOT EXISTS (
                          SELECT 1 FROM code_graph.files source
                          WHERE source.project_id = %s
                            AND source.status = 'active'
                            AND source.id::text = entity.source_id
                      )
                  )
                  OR (
                      entity.source_table = 'nodes'
                      AND NOT EXISTS (
                          SELECT 1 FROM code_graph.nodes source
                          WHERE source.project_id = %s
                            AND source.id::text = entity.source_id
                      )
                  )
                  OR (
                      entity.source_table = 'edge_targets'
                      AND NOT EXISTS (
                          SELECT 1 FROM code_graph.edges source
                          WHERE source.project_id = %s
                            AND source.target_node_id IS NULL
                            AND source.target_ref = entity.source_id
                      )
                  )
              )
            """,
            (project_id, project_id, project_id, project_id),
        )
        cur.execute(
            """
            UPDATE mcum_graph.relations relation
            SET valid_to = NOW(), updated_at = NOW()
            WHERE relation.project_id = %s
              AND relation.valid_to IS NULL
              AND (
                  EXISTS (
                      SELECT 1
                      FROM mcum_graph.entities source
                      WHERE source.id = relation.source_entity_id AND source.valid_to IS NOT NULL
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM mcum_graph.entities target
                      WHERE target.id = relation.target_entity_id AND target.valid_to IS NOT NULL
                  )
                  OR (
                      relation.relation_type LIKE 'CODE_%%'
                      AND NOT EXISTS (
                          SELECT 1 FROM code_graph.edges source
                          WHERE source.project_id = %s
                            AND source.id::text = relation.evidence_ref->>'edge_id'
                      )
                  )
              )
            """,
            (project_id, project_id),
        )
    cur.execute(
        """
        UPDATE mcum_graph.entities entity
        SET valid_to = NOW(), updated_at = NOW()
        WHERE entity.project_id = %s
          AND entity.valid_to IS NULL
          AND (
              (
                  entity.source_schema = 'core_brain'
                  AND entity.source_table = 'experiences'
                  AND NOT EXISTS (
                      SELECT 1 FROM core_brain.experiences source
                      WHERE source.project_id = %s
                        AND source.superseded_by IS NULL
                        AND source.id::text = entity.source_id
                  )
              )
              OR (
                  entity.source_schema = 'project_registry'
                  AND entity.source_table = 'skill_catalog'
                  AND EXISTS (
                      SELECT 1 FROM project_registry.skill_catalog source
                      WHERE source.skill_name = entity.source_id
                        AND source.status IN ('deprecated', 'blocked')
                  )
              )
          )
        """,
        (project_id, project_id),
    )


def _code_projection_node_budget() -> int:
    """Max code-graph nodes to federate before skipping the code projection.

    A project whose code graph exceeds this budget is almost always the
    workspace root indexed as one project over many unrelated sibling folders
    (the anti-pattern SKILL.md forbids). Projecting tens of thousands of code
    nodes into the federated graph exceeds practical timeouts, so we skip the
    code projection for such graphs and keep only the memory/pattern/playbook
    projection. Scoped subprojects (small graphs) are unaffected. Set
    MCUM_GRAPH_MAX_CODE_PROJECTION_NODES=0 to disable the guard.
    """
    raw = os.getenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "15000")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 15000


def _should_project_code(cur: Any, project_id: str, code_graph_sync: dict[str, Any] | None) -> bool:
    budget = _code_projection_node_budget()
    if budget:
        cur.execute(
            "SELECT COUNT(*) AS n FROM code_graph.nodes WHERE project_id = %s",
            (project_id,),
        )
        node_count = int((dict(cur.fetchone() or {})).get("n") or 0)
        if node_count > budget:
            # Oversized merged-sibling graph: skip the heavy code projection so
            # the federated sync completes instead of timing out and going stale.
            return False
    if str((code_graph_sync or {}).get("status") or "") != "no_changes":
        return True
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM mcum_graph.entities
            WHERE project_id = %s
              AND source_schema = 'code_graph'
              AND valid_to IS NULL
        ) AS projected
        """,
        (project_id,),
    )
    row = dict(cur.fetchone() or {})
    return not bool(row.get("projected"))


def _upsert_entities(
    cur: Any,
    project_id: str,
    selected_skill: str | None,
    *,
    include_code: bool,
) -> None:
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            f.project_id, 'code_file', 'code_file:' || f.relative_path,
            'code_graph', 'files', f.id::text, f.relative_path,
            f.language || ' source file',
            f.file_hash, 1.0, 'extracted', f.status,
            jsonb_build_object(
                'relative_path', f.relative_path,
                'language', f.language,
                'bytes_size', f.bytes_size,
                'line_count', f.line_count,
                'token_estimate', f.token_estimate,
                'graph_id', f.graph_id
            ),
            NULL, NOW()
        FROM code_graph.files f
        WHERE f.project_id = %s AND f.status = 'active' AND %s
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        WHERE mcum_graph.entities.content_hash IS DISTINCT FROM EXCLUDED.content_hash
           OR mcum_graph.entities.health_state IS DISTINCT FROM EXCLUDED.health_state
           OR mcum_graph.entities.metadata IS DISTINCT FROM EXCLUDED.metadata
           OR mcum_graph.entities.valid_to IS NOT NULL
        """,
        (project_id, include_code),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT DISTINCT ON (e.project_id, e.target_ref)
            e.project_id, 'external_symbol', 'external_symbol:' || e.target_ref,
            'code_graph', 'edge_targets', e.target_ref, e.target_ref,
            'Unresolved external code reference',
            md5(e.target_ref), GREATEST(0.0, LEAST(1.0, e.confidence)),
            'extracted', 'active',
            jsonb_build_object(
                'target_ref', e.target_ref,
                'edge_kind', e.edge_kind,
                'unresolved', TRUE
            ),
            NULL::timestamptz, NOW()
        FROM code_graph.edges e
        WHERE e.project_id = %s
          AND e.target_node_id IS NULL
          AND COALESCE(e.target_ref, '') <> ''
          AND %s
        ORDER BY e.project_id, e.target_ref, e.confidence DESC
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            confidence = EXCLUDED.confidence,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        WHERE mcum_graph.entities.content_hash IS DISTINCT FROM EXCLUDED.content_hash
           OR mcum_graph.entities.confidence IS DISTINCT FROM EXCLUDED.confidence
           OR mcum_graph.entities.metadata IS DISTINCT FROM EXCLUDED.metadata
           OR mcum_graph.entities.valid_to IS NOT NULL
        """,
        (project_id, include_code),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            n.project_id, 'code_symbol',
            'code_symbol:' || f.relative_path || ':' || n.qualified_name || ':' || COALESCE(n.line_start::text, '0'),
            'code_graph', 'nodes', n.id::text, n.qualified_name,
            CONCAT(n.node_kind, ' ', n.qualified_name, ' @ ', f.relative_path, ':', COALESCE(n.line_start::text, '?')),
            md5(COALESCE(n.search_text, '') || COALESCE(n.signature, '')),
            1.0, 'extracted', 'active',
            jsonb_build_object(
                'relative_path', f.relative_path,
                'language', f.language,
                'node_kind', n.node_kind,
                'signature', n.signature,
                'line_start', n.line_start,
                'line_end', n.line_end,
                'file_id', n.file_id
            ),
            NULL, NOW()
        FROM code_graph.nodes n
        JOIN code_graph.files f ON f.id = n.file_id
        WHERE n.project_id = %s AND f.status = 'active' AND %s
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        WHERE mcum_graph.entities.content_hash IS DISTINCT FROM EXCLUDED.content_hash
           OR mcum_graph.entities.metadata IS DISTINCT FROM EXCLUDED.metadata
           OR mcum_graph.entities.valid_to IS NOT NULL
        """,
        (project_id, include_code),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            e.project_id, 'experience', 'experience:' || e.id::text,
            'core_brain', 'experiences', e.id::text, e.title,
            COALESCE(e.content->>'conclusion', e.task_description, e.title),
            md5(e.content::text || COALESCE(e.task_description, '')),
            COALESCE(e.current_confidence, 0.5), 'validated', 'active',
            jsonb_build_object(
                'category', e.category::text,
                'skill_name', e.skill_name,
                'task_description', e.task_description,
                'last_validated_at', e.last_validated_at
            ),
            NULL, NOW()
        FROM core_brain.experiences e
        WHERE e.project_id = %s AND e.superseded_by IS NULL
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            confidence = EXCLUDED.confidence,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id,),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            %s::uuid, 'pattern', 'pattern:' || p.id::text,
            'core_brain', 'patterns', p.id::text, p.name, p.description,
            md5(p.description || COALESCE(p.metadata::text, '')),
            GREATEST(0.0, LEAST(1.0, COALESCE(p.utility_score, p.avg_score, 0.5))),
            'validated', COALESCE(p.health_state, 'active'),
            jsonb_build_object(
                'category', p.category::text,
                'status', p.status::text,
                'scope_type', p.scope_type,
                'scope_project_id', p.scope_project_id,
                'scope_skill_name', p.scope_skill_name,
                'usage_count', p.usage_count
            ),
            NULL, NOW()
        FROM core_brain.patterns p
        WHERE p.status::text = 'active'
          AND COALESCE(p.health_state, 'active') <> 'degraded'
          AND (p.scope_project_id = %s OR p.scope_project_id IS NULL)
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            confidence = EXCLUDED.confidence,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id, project_id),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            %s::uuid, 'skill', 'skill:' || sc.skill_name,
            'project_registry', 'skill_catalog', sc.skill_name, sc.skill_name,
            COALESCE(sc.description, sc.skill_path),
            md5(COALESCE(sc.description, '') || COALESCE(sc.metadata::text, '')),
            GREATEST(0.0, LEAST(1.0, COALESCE(sc.avg_confidence, 0.5))),
            'validated', sc.status,
            jsonb_build_object(
                'skill_path', sc.skill_path,
                'experience_count', sc.experience_count,
                'active_test_count', sc.active_test_count,
                'project_count', sc.project_count
            ),
            NULL, NOW()
        FROM project_registry.skill_catalog sc
        WHERE sc.status NOT IN ('deprecated', 'blocked')
          AND (
              sc.skill_name = %s
              OR sc.skill_name IN (
                  SELECT DISTINCT e.skill_name
                  FROM core_brain.experiences e
                  WHERE e.project_id = %s AND e.superseded_by IS NULL
                  UNION
                  SELECT DISTINCT sp.skill_name
                  FROM core_brain.session_playbooks sp
                  WHERE sp.project_id = %s AND sp.outcome IN ('success', 'partial')
              )
          )
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            confidence = EXCLUDED.confidence,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id, selected_skill or "mcum-orchestrator", project_id, project_id),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            sp.project_id, 'playbook', 'playbook:' || sp.id::text,
            'core_brain', 'session_playbooks', sp.id::text, sp.title,
            COALESCE(sp.output_summary, sp.objective, sp.task_description),
            md5(
                COALESCE(sp.output_summary, '') || COALESCE(sp.validation_summary, '')
                || COALESCE(sp.files_touched::text, '')
            ),
            GREATEST(0.0, LEAST(1.0, COALESCE(sp.confidence_score, 0.5))),
            'validated', 'active',
            jsonb_build_object(
                'skill_name', sp.skill_name,
                'outcome', sp.outcome,
                'objective', sp.objective,
                'validation_summary', sp.validation_summary,
                'files_touched', sp.files_touched,
                'reuse_count', sp.reuse_count,
                'source_session_id', sp.source_session_id
            ),
            NULL, NOW()
        FROM core_brain.session_playbooks sp
        WHERE sp.project_id = %s AND sp.outcome IN ('success', 'partial')
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            confidence = EXCLUDED.confidence,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id,),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            s.project_id, 'spec_contract', 'spec:' || s.id::text,
            'project_registry', 'spec_contracts', s.id::text,
            s.objective, s.success_criteria,
            md5(s.contract_payload::text || s.status),
            1.0, 'validated', s.status,
            jsonb_build_object(
                'task_id', s.task_id,
                'task_type', s.task_type,
                'expected_deliverable', s.expected_deliverable,
                'execution_mode', s.execution_mode,
                'validation_required', s.validation_required
            ),
            NULL, NOW()
        FROM project_registry.spec_contracts s
        WHERE s.project_id = %s
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id,),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.entities (
            project_id, entity_type, canonical_key, source_schema, source_table,
            source_id, title, summary, content_hash, confidence, provenance_kind,
            health_state, metadata, valid_to, updated_at
        )
        SELECT
            p.id, 'design_system', 'design_system:' || v.id::text,
            'project_registry', 'design_system_versions', v.id::text,
            dsp.product_name || ' design system v' || v.version_number::text,
            COALESCE(dsp.source_summary, 'Approved project design system'),
            md5(v.design_tokens::text || v.component_guidelines::text || v.layout_system::text),
            1.0, 'validated', v.status,
            jsonb_build_object(
                'profile_id', dsp.id,
                'version_number', v.version_number,
                'design_tokens', v.design_tokens,
                'layout_system', v.layout_system,
                'component_guidelines', v.component_guidelines,
                'accessibility_guidelines', v.accessibility_guidelines
            ),
            NULL, NOW()
        FROM project_registry.projects p
        JOIN project_registry.design_system_profiles dsp ON dsp.project_id = p.id
        JOIN project_registry.design_system_versions v ON v.profile_id = dsp.id
        WHERE p.id = %s AND v.status = 'approved'
        ON CONFLICT (project_id, source_schema, source_table, source_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            content_hash = EXCLUDED.content_hash,
            health_state = EXCLUDED.health_state,
            metadata = EXCLUDED.metadata,
            valid_to = NULL,
            updated_at = NOW()
        """,
        (project_id,),
    )


def _upsert_relations(cur: Any, project_id: str, *, include_code: bool) -> None:
    cur.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT
                %s::uuid AS project_id,
                file_entity.id AS source_entity_id,
                symbol_entity.id AS target_entity_id,
                'DEFINES'::text AS relation_type,
                jsonb_build_object('file_id', n.file_id, 'node_id', n.id) AS evidence_ref
            FROM code_graph.nodes n
            JOIN mcum_graph.entities file_entity
              ON file_entity.project_id = %s
             AND file_entity.source_schema = 'code_graph'
             AND file_entity.source_table = 'files'
             AND file_entity.source_id = n.file_id::text
            JOIN mcum_graph.entities symbol_entity
              ON symbol_entity.project_id = %s
             AND symbol_entity.source_schema = 'code_graph'
             AND symbol_entity.source_table = 'nodes'
             AND symbol_entity.source_id = n.id::text
            WHERE n.project_id = %s AND %s
        ),
        changed AS (
            SELECT candidate.*
            FROM candidates candidate
            LEFT JOIN mcum_graph.relations existing
              ON existing.project_id = candidate.project_id
             AND existing.source_entity_id = candidate.source_entity_id
             AND existing.target_entity_id = candidate.target_entity_id
             AND existing.relation_type = candidate.relation_type
            WHERE existing.id IS NULL
               OR existing.evidence_ref IS DISTINCT FROM candidate.evidence_ref
               OR existing.valid_to IS NOT NULL
        )
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            project_id, source_entity_id, target_entity_id, relation_type,
            1.0, 'extracted', evidence_ref, NOW()
        FROM changed
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET evidence_ref = EXCLUDED.evidence_ref, updated_at = NOW(), valid_to = NULL
        WHERE mcum_graph.relations.evidence_ref IS DISTINCT FROM EXCLUDED.evidence_ref
           OR mcum_graph.relations.valid_to IS NOT NULL
        """,
        (project_id, project_id, project_id, project_id, include_code),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            %s::uuid, playbook_entity.id, skill_entity.id, 'USES_SKILL',
            GREATEST(0.0, LEAST(1.0, COALESCE(sp.confidence_score, 0.5))), 'validated',
            jsonb_build_object('skill_name', sp.skill_name, 'outcome', sp.outcome),
            NOW()
        FROM core_brain.session_playbooks sp
        JOIN mcum_graph.entities playbook_entity
          ON playbook_entity.project_id = %s
         AND playbook_entity.source_schema = 'core_brain'
         AND playbook_entity.source_table = 'session_playbooks'
         AND playbook_entity.source_id = sp.id::text
        JOIN mcum_graph.entities skill_entity
          ON skill_entity.project_id = %s
         AND skill_entity.source_schema = 'project_registry'
         AND skill_entity.source_table = 'skill_catalog'
         AND skill_entity.source_id = sp.skill_name
        WHERE sp.project_id = %s AND sp.outcome IN ('success', 'partial')
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        WHERE mcum_graph.relations.confidence IS DISTINCT FROM EXCLUDED.confidence
           OR mcum_graph.relations.evidence_ref IS DISTINCT FROM EXCLUDED.evidence_ref
           OR mcum_graph.relations.valid_to IS NOT NULL
        """,
        (project_id, project_id, project_id, project_id),
    )
    cur.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT DISTINCT ON (source_entity.id, target_entity.id, e.edge_kind)
                %s::uuid AS project_id,
                source_entity.id AS source_entity_id,
                target_entity.id AS target_entity_id,
                'CODE_' || UPPER(REPLACE(e.edge_kind, ' ', '_')) AS relation_type,
                GREATEST(0.0, LEAST(1.0, e.confidence)) AS confidence,
                jsonb_build_object(
                    'edge_id', e.id,
                    'source_ref', e.source_ref,
                    'target_ref', e.target_ref,
                    'edge_kind', e.edge_kind
                ) AS evidence_ref
            FROM code_graph.edges e
            JOIN mcum_graph.entities source_entity
              ON source_entity.project_id = %s
             AND source_entity.source_schema = 'code_graph'
             AND source_entity.source_table = 'nodes'
             AND source_entity.source_id = e.source_node_id::text
            JOIN mcum_graph.entities target_entity
              ON target_entity.project_id = %s
             AND target_entity.source_schema = 'code_graph'
             AND target_entity.source_table = 'nodes'
             AND target_entity.source_id = e.target_node_id::text
            WHERE e.project_id = %s
              AND e.source_node_id IS NOT NULL
              AND e.target_node_id IS NOT NULL
              AND %s
            ORDER BY source_entity.id, target_entity.id, e.edge_kind, e.confidence DESC
        ),
        changed AS (
            SELECT candidate.*
            FROM candidates candidate
            LEFT JOIN mcum_graph.relations existing
              ON existing.project_id = candidate.project_id
             AND existing.source_entity_id = candidate.source_entity_id
             AND existing.target_entity_id = candidate.target_entity_id
             AND existing.relation_type = candidate.relation_type
            WHERE existing.id IS NULL
               OR existing.confidence IS DISTINCT FROM candidate.confidence
               OR existing.evidence_ref IS DISTINCT FROM candidate.evidence_ref
               OR existing.valid_to IS NOT NULL
        )
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, 'extracted', evidence_ref, NOW()
        FROM changed
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        WHERE mcum_graph.relations.confidence IS DISTINCT FROM EXCLUDED.confidence
           OR mcum_graph.relations.evidence_ref IS DISTINCT FROM EXCLUDED.evidence_ref
           OR mcum_graph.relations.valid_to IS NOT NULL
        """,
        (project_id, project_id, project_id, project_id, include_code),
    )
    cur.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT DISTINCT ON (source_entity.id, target_entity.id, e.edge_kind)
                %s::uuid AS project_id,
                source_entity.id AS source_entity_id,
                target_entity.id AS target_entity_id,
                'CODE_' || UPPER(REPLACE(e.edge_kind, ' ', '_')) AS relation_type,
                GREATEST(0.0, LEAST(1.0, e.confidence)) AS confidence,
                jsonb_build_object(
                    'edge_id', e.id,
                    'source_ref', e.source_ref,
                    'target_ref', e.target_ref,
                    'edge_kind', e.edge_kind
                ) AS evidence_ref
            FROM code_graph.edges e
            JOIN mcum_graph.entities source_entity
              ON source_entity.project_id = %s
             AND source_entity.source_schema = 'code_graph'
             AND source_entity.source_table = 'nodes'
             AND source_entity.source_id = e.source_node_id::text
            JOIN mcum_graph.entities target_entity
              ON target_entity.project_id = %s
             AND target_entity.source_schema = 'code_graph'
             AND target_entity.source_table = 'edge_targets'
             AND target_entity.source_id = e.target_ref
            WHERE e.project_id = %s
              AND e.source_node_id IS NOT NULL
              AND e.target_node_id IS NULL
              AND %s
            ORDER BY source_entity.id, target_entity.id, e.edge_kind, e.confidence DESC
        ),
        changed AS (
            SELECT candidate.*
            FROM candidates candidate
            LEFT JOIN mcum_graph.relations existing
              ON existing.project_id = candidate.project_id
             AND existing.source_entity_id = candidate.source_entity_id
             AND existing.target_entity_id = candidate.target_entity_id
             AND existing.relation_type = candidate.relation_type
            WHERE existing.id IS NULL
               OR existing.confidence IS DISTINCT FROM candidate.confidence
               OR existing.evidence_ref IS DISTINCT FROM candidate.evidence_ref
               OR existing.valid_to IS NOT NULL
        )
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, 'extracted', evidence_ref, NOW()
        FROM changed
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        WHERE mcum_graph.relations.confidence IS DISTINCT FROM EXCLUDED.confidence
           OR mcum_graph.relations.evidence_ref IS DISTINCT FROM EXCLUDED.evidence_ref
           OR mcum_graph.relations.valid_to IS NOT NULL
        """,
        (project_id, project_id, project_id, project_id, include_code),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            %s::uuid, experience_entity.id, target_entity.id, 'APPLIES_TO',
            l.confidence, 'validated',
            jsonb_build_object(
                'relative_path', l.relative_path,
                'qualified_name', l.qualified_name,
                'link_kind', l.link_kind
            ),
            NOW()
        FROM code_graph.experience_links l
        JOIN mcum_graph.entities experience_entity
          ON experience_entity.project_id = %s
         AND experience_entity.source_schema = 'core_brain'
         AND experience_entity.source_table = 'experiences'
         AND experience_entity.source_id = l.experience_id::text
        JOIN mcum_graph.entities target_entity
          ON target_entity.project_id = %s
         AND target_entity.source_schema = 'code_graph'
         AND (
              (l.node_id IS NOT NULL AND target_entity.source_table = 'nodes' AND target_entity.source_id = l.node_id::text)
              OR (l.node_id IS NULL AND l.file_id IS NOT NULL AND target_entity.source_table = 'files' AND target_entity.source_id = l.file_id::text)
         )
        WHERE l.project_id = %s
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        """,
        (project_id, project_id, project_id, project_id),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            %s::uuid, experience_entity.id, skill_entity.id, 'USES_SKILL',
            COALESCE(e.current_confidence, 0.5), 'validated',
            jsonb_build_object('skill_name', e.skill_name),
            NOW()
        FROM core_brain.experiences e
        JOIN mcum_graph.entities experience_entity
          ON experience_entity.project_id = %s
         AND experience_entity.source_schema = 'core_brain'
         AND experience_entity.source_table = 'experiences'
         AND experience_entity.source_id = e.id::text
        JOIN mcum_graph.entities skill_entity
          ON skill_entity.project_id = %s
         AND skill_entity.source_schema = 'project_registry'
         AND skill_entity.source_table = 'skill_catalog'
         AND skill_entity.source_id = e.skill_name
        WHERE e.project_id = %s AND e.superseded_by IS NULL
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        """,
        (project_id, project_id, project_id, project_id),
    )
    cur.execute(
        """
        INSERT INTO mcum_graph.relations (
            project_id, source_entity_id, target_entity_id, relation_type,
            confidence, provenance_kind, evidence_ref, updated_at
        )
        SELECT
            %s::uuid, pattern_entity.id, experience_entity.id, 'LEARNED_FROM',
            GREATEST(0.0, LEAST(1.0, COALESCE(pe.weight, 1.0))), pe.source,
            jsonb_build_object('evidence_role', pe.evidence_role, 'similarity', pe.similarity),
            NOW()
        FROM core_brain.pattern_evidence pe
        JOIN core_brain.experiences e ON e.id = pe.experience_id
        JOIN mcum_graph.entities pattern_entity
          ON pattern_entity.project_id = %s
         AND pattern_entity.source_schema = 'core_brain'
         AND pattern_entity.source_table = 'patterns'
         AND pattern_entity.source_id = pe.pattern_id::text
        JOIN mcum_graph.entities experience_entity
          ON experience_entity.project_id = %s
         AND experience_entity.source_schema = 'core_brain'
         AND experience_entity.source_table = 'experiences'
         AND experience_entity.source_id = pe.experience_id::text
        WHERE e.project_id = %s
        ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)
        DO UPDATE SET confidence = EXCLUDED.confidence, evidence_ref = EXCLUDED.evidence_ref,
                      updated_at = NOW(), valid_to = NULL
        """,
        (project_id, project_id, project_id, project_id),
    )


def sync_unified_project_graph(
    *,
    project_id: str,
    trigger: str,
    selected_skill: str | None = None,
    code_graph_sync: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _valid_uuid(project_id):
        return {"status": "invalid_project_id", "trigger": trigger}
    started = time.perf_counter()
    try:
        _require_unified_graph_schema()
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute("SET LOCAL lock_timeout = '5s'")
                # Configurable so large per-project graphs do not fail the
                # federated projection prematurely. Defaults to 90s.
                _stmt_timeout_ms = os.getenv("MCUM_GRAPH_STATEMENT_TIMEOUT_MS", "90000")
                try:
                    _stmt_timeout_ms = str(max(10000, int(_stmt_timeout_ms)))
                except (TypeError, ValueError):
                    _stmt_timeout_ms = "90000"
                cur.execute(f"SET LOCAL statement_timeout = '{_stmt_timeout_ms}'")
                include_code = _should_project_code(cur, project_id, code_graph_sync)
                _reconcile_projection(cur, project_id, include_code=include_code)
                _upsert_entities(
                    cur,
                    project_id,
                    selected_skill,
                    include_code=include_code,
                )
                _upsert_relations(cur, project_id, include_code=include_code)
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE valid_to IS NULL) AS entities,
                        COUNT(*) FILTER (WHERE health_state = 'active' AND valid_to IS NULL) AS active_entities
                    FROM mcum_graph.entities
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                entity_counts = dict(cur.fetchone() or {})
                cur.execute(
                    "SELECT COUNT(*) AS relations FROM mcum_graph.relations "
                    "WHERE project_id = %s AND valid_to IS NULL",
                    (project_id,),
                )
                relation_counts = dict(cur.fetchone() or {})
                cur.execute(
                    "SELECT graph_version, source_hash FROM code_graph.graphs WHERE project_id = %s LIMIT 1",
                    (project_id,),
                )
                graph = dict(cur.fetchone() or {})
                source_hash = hashlib.sha256(
                    _json(
                        {
                            "graph": graph,
                            "entities": entity_counts,
                            "relations": relation_counts,
                            "selected_skill": selected_skill,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                cur.execute(
                    """
                    INSERT INTO mcum_graph.snapshots (
                        project_id, trigger, status, code_graph_version,
                        entity_count, relation_count, source_hash, metadata
                    ) VALUES (%s, %s, 'active', %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (
                        project_id,
                        trigger,
                        graph.get("graph_version"),
                        int(entity_counts.get("entities") or 0),
                        int(relation_counts.get("relations") or 0),
                        source_hash,
                        _json(
                            {
                                **dict(metadata or {}),
                                "code_graph_sync": _compact_code_graph_sync(code_graph_sync),
                                "selected_skill": selected_skill,
                                "code_projection_refreshed": include_code,
                            }
                        ),
                    ),
                )
                snapshot = dict(cur.fetchone() or {})
        return {
            "status": "success",
            "trigger": trigger,
            "snapshot_id": str(snapshot.get("id") or ""),
            "snapshot_created_at": snapshot.get("created_at"),
            "code_graph_version": graph.get("graph_version"),
            "entities": int(entity_counts.get("entities") or 0),
            "active_entities": int(entity_counts.get("active_entities") or 0),
            "relations": int(relation_counts.get("relations") or 0),
            "source_hash": source_hash,
            "code_projection_refreshed": include_code,
            "wall_clock_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "failure",
            "trigger": trigger,
            "error": str(exc),
            "wall_clock_ms": int((time.perf_counter() - started) * 1000),
        }


def get_unified_graph_health(*, project_id: str, ensure_schema: bool = True) -> dict[str, Any]:
    if not _valid_uuid(project_id):
        return {"available": False, "status": "invalid_project_id"}
    if ensure_schema:
        _require_unified_graph_schema()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, trigger, status, code_graph_version, entity_count,
                       relation_count, source_hash, metadata, created_at
                FROM mcum_graph.snapshots
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id,),
            )
            snapshot = cur.fetchone()
            cur.execute(
                """
                SELECT entity_type, COUNT(*) AS count
                FROM mcum_graph.entities
                WHERE project_id = %s AND valid_to IS NULL
                GROUP BY entity_type
                ORDER BY entity_type
                """,
                (project_id,),
            )
            by_type = {row["entity_type"]: int(row["count"]) for row in cur.fetchall()}
            cur.execute(
                """
                SELECT graph_version
                FROM code_graph.graphs
                WHERE project_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (project_id,),
            )
            graph = dict(cur.fetchone() or {})
    snapshot_dict = dict(snapshot) if snapshot else None
    snapshot_version = (snapshot_dict or {}).get("code_graph_version")
    current_version = graph.get("graph_version")
    version_aligned = bool(
        snapshot_dict
        and snapshot_version is not None
        and current_version is not None
        and int(snapshot_version) == int(current_version)
    )
    status = "not_projected"
    if snapshot_dict:
        status = "active" if version_aligned else "stale"
    return {
        "available": True,
        "status": status,
        "latest_snapshot": snapshot_dict,
        "current_code_graph_version": current_version,
        "version_aligned": version_aligned,
        "entities_by_type": by_type,
    }


def query_unified_graph(
    *,
    project_id: str,
    query: str,
    limit: int = 12,
    entity_types: list[str] | None = None,
) -> dict[str, Any]:
    if not _valid_uuid(project_id):
        return {"status": "invalid_project_id", "entities": [], "relations": []}
    _require_unified_graph_schema()
    types = [str(item).strip() for item in entity_types or [] if str(item).strip()]
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                WITH q AS (
                    SELECT websearch_to_tsquery(
                               'simple',
                               regexp_replace(COALESCE(NULLIF(%s, ''), 'project'), '\\s+', ' OR ', 'g')
                           ) AS tsq,
                           LOWER(COALESCE(NULLIF(%s, ''), 'project')) AS raw
                )
                SELECT
                    e.*,
                    (
                        ts_rank_cd(to_tsvector('simple', e.title || ' ' || COALESCE(e.summary, '')), q.tsq)
                        + CASE WHEN LOWER(e.title) = q.raw THEN 1.0 ELSE 0.0 END
                        + CASE WHEN LOWER(e.canonical_key) LIKE '%%' || q.raw || '%%' THEN 0.5 ELSE 0.0 END
                        + (e.confidence * 0.1)
                    )::float AS score
                FROM mcum_graph.entities e
                CROSS JOIN q
                WHERE e.project_id = %s
                  AND e.valid_to IS NULL
                  AND e.health_state NOT IN ('deprecated', 'degraded', 'blocked')
                  AND (%s::text[] IS NULL OR cardinality(%s::text[]) = 0 OR e.entity_type = ANY(%s::text[]))
                  AND (
                      to_tsvector('simple', e.title || ' ' || COALESCE(e.summary, '')) @@ q.tsq
                      OR LOWER(e.canonical_key) LIKE '%%' || q.raw || '%%'
                  )
                ORDER BY score DESC, e.updated_at DESC
                LIMIT %s
                """,
                (query, query, project_id, types or None, types or None, types or None, max(1, min(limit, 50))),
            )
            entities = [dict(row) for row in cur.fetchall()]
            entity_ids = [str(item["id"]) for item in entities]
            relations: list[dict[str, Any]] = []
            if entity_ids:
                cur.execute(
                    """
                    SELECT r.*, src.title AS source_title, dst.title AS target_title
                    FROM mcum_graph.relations r
                    JOIN mcum_graph.entities src ON src.id = r.source_entity_id
                    JOIN mcum_graph.entities dst ON dst.id = r.target_entity_id
                    WHERE r.project_id = %s
                      AND r.valid_to IS NULL
                      AND (r.source_entity_id = ANY(%s::uuid[]) OR r.target_entity_id = ANY(%s::uuid[]))
                    ORDER BY r.confidence DESC, r.relation_type
                    LIMIT %s
                    """,
                    (project_id, entity_ids, entity_ids, max(1, min(limit * 3, 100))),
                )
                relations = [dict(row) for row in cur.fetchall()]
    return {"status": "success", "entities": entities, "relations": relations}


def find_unified_graph_path(
    *,
    project_id: str,
    source_entity_id: str,
    target_entity_id: str,
    max_depth: int = 4,
) -> dict[str, Any]:
    if not all(_valid_uuid(value) for value in (project_id, source_entity_id, target_entity_id)):
        return {"status": "invalid_id", "path": []}
    _require_unified_graph_schema()
    depth = max(1, min(int(max_depth or 4), 8))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                WITH RECURSIVE walk AS (
                    SELECT
                        r.source_entity_id,
                        r.target_entity_id,
                        ARRAY[r.source_entity_id, r.target_entity_id]::uuid[] AS entity_path,
                        ARRAY[r.id]::uuid[] AS relation_path,
                        1 AS depth
                    FROM mcum_graph.relations r
                    WHERE r.project_id = %s
                      AND r.valid_to IS NULL
                      AND r.source_entity_id = %s
                    UNION ALL
                    SELECT
                        w.source_entity_id,
                        r.target_entity_id,
                        w.entity_path || r.target_entity_id,
                        w.relation_path || r.id,
                        w.depth + 1
                    FROM walk w
                    JOIN mcum_graph.relations r
                      ON r.project_id = %s
                     AND r.source_entity_id = w.target_entity_id
                     AND r.valid_to IS NULL
                    WHERE w.depth < %s
                      AND NOT r.target_entity_id = ANY(w.entity_path)
                )
                SELECT entity_path, relation_path, depth
                FROM walk
                WHERE target_entity_id = %s
                ORDER BY depth
                LIMIT 1
                """,
                (project_id, source_entity_id, project_id, depth, target_entity_id),
            )
            row = cur.fetchone()
    return {"status": "success" if row else "not_found", "path": dict(row) if row else {}}


def persist_context_pack(
    *,
    project_id: str,
    session_id: str,
    agent_role: str,
    task_query: str,
    envelope: dict[str, Any],
    token_budget: int,
    token_estimate: int,
    snapshot_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    if not _valid_uuid(project_id):
        return None
    _require_unified_graph_schema()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO mcum_graph.context_packs (
                    project_id, snapshot_id, session_id, agent_role, task_query,
                    token_budget, token_estimate, envelope, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    project_id,
                    snapshot_id if _valid_uuid(snapshot_id) else None,
                    session_id,
                    agent_role,
                    task_query,
                    max(0, int(token_budget or 0)),
                    max(0, int(token_estimate or 0)),
                    _json(envelope),
                    _json(metadata),
                ),
            )
            row = cur.fetchone()
    return str((row or {}).get("id") or "") or None
