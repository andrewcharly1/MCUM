"""Project-scoped, bounded graph exploration without mandatory database access."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, Callable, Mapping, Protocol, Sequence

from .graph_policy import GraphPolicy, load_graph_policy


class GraphBackend(Protocol):
    def get_node(self, *, project_id: str, node_ref: str) -> Mapping[str, Any] | None:
        ...

    def get_relations(
        self,
        *,
        project_id: str,
        node_id: str,
        direction: str,
        relation_types: Sequence[str],
        limit: int,
        offset: int,
    ) -> Sequence[Mapping[str, Any]]:
        ...

    def get_snapshot(self, *, project_id: str) -> Mapping[str, Any] | None:
        ...


class CallbackGraphBackend:
    """Backend adapter for pure tests or existing project-scoped store functions."""

    def __init__(
        self,
        *,
        get_node: Callable[..., Mapping[str, Any] | None],
        get_relations: Callable[..., Sequence[Mapping[str, Any]]],
        get_snapshot: Callable[..., Mapping[str, Any] | None] | None = None,
    ) -> None:
        self._get_node = get_node
        self._get_relations = get_relations
        self._get_snapshot = get_snapshot

    def get_node(self, **kwargs: Any) -> Mapping[str, Any] | None:
        return self._get_node(**kwargs)

    def get_relations(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
        return self._get_relations(**kwargs)

    def get_snapshot(self, **kwargs: Any) -> Mapping[str, Any] | None:
        return self._get_snapshot(**kwargs) if self._get_snapshot else None


class PostgreSQLGraphBackend:
    """Optional PostgreSQL adapter using caller-owned read-only query callbacks."""

    def __init__(
        self,
        *,
        query_one: Callable[[str, Sequence[Any]], Mapping[str, Any] | None],
        query_all: Callable[[str, Sequence[Any]], Sequence[Mapping[str, Any]]],
    ) -> None:
        self._query_one = query_one
        self._query_all = query_all

    def get_node(self, *, project_id: str, node_ref: str) -> Mapping[str, Any] | None:
        return self._query_one(
            """
            SELECT *
            FROM mcum_graph.entities
            WHERE project_id = %s
              AND valid_to IS NULL
              AND (id::text = %s OR canonical_key = %s)
            ORDER BY CASE WHEN id::text = %s THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (project_id, node_ref, node_ref, node_ref),
        )

    def get_relations(
        self,
        *,
        project_id: str,
        node_id: str,
        direction: str,
        relation_types: Sequence[str],
        limit: int,
        offset: int,
    ) -> Sequence[Mapping[str, Any]]:
        direction_sql = {
            "in": "r.target_entity_id::text = %s",
            "out": "r.source_entity_id::text = %s",
            "both": "(r.source_entity_id::text = %s OR r.target_entity_id::text = %s)",
        }[direction]
        direction_params: tuple[Any, ...] = (node_id, node_id) if direction == "both" else (node_id,)
        type_sql = "AND r.relation_type = ANY(%s::text[])" if relation_types else ""
        type_params: tuple[Any, ...] = (list(relation_types),) if relation_types else ()
        return self._query_all(
            f"""
            SELECT r.*, to_jsonb(src) AS source_node, to_jsonb(dst) AS target_node
            FROM mcum_graph.relations r
            JOIN mcum_graph.entities src
              ON src.id = r.source_entity_id AND src.project_id = r.project_id AND src.valid_to IS NULL
            JOIN mcum_graph.entities dst
              ON dst.id = r.target_entity_id AND dst.project_id = r.project_id AND dst.valid_to IS NULL
            WHERE r.project_id = %s
              AND r.valid_to IS NULL
              AND {direction_sql}
              {type_sql}
            ORDER BY r.relation_type, r.source_entity_id, r.target_entity_id, r.id
            LIMIT %s OFFSET %s
            """,
            (project_id, *direction_params, *type_params, limit, offset),
        )

    def get_snapshot(self, *, project_id: str) -> Mapping[str, Any] | None:
        return self._query_one(
            """
            SELECT *
            FROM mcum_graph.snapshots
            WHERE project_id = %s
            ORDER BY created_at DESC, id
            LIMIT 1
            """,
            (project_id,),
        )


