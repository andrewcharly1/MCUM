"""Persistence and read adapters for governed MCUM graph intelligence."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping
import uuid

from ..core.graph_query_service import GraphQueryService, PostgreSQLGraphBackend
from .connection import get_cursor, get_db


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _query_one(sql: str, params: Iterable[Any]) -> Mapping[str, Any] | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
    return dict(row) if row else None


def _query_all(sql: str, params: Iterable[Any]) -> list[Mapping[str, Any]]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [dict(row) for row in rows]


def get_graph_query_service(*, policy: Any = None) -> GraphQueryService:
    backend = PostgreSQLGraphBackend(query_one=_query_one, query_all=_query_all)
    return GraphQueryService(backend, policy=policy)


def load_project_graph(
    *,
    project_id: str,
    max_nodes: int = 10_000,
    max_edges: int = 30_000,
    entity_types: list[str] | None = None,
) -> dict[str, Any]:
    """Load one bounded project snapshot for pure analytics and exports."""
    if not _valid_uuid(project_id):
        raise ValueError("project_id must be a UUID")
    types = [str(item).strip() for item in entity_types or [] if str(item).strip()]
    node_limit = max(1, min(int(max_nodes or 10_000), 100_000))
    edge_limit = max(1, min(int(max_edges or 30_000), 300_000))
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, project_id, entity_type, canonical_key, source_schema,
                       source_table, source_id, title, summary, content_hash,
                       confidence, provenance_kind, health_state, metadata,
                       created_at, updated_at
                FROM mcum_graph.entities
                WHERE project_id = %s
                  AND valid_to IS NULL
                  AND health_state NOT IN ('deprecated', 'blocked')
                  AND (%s::text[] IS NULL OR cardinality(%s::text[]) = 0 OR entity_type = ANY(%s::text[]))
                ORDER BY canonical_key, id
                LIMIT %s
                """,
                (project_id, types or None, types or None, types or None, node_limit),
            )
            nodes = [dict(row) for row in cur.fetchall()]
            node_ids = [str(row["id"]) for row in nodes]
            edges: list[dict[str, Any]] = []
            if node_ids:
                cur.execute(
                    """
                    SELECT id, project_id, source_entity_id, target_entity_id,
                           relation_type, weight, confidence, provenance_kind,
                           evidence_ref, metadata, created_at, updated_at
                    FROM mcum_graph.relations
                    WHERE project_id = %s
                      AND valid_to IS NULL
                      AND source_entity_id = ANY(%s::uuid[])
                      AND target_entity_id = ANY(%s::uuid[])
                    ORDER BY source_entity_id, target_entity_id, relation_type, id
                    LIMIT %s
                    """,
                    (project_id, node_ids, node_ids, edge_limit),
                )
                edges = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id, project_id, trigger, status, code_graph_version,
                       entity_count, relation_count, source_hash, metadata, created_at
                FROM mcum_graph.snapshots
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id,),
            )
            snapshot = dict(cur.fetchone() or {})
    return {
        "project_id": project_id,
        "snapshot_id": str(snapshot.get("id") or ""),
        "snapshot": snapshot,
        "nodes": nodes,
        "edges": edges,
        "truncated": len(nodes) >= node_limit or len(edges) >= edge_limit,
    }


