from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from MCUM.core.graph_policy import GraphPolicy, GraphQueryLimits
from MCUM.core.graph_query_service import GraphQueryService, PostgreSQLGraphBackend


class MemoryBackend:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.relations: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def add_node(self, project: str, node_id: str, entity_type: str = "code_symbol") -> None:
        self.nodes[project][node_id] = {
            "id": node_id,
            "project_id": project,
            "canonical_key": f"{entity_type}:{node_id}",
            "entity_type": entity_type,
            "title": node_id.upper(),
            "source_schema": "fixture",
            "source_table": "nodes",
            "source_id": node_id,
            "confidence": 1.0,
        }

    def add_relation(
        self,
        project: str,
        relation_id: str,
        source: str,
        target: str,
        relation_type: str = "CALLS",
    ) -> None:
        self.relations.append(
            {
                "id": relation_id,
                "project_id": project,
                "source_entity_id": source,
                "target_entity_id": target,
                "relation_type": relation_type,
                "evidence_ref": {"fixture": relation_id},
            }
        )

    def get_node(self, *, project_id: str, node_ref: str) -> dict[str, Any] | None:
        self.calls.append({"kind": "node", "project_id": project_id, "node_ref": node_ref})
        for node in self.nodes[project_id].values():
            if node["id"] == node_ref or node["canonical_key"] == node_ref:
                return dict(node)
        return None

    def get_relations(
        self,
        *,
        project_id: str,
        node_id: str,
        direction: str,
        relation_types: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        self.calls.append({"kind": "relations", "project_id": project_id, "node_id": node_id})
        rows = []
        for relation in self.relations:
            if relation["project_id"] != project_id:
                continue
            if relation_types and relation["relation_type"] not in relation_types:
                continue
            inbound = relation["target_entity_id"] == node_id
            outbound = relation["source_entity_id"] == node_id
            if (direction == "in" and inbound) or (direction == "out" and outbound) or (
                direction == "both" and (inbound or outbound)
            ):
                rows.append(dict(relation))
        return sorted(rows, key=lambda row: row["id"])[offset : offset + limit]

    def get_snapshot(self, *, project_id: str) -> dict[str, Any]:
        return {"id": f"snapshot-{project_id}", "project_id": project_id}


def _service(backend: MemoryBackend, *, max_nodes: int = 20) -> GraphQueryService:
    return GraphQueryService(
        backend,
        GraphPolicy(
            query=GraphQueryLimits(
                default_depth=1,
                max_depth=4,
                default_page_size=10,
                max_page_size=20,
                max_nodes=max_nodes,
                max_edges_per_node=20,
                max_evidence_items=20,
            )
        ),
    )


def test_get_node_is_project_scoped_and_supports_canonical_key() -> None:
    backend = MemoryBackend()
    backend.add_node("project-a", "shared")
    backend.add_node("project-a", "a2")
    backend.add_node("project-b", "shared")
    backend.add_node("project-b", "b2")
    backend.add_relation("project-a", "r-a", "shared", "a2")
    backend.add_relation("project-b", "r-b", "shared", "b2")

    result = _service(backend).get_node(
        project_id="project-a",
        node_ref="code_symbol:shared",
    )

    assert result["node"]["project_id"] == "project-a"
    assert [row["id"] for row in result["relations"]] == ["r-a"]
    assert all(call["project_id"] == "project-a" for call in backend.calls)


def test_neighbors_bfs_protects_cycles_and_honors_direction_filters_and_depth() -> None:
    backend = MemoryBackend()
    for node_id in ("a", "b", "c", "d"):
        backend.add_node("project-a", node_id)
    backend.add_relation("project-a", "r1", "a", "b", "CALLS")
    backend.add_relation("project-a", "r2", "b", "c", "CALLS")
    backend.add_relation("project-a", "r3", "c", "a", "CALLS")
    backend.add_relation("project-a", "r4", "d", "a", "IMPORTS")

    result = _service(backend).neighbors(
        project_id="project-a",
        node_ref="a",
        direction="out",
        depth=3,
        relation_types=["CALLS"],
    )

    assert [item["node"]["id"] for item in result["neighbors"]] == ["b", "c"]
    assert result["traversal"]["visited_nodes"] == 3
    assert result["traversal"]["budget_exhausted"] is False
    assert {row["id"] for row in result["relations"]} == {"r1", "r2", "r3"}


def test_neighbors_applies_entity_filter_pagination_and_node_budget() -> None:
    backend = MemoryBackend()
    backend.add_node("project-a", "root")
    for node_id, entity_type in (("a", "test"), ("b", "code_file"), ("c", "test")):
        backend.add_node("project-a", node_id, entity_type)
        backend.add_relation("project-a", f"r-{node_id}", "root", node_id)

    filtered = _service(backend).neighbors(
        project_id="project-a",
        node_ref="root",
        entity_types=["test"],
        limit=1,
        offset=1,
    )
    bounded = _service(backend, max_nodes=2).neighbors(
        project_id="project-a",
        node_ref="root",
        node_budget=99,
    )

    assert [item["node"]["id"] for item in filtered["neighbors"]] == ["c"]
    assert filtered["pagination"]["has_more"] is False
    assert bounded["traversal"]["visited_nodes"] == 2
    assert bounded["traversal"]["budget_exhausted"] is True


def test_neighbors_with_root_only_budget_does_not_expand() -> None:
    backend = MemoryBackend()
    backend.add_node("project-a", "root")
    backend.add_node("project-a", "child")
    backend.add_relation("project-a", "r1", "root", "child")

    result = _service(backend, max_nodes=1).neighbors(
        project_id="project-a",
        node_ref="root",
        node_budget=10,
    )

    assert result["neighbors"] == []
    assert result["traversal"]["visited_nodes"] == 1
    assert result["traversal"]["budget_exhausted"] is True


def test_explain_is_deterministic_and_evidence_is_traceable() -> None:
    backend = MemoryBackend()
    for node_id, entity_type in (
        ("root", "code_symbol"),
        ("caller", "code_symbol"),
        ("file", "code_file"),
        ("experience", "experience"),
        ("test", "test"),
    ):
        backend.add_node("project-a", node_id, entity_type)
    backend.add_relation("project-a", "r1", "caller", "root")
    backend.add_relation("project-a", "r2", "root", "file", "DEFINED_IN")
    backend.add_relation("project-a", "r3", "root", "experience", "SUPPORTED_BY")
    backend.add_relation("project-a", "r4", "root", "test", "TESTED_BY")

    service = _service(backend)
    first = service.explain(project_id="project-a", node_ref="root")
    second = service.explain(project_id="project-a", node_ref="root")

    assert first == second
    assert first["facts"]["inbound_neighbors"] == 1
    assert first["facts"]["outbound_neighbors"] == 3
    assert [item["id"] for item in first["related"]["files"]] == ["file"]
    assert [item["id"] for item in first["related"]["experiences"]] == ["experience"]
    assert [item["id"] for item in first["related"]["tests"]] == ["test"]
    assert {item["kind"] for item in first["evidence"]} == {"node", "relation"}


def test_service_rechecks_backend_filters_and_removes_cross_project_embedded_nodes() -> None:
    backend = MemoryBackend()
    backend.add_node("project-a", "root")
    backend.add_node("project-a", "child")
    backend.add_relation("project-a", "allowed", "root", "child", "CALLS")
    backend.add_relation("project-a", "blocked", "other", "elsewhere", "IMPORTS")

    def unfiltered_relations(**kwargs: Any) -> list[dict[str, Any]]:
        rows = [dict(row) for row in backend.relations]
        rows[0]["target_node"] = {"id": "child", "project_id": "project-b"}
        return rows

    backend.get_relations = unfiltered_relations  # type: ignore[method-assign]
    result = _service(backend).get_node(
        project_id="project-a",
        node_ref="root",
        direction="out",
        relation_types=["CALLS"],
    )
    neighbors = _service(backend).neighbors(
        project_id="project-a",
        node_ref="root",
        direction="out",
        relation_types=["CALLS"],
    )

    assert [row["id"] for row in result["relations"]] == ["allowed"]
    assert "target_node" not in result["relations"][0]
    assert [item["node"]["id"] for item in neighbors["neighbors"]] == ["child"]
    assert [row["id"] for row in neighbors["relations"]] == ["allowed"]


def test_invalid_direction_is_rejected() -> None:
    backend = MemoryBackend()
    backend.add_node("project-a", "a")

    with pytest.raises(ValueError, match="direction"):
        _service(backend).neighbors(project_id="project-a", node_ref="a", direction="sideways")


def test_postgresql_adapter_scopes_every_query_by_project() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def query_one(sql: str, params: tuple[Any, ...]) -> None:
        calls.append((sql, params))
        return None

    def query_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        return []

    backend = PostgreSQLGraphBackend(query_one=query_one, query_all=query_all)
    backend.get_node(project_id="project-a", node_ref="node-a")
    backend.get_relations(
        project_id="project-a",
        node_id="node-a",
        direction="both",
        relation_types=("CALLS",),
        limit=10,
        offset=0,
    )
    backend.get_snapshot(project_id="project-a")

    assert len(calls) == 3
    assert all("project_id = %s" in sql or "r.project_id = %s" in sql for sql, _ in calls)
    assert all(params[0] == "project-a" for _, params in calls)