class GraphQueryService:
    """Bounded graph API with deterministic traversal and traceable evidence."""

    _DIRECTIONS = {"in", "out", "both"}

    def __init__(self, backend: GraphBackend, policy: GraphPolicy | None = None) -> None:
        self.backend = backend
        self.policy = policy or load_graph_policy()

    def get_node(
        self,
        *,
        project_id: str,
        node_ref: str,
        direction: str = "both",
        relation_types: Sequence[str] | None = None,
        relation_limit: int | None = None,
        relation_offset: int = 0,
    ) -> dict[str, Any]:
        project = self._required(project_id, "project_id")
        reference = self._required(node_ref, "node_ref")
        resolved_direction = self._direction(direction)
        node = self._scoped_node(project, reference)
        if node is None:
            return self._not_found(project, reference)

        limit = min(self._page_limit(relation_limit), self.policy.query.max_edges_per_node)
        offset = max(0, int(relation_offset or 0))
        relation_filter = self._filters(relation_types)
        relation_rows = self.backend.get_relations(
            project_id=project,
            node_id=str(node["id"]),
            direction=resolved_direction,
            relation_types=relation_filter,
            limit=limit + 1,
            offset=offset,
        )
        relations = []
        for relation in self._scoped_relations(project, relation_rows):
            if not self._relation_matches(str(node["id"]), relation, resolved_direction, relation_filter):
                continue
            neighbor_id, _ = self._neighbor_ref(str(node["id"]), relation)
            if neighbor_id and self._embedded_or_fetch(project, neighbor_id, relation) is None:
                continue
            relations.append(relation)
        has_more = len(relations) > limit
        relations = relations[:limit]
        return {
            "status": "success",
            "project_id": project,
            "node": node,
            "relations": relations,
            "snapshot": self._scoped_snapshot(project),
            "pagination": {
                "offset": offset,
                "limit": limit,
                "returned": len(relations),
                "has_more": has_more,
            },
            "evidence": self._evidence(node, relations),
        }

    def neighbors(
        self,
        *,
        project_id: str,
        node_ref: str,
        direction: str = "both",
        depth: int | None = None,
        relation_types: Sequence[str] | None = None,
        entity_types: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        node_budget: int | None = None,
    ) -> dict[str, Any]:
        project = self._required(project_id, "project_id")
        reference = self._required(node_ref, "node_ref")
        resolved_direction = self._direction(direction)
        root = self._scoped_node(project, reference)
        if root is None:
            return self._not_found(project, reference)

        max_depth = self._depth(depth)
        page_limit = self._page_limit(limit)
        page_offset = max(0, int(offset or 0))
        budget = max(1, min(int(node_budget or self.policy.query.max_nodes), self.policy.query.max_nodes))
        relation_filter = self._filters(relation_types)
        entity_filter = set(self._filters(entity_types))

        root_id = str(root["id"])
        queue: deque[tuple[dict[str, Any], int]] = deque([(root, 0)])
        visited = {root_id}
        discovered: list[dict[str, Any]] = []
        relation_map: dict[str, dict[str, Any]] = {}
        budget_exhausted = budget <= len(visited)

        while queue and not budget_exhausted:
            current, distance = queue.popleft()
            if distance >= max_depth:
                continue
            rows = self.backend.get_relations(
                project_id=project,
                node_id=str(current["id"]),
                direction=resolved_direction,
                relation_types=relation_filter,
                limit=self.policy.query.max_edges_per_node,
                offset=0,
            )
            for relation in self._scoped_relations(project, rows):
                if not self._relation_matches(
                    str(current["id"]),
                    relation,
                    resolved_direction,
                    relation_filter,
                ):
                    continue
                neighbor_id, edge_direction = self._neighbor_ref(str(current["id"]), relation)
                if not neighbor_id:
                    continue
                if neighbor_id in visited:
                    relation_map[self._relation_key(relation)] = relation
                    continue
                node = self._embedded_or_fetch(project, neighbor_id, relation)
                if node is None:
                    continue
                relation_map[self._relation_key(relation)] = relation
                visited.add(neighbor_id)
                if len(visited) >= budget:
                    budget_exhausted = True
                if entity_filter and str(node.get("entity_type") or "") not in entity_filter:
                    if budget_exhausted:
                        break
                    continue
                item = {
                    "node": node,
                    "distance": distance + 1,
                    "direction": edge_direction,
                    "via_relation_id": str(relation.get("id") or ""),
                    "via_relation_type": str(relation.get("relation_type") or ""),
                }
                discovered.append(item)
                if distance + 1 < max_depth and not budget_exhausted:
                    queue.append((node, distance + 1))
                if budget_exhausted:
                    break

        page = discovered[page_offset : page_offset + page_limit]
        page_ids = {root_id, *(str(item["node"]["id"]) for item in page)}
        page_relations = [
            relation
            for relation in relation_map.values()
            if str(relation.get("source_entity_id")) in page_ids
            or str(relation.get("target_entity_id")) in page_ids
        ]
        page_relations = sorted(page_relations, key=self._relation_sort_key)[
            : self.policy.query.max_evidence_items
        ]
        return {
            "status": "success",
            "project_id": project,
            "root": root,
            "neighbors": page,
            "relations": page_relations,
            "snapshot": self._scoped_snapshot(project),
            "traversal": {
                "direction": resolved_direction,
                "depth": max_depth,
                "visited_nodes": len(visited),
                "discovered_neighbors": len(discovered),
                "node_budget": budget,
                "budget_exhausted": budget_exhausted,
            },
            "pagination": {
                "offset": page_offset,
                "limit": page_limit,
                "returned": len(page),
                "has_more": page_offset + page_limit < len(discovered),
            },
            "evidence": self._evidence(root, page_relations),
        }

    def explain(
        self,
        *,
        project_id: str,
        node_ref: str,
        relation_types: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        result = self.neighbors(
            project_id=project_id,
            node_ref=node_ref,
            direction="both",
            depth=1,
            relation_types=relation_types,
            limit=self.policy.query.max_page_size,
            node_budget=min(self.policy.query.max_nodes, self.policy.query.max_edges_per_node + 1),
        )
        if result["status"] != "success":
            return result

        node = result["root"]
        related = {
            "callers": [],
            "callees": [],
            "files": [],
            "experiences": [],
            "specs": [],
            "tests": [],
            "other": [],
        }
        inbound = 0
        outbound = 0
        for item in result["neighbors"]:
            compact = self._compact_node(item["node"], item)
            if item["direction"] == "in":
                inbound += 1
                related["callers"].append(compact)
            elif item["direction"] == "out":
                outbound += 1
                related["callees"].append(compact)
            category = self._explain_category(str(item["node"].get("entity_type") or ""))
            if category not in {"callers", "callees"}:
                related[category].append(compact)

        for values in related.values():
            values.sort(key=lambda item: (item["canonical_key"], item["id"]))
        entity_type = str(node.get("entity_type") or "entity")
        title = str(node.get("title") or node.get("canonical_key") or node["id"])
        return {
            "status": "success",
            "project_id": result["project_id"],
            "node": node,
            "summary": (
                f"{title} is a {entity_type} with {inbound} inbound and "
                f"{outbound} outbound direct related entities."
            ),
            "facts": {
                "entity_type": entity_type,
                "canonical_key": node.get("canonical_key"),
                "confidence": node.get("confidence"),
                "source": {
                    "schema": node.get("source_schema"),
                    "table": node.get("source_table"),
                    "id": node.get("source_id"),
                },
                "inbound_neighbors": inbound,
                "outbound_neighbors": outbound,
            },
            "related": related,
            "snapshot": result["snapshot"],
            "evidence": result["evidence"],
            "truncated": bool(
                result["traversal"]["budget_exhausted"] or result["pagination"]["has_more"]
            ),
        }

    def _scoped_node(self, project_id: str, node_ref: str) -> dict[str, Any] | None:
        raw = self.backend.get_node(project_id=project_id, node_ref=node_ref)
        if not isinstance(raw, Mapping):
            return None
        node = deepcopy(dict(raw))
        node_project = str(node.get("project_id") or project_id)
        if node_project != project_id or not node.get("id"):
            return None
        node["project_id"] = project_id
        node["id"] = str(node["id"])
        return node

    def _scoped_snapshot(self, project_id: str) -> dict[str, Any] | None:
        raw = self.backend.get_snapshot(project_id=project_id)
        if not isinstance(raw, Mapping):
            return None
        snapshot = deepcopy(dict(raw))
        if str(snapshot.get("project_id") or project_id) != project_id:
            return None
        snapshot["project_id"] = project_id
        return snapshot

    def _scoped_relations(
        self,
        project_id: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        for raw in rows or ():
            if not isinstance(raw, Mapping):
                continue
            relation = deepcopy(dict(raw))
            if str(relation.get("project_id") or project_id) != project_id:
                continue
            if not relation.get("source_entity_id") or not relation.get("target_entity_id"):
                continue
            relation["project_id"] = project_id
            relation["source_entity_id"] = str(relation["source_entity_id"])
            relation["target_entity_id"] = str(relation["target_entity_id"])
            for key in ("source_node", "target_node"):
                candidate = relation.get(key)
                if isinstance(candidate, Mapping):
                    embedded = deepcopy(dict(candidate))
                    if str(embedded.get("project_id") or project_id) != project_id:
                        relation.pop(key, None)
                    else:
                        embedded["project_id"] = project_id
                        relation[key] = embedded
            relations.append(relation)
        return sorted(relations, key=self._relation_sort_key)

    def _embedded_or_fetch(
        self,
        project_id: str,
        node_id: str,
        relation: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        for key in ("source_node", "target_node"):
            candidate = relation.get(key)
            if isinstance(candidate, Mapping) and str(candidate.get("id")) == node_id:
                node = deepcopy(dict(candidate))
                if str(node.get("project_id") or project_id) == project_id:
                    node["project_id"] = project_id
                    node["id"] = node_id
                    return node
        return self._scoped_node(project_id, node_id)

    @staticmethod
    def _neighbor_ref(current_id: str, relation: Mapping[str, Any]) -> tuple[str | None, str]:
        source = str(relation.get("source_entity_id") or "")
        target = str(relation.get("target_entity_id") or "")
        if source == current_id and target == current_id:
            return None, "self"
        if source == current_id:
            return target or None, "out"
        if target == current_id:
            return source or None, "in"
        return None, "unknown"

    def _evidence(
        self,
        node: Mapping[str, Any],
        relations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        evidence = [
            {
                "kind": "node",
                "node_id": str(node.get("id") or ""),
                "canonical_key": node.get("canonical_key"),
                "source_schema": node.get("source_schema"),
                "source_table": node.get("source_table"),
                "source_id": node.get("source_id"),
                "content_hash": node.get("content_hash"),
            }
        ]
        for relation in relations:
            evidence.append(
                {
                    "kind": "relation",
                    "relation_id": str(relation.get("id") or ""),
                    "relation_type": relation.get("relation_type"),
                    "source_entity_id": relation.get("source_entity_id"),
                    "target_entity_id": relation.get("target_entity_id"),
                    "provenance_kind": relation.get("provenance_kind"),
                    "confidence": relation.get("confidence"),
                    "evidence_ref": deepcopy(relation.get("evidence_ref") or {}),
                }
            )
        return evidence[: self.policy.query.max_evidence_items]

    @staticmethod
    def _compact_node(node: Mapping[str, Any], traversal: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(node.get("id") or ""),
            "canonical_key": str(node.get("canonical_key") or ""),
            "title": str(node.get("title") or ""),
            "entity_type": str(node.get("entity_type") or ""),
            "direction": traversal.get("direction"),
            "relation_type": traversal.get("via_relation_type"),
        }

    @staticmethod
    def _explain_category(entity_type: str) -> str:
        lowered = entity_type.lower()
        if "file" in lowered or "artifact" in lowered or "document" in lowered:
            return "files"
        if "experience" in lowered or "playbook" in lowered or "pattern" in lowered:
            return "experiences"
        if "spec" in lowered or "contract" in lowered or "requirement" in lowered:
            return "specs"
        if "test" in lowered:
            return "tests"
        return "other"

    @staticmethod
    def _relation_key(relation: Mapping[str, Any]) -> str:
        return str(
            relation.get("id")
            or (
                f"{relation.get('source_entity_id')}|{relation.get('relation_type')}|"
                f"{relation.get('target_entity_id')}"
            )
        )

    @staticmethod
    def _relation_sort_key(relation: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(relation.get("relation_type") or ""),
            str(relation.get("source_entity_id") or ""),
            str(relation.get("target_entity_id") or ""),
            str(relation.get("id") or ""),
        )

    @classmethod
    def _relation_matches(
        cls,
        node_id: str,
        relation: Mapping[str, Any],
        direction: str,
        relation_types: Sequence[str],
    ) -> bool:
        source = str(relation.get("source_entity_id") or "")
        target = str(relation.get("target_entity_id") or "")
        if relation_types and str(relation.get("relation_type") or "") not in relation_types:
            return False
        return (
            (direction == "in" and target == node_id)
            or (direction == "out" and source == node_id)
            or (direction == "both" and (source == node_id or target == node_id))
        )

    def _page_limit(self, value: int | None) -> int:
        requested = self.policy.query.default_page_size if value is None else int(value)
        return max(1, min(requested, self.policy.query.max_page_size))

    def _depth(self, value: int | None) -> int:
        requested = self.policy.query.default_depth if value is None else int(value)
        return max(0, min(requested, self.policy.query.max_depth))

    def _direction(self, value: str) -> str:
        direction = str(value or "").lower()
        if direction not in self._DIRECTIONS:
            raise ValueError("direction must be one of: in, out, both")
        return direction

    @staticmethod
    def _filters(values: Sequence[str] | None) -> tuple[str, ...]:
        return tuple(sorted({str(value).strip() for value in values or () if str(value).strip()}))

    @staticmethod
    def _required(value: str, name: str) -> str:
        resolved = str(value or "").strip()
        if not resolved:
            raise ValueError(f"{name} is required")
        return resolved

    @staticmethod
    def _not_found(project_id: str, node_ref: str) -> dict[str, Any]:
        return {
            "status": "not_found",
            "project_id": project_id,
            "node_ref": node_ref,
            "node": None,
            "relations": [],
            "evidence": [],
        }