def persist_analytics_result(result: Mapping[str, Any]) -> dict[str, Any]:
    project_id = str(result.get("project_id") or "")
    if not _valid_uuid(project_id):
        raise ValueError("analytics result requires a valid project_id")
    snapshot_id = str(result.get("snapshot_id") or "")
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO mcum_graph.analytics_runs (
                    project_id, snapshot_id, analysis_type, algorithm,
                    algorithm_version, parameters, status, metrics
                ) VALUES (%s, %s, 'graph_structure', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    project_id,
                    snapshot_id if _valid_uuid(snapshot_id) else None,
                    str(result.get("algorithm") or "unknown"),
                    str(result.get("algorithm_version") or "unknown"),
                    _json(result.get("parameters") or {}),
                    str(result.get("status") or "success"),
                    _json(result.get("metrics") or {}),
                ),
            )
            run_id = str(cur.fetchone()["id"])
            community_ids: dict[str, str] = {}
            for community in result.get("communities") or []:
                cur.execute(
                    """
                    INSERT INTO mcum_graph.communities (
                        project_id, snapshot_id, run_id, community_key, label,
                        member_count, modularity, conductance, cohesion, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        project_id,
                        snapshot_id if _valid_uuid(snapshot_id) else None,
                        run_id,
                        str(community.get("community_key") or ""),
                        str(community.get("label") or ""),
                        int(community.get("member_count") or 0),
                        community.get("modularity"),
                        community.get("conductance"),
                        community.get("cohesion"),
                        _json({"representative_id": community.get("representative_id")}),
                    ),
                )
                community_id = str(cur.fetchone()["id"])
                community_ids[str(community.get("community_key") or "")] = community_id
                for member in community.get("members") or []:
                    entity_id = str(member.get("entity_id") or "")
                    if not _valid_uuid(entity_id):
                        continue
                    cur.execute(
                        """
                        INSERT INTO mcum_graph.entity_communities (
                            community_id, entity_id, membership_strength, is_representative
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (community_id, entity_id) DO UPDATE
                        SET membership_strength = EXCLUDED.membership_strength,
                            is_representative = EXCLUDED.is_representative
                        """,
                        (
                            community_id,
                            entity_id,
                            float(member.get("membership_strength") or 0),
                            bool(member.get("is_representative", False)),
                        ),
                    )
            for metric in result.get("entity_metrics") or []:
                entity_id = str(metric.get("entity_id") or "")
                if not _valid_uuid(entity_id):
                    continue
                cur.execute(
                    """
                    INSERT INTO mcum_graph.entity_metrics (
                        project_id, snapshot_id, entity_id, degree_in, degree_out,
                        pagerank, betweenness, k_core, hub_score, god_node_score,
                        surprise_score, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, snapshot_id, entity_id) DO UPDATE
                    SET degree_in = EXCLUDED.degree_in,
                        degree_out = EXCLUDED.degree_out,
                        pagerank = EXCLUDED.pagerank,
                        betweenness = EXCLUDED.betweenness,
                        k_core = EXCLUDED.k_core,
                        hub_score = EXCLUDED.hub_score,
                        god_node_score = EXCLUDED.god_node_score,
                        surprise_score = EXCLUDED.surprise_score,
                        metadata = EXCLUDED.metadata,
                        created_at = NOW()
                    """,
                    (
                        project_id,
                        snapshot_id if _valid_uuid(snapshot_id) else None,
                        entity_id,
                        int(metric.get("degree_in") or 0),
                        int(metric.get("degree_out") or 0),
                        float(metric.get("pagerank") or 0),
                        float(metric.get("betweenness") or 0),
                        int(metric.get("k_core") or 0),
                        float(metric.get("hub_score") or 0),
                        float(metric.get("god_node_score") or 0),
                        float(metric.get("surprise_score") or 0),
                        _json({"community_key": metric.get("community_key")}),
                    ),
                )
            for item in result.get("surprising_connections") or []:
                source_id = str(item.get("source_entity_id") or "")
                target_id = str(item.get("target_entity_id") or "")
                if not (_valid_uuid(source_id) and _valid_uuid(target_id)):
                    continue
                cur.execute(
                    """
                    INSERT INTO mcum_graph.surprising_connections (
                        project_id, snapshot_id, source_entity_id, target_entity_id,
                        surprise_kind, score, confidence, explanation, evidence, review_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, snapshot_id, source_entity_id, target_entity_id, surprise_kind)
                    DO UPDATE SET score = EXCLUDED.score, confidence = EXCLUDED.confidence,
                                  explanation = EXCLUDED.explanation, evidence = EXCLUDED.evidence
                    """,
                    (
                        project_id,
                        snapshot_id if _valid_uuid(snapshot_id) else None,
                        source_id,
                        target_id,
                        str(item.get("surprise_kind") or "unknown"),
                        float(item.get("score") or 0),
                        float(item.get("confidence") or 0),
                        str(item.get("explanation") or ""),
                        _json(item.get("evidence") or {}),
                        str(item.get("review_status") or "unreviewed"),
                    ),
                )
    return {
        "status": "success",
        "run_id": run_id,
        "communities": len(community_ids),
        "metrics": len(result.get("entity_metrics") or []),
        "surprising_connections": len(result.get("surprising_connections") or []),
    }


def persist_impact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    project_id = str(result.get("project_id") or "")
    if not _valid_uuid(project_id):
        raise ValueError("impact result requires a valid project_id")
    snapshot_id = str(result.get("snapshot_id") or "")
    changed = dict(result.get("changed") or {})
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO mcum_graph.impact_runs (
                    project_id, snapshot_id, changed_paths, changed_entities,
                    status, max_depth, metrics
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    project_id,
                    snapshot_id if _valid_uuid(snapshot_id) else None,
                    _json(changed.get("paths") or []),
                    _json(changed.get("entities") or []),
                    str(result.get("status") or "success"),
                    int(result.get("max_depth") or 0),
                    _json(result.get("metrics") or {}),
                ),
            )
            run_id = str(cur.fetchone()["id"])
            impact_count = 0
            for item in result.get("impact_items") or []:
                entity_id = str(item.get("entity_id") or "")
                cur.execute(
                    """
                    INSERT INTO mcum_graph.impact_items (
                        impact_run_id, entity_id, impact_kind, distance,
                        risk_score, reason, evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        entity_id if _valid_uuid(entity_id) else None,
                        str(item.get("impact_kind") or "dependency"),
                        int(item.get("distance") or 0),
                        float(item.get("risk_score") or 0),
                        str(item.get("reason") or ""),
                        _json(item.get("evidence") or {}),
                    ),
                )
                impact_count += 1
            test_count = 0
            selection = dict(result.get("test_selection") or {})
            for test in selection.get("tests") or []:
                test_id = str(test.get("test_entity_id") or "")
                cur.execute(
                    """
                    INSERT INTO mcum_graph.test_selections (
                        impact_run_id, test_entity_id, test_ref, selection_rank,
                        coverage_score, historical_failure_score, required, reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        test_id if _valid_uuid(test_id) else None,
                        test_id or str(test.get("relative_path") or ""),
                        int(test.get("selection_rank") or 0),
                        float(test.get("coverage_score") or 0),
                        float(test.get("historical_failure_score") or 0),
                        bool(test.get("required", False)),
                        str(test.get("reason") or ""),
                    ),
                )
                test_count += 1
    return {"status": "success", "run_id": run_id, "impact_items": impact_count, "tests": test_count}


def persist_comparison_result(result: Mapping[str, Any]) -> dict[str, Any]:
    scope = dict(result.get("comparison_scope") or {})
    left_project_id = str(result.get("left_project_id") or scope.get("left_project_id") or "")
    right_project_id = str(result.get("right_project_id") or scope.get("right_project_id") or "")
    if not (_valid_uuid(left_project_id) and _valid_uuid(right_project_id)):
        raise ValueError("comparison result requires valid project IDs")
    left_snapshot_id = str(scope.get("left_snapshot_id") or "")
    right_snapshot_id = str(scope.get("right_snapshot_id") or "")
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO mcum_graph.comparisons (
                    left_project_id, right_project_id, left_snapshot_id,
                    right_snapshot_id, comparison_mode, status, metrics
                ) VALUES (%s, %s, %s, %s, 'explicit', %s, %s)
                RETURNING id
                """,
                (
                    left_project_id,
                    right_project_id,
                    left_snapshot_id if _valid_uuid(left_snapshot_id) else None,
                    right_snapshot_id if _valid_uuid(right_snapshot_id) else None,
                    str(result.get("status") or "success"),
                    _json(result.get("metrics") or {}),
                ),
            )
            comparison_id = str(cur.fetchone()["id"])
            items: list[dict[str, Any]] = []
            matches = dict(result.get("matches") or {})
            for kind in ("exact", "probable", "ambiguous"):
                for item in matches.get(kind) or []:
                    items.append({"item_kind": f"match_{kind}", "severity": "info", **item})
            entities = dict(result.get("entities") or {})
            for kind in ("added", "removed", "changed"):
                for item in entities.get(kind) or []:
                    items.append(
                        {
                            "item_kind": f"entity_{kind}",
                            "severity": item.get("severity") or ("high" if kind == "changed" else "medium"),
                            **item,
                        }
                    )
            persisted = 0
            for item in items:
                left_id = str(item.get("left_entity_id") or "")
                right_id = str(item.get("right_entity_id") or "")
                cur.execute(
                    """
                    INSERT INTO mcum_graph.comparison_items (
                        comparison_id, item_kind, left_entity_id, right_entity_id,
                        similarity, severity, summary, evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        comparison_id,
                        str(item.get("item_kind") or "comparison_item"),
                        left_id if _valid_uuid(left_id) else None,
                        right_id if _valid_uuid(right_id) else None,
                        float(item.get("similarity") or 0),
                        str(item.get("severity") or "info"),
                        str(item.get("summary") or item.get("title") or ""),
                        _json(item.get("evidence") or item),
                    ),
                )
                persisted += 1
    return {"status": "success", "comparison_id": comparison_id, "items": persisted}


def persist_artifact_result(*, project_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Persist references and excerpts only; never persist binary content."""
    if not _valid_uuid(project_id):
        raise ValueError("artifact result requires a valid project_id")
    source_path = str(result.get("source_path") or "").strip()
    content_hash = str(result.get("content_hash") or "").strip()
    metadata = dict(result.get("metadata") or {})
    if not source_path or not content_hash:
        raise ValueError("artifact result requires source_path and content_hash")
    artifact_kind = str(metadata.get("artifact_kind") or "document")
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO mcum_graph.source_artifacts (
                    project_id, relative_path, artifact_kind, mime_type, content_hash,
                    bytes_size, extractor_version, status, metadata, indexed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, NOW())
                ON CONFLICT (project_id, relative_path) DO UPDATE
                SET artifact_kind = EXCLUDED.artifact_kind,
                    mime_type = EXCLUDED.mime_type,
                    content_hash = EXCLUDED.content_hash,
                    bytes_size = EXCLUDED.bytes_size,
                    extractor_version = EXCLUDED.extractor_version,
                    status = 'active',
                    metadata = EXCLUDED.metadata,
                    indexed_at = NOW()
                RETURNING id
                """,
                (
                    project_id,
                    source_path,
                    artifact_kind,
                    metadata.get("mime_type"),
                    content_hash,
                    int(metadata.get("bytes") or 0),
                    str(metadata.get("extractor_version") or "artifact-v1"),
                    _json(metadata),
                ),
            )
            artifact_id = str(cur.fetchone()["id"])
            cur.execute("DELETE FROM mcum_graph.artifact_sections WHERE artifact_id = %s", (artifact_id,))
            artifact_canonical = f"artifact:{source_path}"
            cur.execute(
                """
                INSERT INTO mcum_graph.entities (
                    project_id, entity_type, canonical_key, source_schema, source_table,
                    source_id, title, summary, content_hash, confidence,
                    provenance_kind, health_state, metadata, valid_to, updated_at
                ) VALUES (%s, 'source_artifact', %s, 'mcum_graph', 'source_artifacts',
                          %s, %s, %s, %s, 1.0, 'extracted', 'active', %s, NULL, NOW())
                ON CONFLICT (project_id, source_schema, source_table, source_id) DO UPDATE
                SET canonical_key = EXCLUDED.canonical_key,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    content_hash = EXCLUDED.content_hash,
                    health_state = 'active',
                    metadata = EXCLUDED.metadata,
                    valid_to = NULL,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    project_id,
                    artifact_canonical,
                    artifact_id,
                    source_path,
                    f"{artifact_kind} artifact",
                    content_hash,
                    _json(metadata),
                ),
            )
            artifact_entity_id = str(cur.fetchone()["id"])
            cur.execute(
                """
                UPDATE mcum_graph.entities
                SET valid_to = NOW(), health_state = 'deprecated', updated_at = NOW()
                WHERE project_id = %s
                  AND source_schema = 'mcum_graph'
                  AND source_table = 'artifact_sections'
                  AND metadata->>'artifact_id' = %s
                  AND valid_to IS NULL
                """,
                (project_id, artifact_id),
            )
            cur.execute(
                """
                UPDATE mcum_graph.relations relation
                SET valid_to = NOW(), updated_at = NOW()
                WHERE relation.project_id = %s
                  AND relation.source_entity_id = %s
                  AND relation.relation_type = 'HAS_SECTION'
                  AND relation.valid_to IS NULL
                """,
                (project_id, artifact_entity_id),
            )
            section_count = 0
            for section in result.get("sections") or []:
                cur.execute(
                    """
                    INSERT INTO mcum_graph.artifact_sections (
                        artifact_id, section_kind, locator, title, summary,
                        text_excerpt, confidence, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        artifact_id,
                        str(section.get("section_kind") or "text"),
                        _json({"ordinal": section.get("ordinal")}),
                        str((section.get("metadata") or {}).get("heading") or ""),
                        None,
                        str(section.get("text") or "")[:4000],
                        float(section.get("confidence") or 0),
                        _json(section.get("metadata") or {}),
                    ),
                )
                section_id = str(cur.fetchone()["id"])
                ordinal = int(section.get("ordinal") or section_count)
                title = str((section.get("metadata") or {}).get("heading") or f"{source_path} section {ordinal + 1}")
                excerpt = str(section.get("text") or "")[:1000]
                cur.execute(
                    """
                    INSERT INTO mcum_graph.entities (
                        project_id, entity_type, canonical_key, source_schema, source_table,
                        source_id, title, summary, confidence, provenance_kind,
                        health_state, metadata, valid_to, updated_at
                    ) VALUES (%s, 'artifact_section', %s, 'mcum_graph', 'artifact_sections',
                              %s, %s, %s, %s, 'extracted', 'active', %s, NULL, NOW())
                    ON CONFLICT (project_id, source_schema, source_table, source_id) DO UPDATE
                    SET canonical_key = EXCLUDED.canonical_key,
                        title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        confidence = EXCLUDED.confidence,
                        health_state = 'active',
                        metadata = EXCLUDED.metadata,
                        valid_to = NULL,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        project_id,
                        f"artifact_section:{artifact_id}:{ordinal}",
                        section_id,
                        title,
                        excerpt,
                        float(section.get("confidence") or 0),
                        _json({"artifact_id": artifact_id, "relative_path": source_path, "ordinal": ordinal}),
                    ),
                )
                section_entity_id = str(cur.fetchone()["id"])
                cur.execute(
                    """
                    INSERT INTO mcum_graph.relations (
                        project_id, source_entity_id, target_entity_id, relation_type,
                        weight, confidence, provenance_kind, evidence_ref, metadata,
                        valid_to, updated_at
                    ) VALUES (%s, %s, %s, 'HAS_SECTION', 1.0, %s, 'extracted', %s, %s, NULL, NOW())
                    ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type) DO UPDATE
                    SET confidence = EXCLUDED.confidence,
                        evidence_ref = EXCLUDED.evidence_ref,
                        metadata = EXCLUDED.metadata,
                        valid_to = NULL,
                        updated_at = NOW()
                    """,
                    (
                        project_id,
                        artifact_entity_id,
                        section_entity_id,
                        float(section.get("confidence") or 0),
                        _json({"artifact_id": artifact_id, "section_id": section_id}),
                        _json({"relative_path": source_path, "ordinal": ordinal}),
                    ),
                )
                section_count += 1
    return {
        "status": "success",
        "artifact_id": artifact_id,
        "artifact_entity_id": artifact_entity_id,
        "sections": section_count,
    }
