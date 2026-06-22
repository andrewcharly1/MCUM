"""Pure bounded change-impact analysis and conservative test selection."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from .graph_analytics import _clamp, _normalize_graph, _text


IMPACT_VERSION = "mcum-graph-impact-v1"
TEST_KINDS = {"test", "test_case", "test_suite", "unit_test", "integration_test", "e2e_test"}


def _is_test(node: dict[str, Any]) -> bool:
    kind = _text(node.get("entity_type") or node.get("node_kind")).lower()
    path = _text(node.get("relative_path")).lower()
    title = _text(node.get("title") or node.get("name")).lower()
    return (
        kind in TEST_KINDS
        or path.startswith(("test/", "tests/", "spec/", "specs/"))
        or "/test_" in f"/{path}"
        or path.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        or title.startswith("test_")
    )


def _risk_boost(node: dict[str, Any]) -> tuple[float, list[str]]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    reasons: list[str] = []
    boost = 0.0
    criticality = _text(metadata.get("criticality") or node.get("criticality")).lower()
    if criticality in {"critical", "high", "security", "regulatory"}:
        boost += 0.18
        reasons.append(f"criticality={criticality}")
    if _text(node.get("entity_type")).lower() in {"spec_contract", "database", "api_contract"}:
        boost += 0.12
        reasons.append(f"entity_type={node.get('entity_type')}")
    try:
        explicit = _clamp(metadata.get("risk_score") if "risk_score" in metadata else node.get("risk_score"))
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit:
        boost += explicit * 0.20
        reasons.append(f"declared_risk={explicit:.2f}")
    return min(0.30, boost), reasons


def _mandatory_tests(
    mandatory_test_ids: Iterable[str] | None,
    spec_contract: dict[str, Any] | None,
) -> set[str]:
    required = {_text(value) for value in mandatory_test_ids or [] if _text(value)}
    spec = dict(spec_contract or {})
    for key in ("mandatory_test_ids", "required_test_ids", "tests_required"):
        values = spec.get(key)
        if isinstance(values, (list, tuple, set)):
            required.update(_text(value) for value in values if _text(value))
    return required


def analyze_impact(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    changed_paths: Iterable[str] | None = None,
    changed_entities: Iterable[str] | None = None,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
    tests: Iterable[dict[str, Any]] | None = None,
    mandatory_test_ids: Iterable[str] | None = None,
    spec_contract: dict[str, Any] | None = None,
    snapshot_id: str | None = None,
    max_depth: int = 3,
    max_items: int = 250,
    confidence_threshold: float = 0.65,
) -> dict[str, Any]:
    """Walk both dependency directions and select tests, escalating when unsure."""
    source_graph = dict(graph or {})
    if nodes is None:
        raw_nodes = list(source_graph.get("nodes") or source_graph.get("entities") or [])
    else:
        raw_nodes = list(nodes)
    identities = {
        _text(item.get("id") or item.get("entity_id") or item.get("canonical_key") or item.get("qualified_name"))
        for item in raw_nodes
    }
    for test in tests or []:
        test_identity = _text(
            test.get("id") or test.get("entity_id") or test.get("canonical_key") or test.get("qualified_name")
        )
        if test_identity and test_identity not in identities:
            raw_nodes.append(dict(test))
            identities.add(test_identity)
    normalized_nodes, normalized_edges, source = _normalize_graph(
        source_graph,
        project_id=project_id,
        nodes=raw_nodes,
        edges=edges,
    )
    node_by_id = {item["id"]: item for item in normalized_nodes}
    aliases: defaultdict[str, set[str]] = defaultdict(set)
    path_map: defaultdict[str, set[str]] = defaultdict(set)
    for node in normalized_nodes:
        for alias in (
            node["id"],
            node.get("canonical_key"),
            node.get("qualified_name"),
            node.get("name"),
            node.get("title"),
        ):
            if _text(alias):
                aliases[_text(alias)].add(node["id"])
        if node.get("relative_path"):
            path_map[_text(node["relative_path"]).replace("\\", "/")].add(node["id"])

    requested_entities = [_text(value) for value in changed_entities or [] if _text(value)]
    requested_paths = [_text(value).replace("\\", "/") for value in changed_paths or [] if _text(value)]
    if not requested_entities and not requested_paths:
        raise ValueError("changed_paths or changed_entities is required")
    changed_ids: set[str] = set()
    unresolved_changes: list[str] = []
    for reference in requested_entities:
        matches = aliases.get(reference, set())
        if len(matches) == 1:
            changed_ids.update(matches)
        else:
            unresolved_changes.append(reference)
    for path in requested_paths:
        matches = path_map.get(path, set())
        if matches:
            changed_ids.update(matches)
        else:
            unresolved_changes.append(path)

    outgoing: defaultdict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    incoming: defaultdict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for edge in normalized_edges:
        outgoing[edge["source_id"]].append((edge["target_id"], edge))
        incoming[edge["target_id"]].append((edge["source_id"], edge))
    for values in list(outgoing.values()) + list(incoming.values()):
        values.sort(key=lambda pair: (pair[0], pair[1]["relation_type"], pair[1]["id"]))

    depth_limit = max(0, min(8, int(max_depth)))
    item_limit = max(1, int(max_items))
    best: dict[str, dict[str, Any]] = {}
    queue: deque[tuple[str, int, float, list[str], list[str]]] = deque()
    for node_id in sorted(changed_ids):
        queue.append((node_id, 0, 1.0, [node_id], ["directly changed"]))
    edge_confidences: list[float] = []
    truncated = False
    while queue:
        node_id, distance, inherited_risk, path, reasons = queue.popleft()
        boost, boost_reasons = _risk_boost(node_by_id[node_id])
        risk = min(1.0, inherited_risk + boost)
        previous = best.get(node_id)
        if previous and (previous["distance"] < distance or previous["risk_score"] >= risk):
            continue
        best[node_id] = {
            "project_id": source["project_id"],
            "entity_id": node_id,
            "title": node_by_id[node_id]["title"],
            "entity_type": node_by_id[node_id]["entity_type"],
            "relative_path": node_by_id[node_id]["relative_path"],
            "impact_kind": "changed" if distance == 0 else "dependency",
            "distance": distance,
            "risk_score": round(risk, 6),
            "reason": "; ".join(reasons + boost_reasons),
            "evidence": {"path": path},
        }
        if len(best) >= item_limit:
            truncated = bool(queue)
            break
        if distance >= depth_limit:
            continue
        neighbors = [
            (target, edge, "outgoing dependency")
            for target, edge in outgoing[node_id]
        ] + [
            (target, edge, "incoming dependent")
            for target, edge in incoming[node_id]
        ]
        for target, edge, direction in neighbors:
            if target in path:
                continue
            confidence = _clamp(edge.get("confidence"), 1.0)
            edge_confidences.append(confidence)
            next_risk = risk * confidence * 0.78
            queue.append(
                (
                    target,
                    distance + 1,
                    next_risk,
                    path + [target],
                    [f"{direction} via {edge['relation_type']} ({confidence:.2f})"],
                )
            )

    impact_items = sorted(best.values(), key=lambda item: (-item["risk_score"], item["distance"], item["entity_id"]))
    test_nodes = {node["id"]: node for node in normalized_nodes if _is_test(node)}
    required_refs = _mandatory_tests(mandatory_test_ids, spec_contract)
    required_ids: set[str] = set()
    missing_required: list[str] = []
    for reference in sorted(required_refs):
        matches = aliases.get(reference, set())
        test_matches = {item for item in matches if item in test_nodes}
        if len(test_matches) == 1:
            required_ids.update(test_matches)
        else:
            missing_required.append(reference)

    selected: dict[str, dict[str, Any]] = {}
    for test_id, test_node in test_nodes.items():
        impacted = best.get(test_id)
        linked_impacts: list[dict[str, Any]] = []
        for neighbor, edge in outgoing[test_id] + incoming[test_id]:
            if neighbor in best:
                linked_impacts.append({"entity_id": neighbor, "edge": edge})
        if impacted or linked_impacts or test_id in required_ids:
            coverage = max(
                [best[item["entity_id"]]["risk_score"] * item["edge"]["confidence"] for item in linked_impacts]
                + ([impacted["risk_score"]] if impacted else [])
                + ([1.0] if test_id in required_ids else [0.0])
            )
            reasons = []
            if test_id in required_ids:
                reasons.append("required by active spec contract")
            if impacted:
                reasons.append(f"test node reached at distance {impacted['distance']}")
            if linked_impacts:
                reasons.append(
                    "linked to impacted entities: "
                    + ", ".join(sorted({item["entity_id"] for item in linked_impacts}))
                )
            selected[test_id] = {
                "project_id": source["project_id"],
                "test_entity_id": test_id,
                "title": test_node["title"],
                "relative_path": test_node["relative_path"],
                "coverage_score": round(min(1.0, coverage), 6),
                "historical_failure_score": round(
                    _clamp((test_node.get("metadata") or {}).get("historical_failure_score")), 6
                ),
                "required": test_id in required_ids,
                "reason": "; ".join(reasons),
            }

    total_requests = len(requested_entities) + len(requested_paths)
    resolved_ratio = (total_requests - len(unresolved_changes)) / max(1, total_requests)
    traversal_confidence = sum(edge_confidences) / len(edge_confidences) if edge_confidences else (1.0 if changed_ids else 0.0)
    edge_resolution = len(normalized_edges) / max(1, len(normalized_edges) + source["unresolved_edges"])
    test_coverage = 1.0 if selected else 0.0
    confidence = round(
        (0.50 * resolved_ratio + 0.30 * traversal_confidence + 0.20 * test_coverage) * edge_resolution,
        6,
    )
    fallback_reasons: list[str] = []
    if confidence < _clamp(confidence_threshold, 0.65):
        fallback_reasons.append(f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}")
    if unresolved_changes:
        fallback_reasons.append("some changed inputs could not be resolved")
    if missing_required:
        fallback_reasons.append("mandatory tests could not be resolved")
    if test_nodes and not selected:
        fallback_reasons.append("no relevant tests could be linked")
    if not test_nodes:
        fallback_reasons.append("test inventory unavailable")
    if source["unresolved_edges"]:
        fallback_reasons.append("some graph edges could not be resolved")
    if truncated:
        fallback_reasons.append("impact traversal reached its item budget")
    selection_mode = "full_suite" if fallback_reasons else "targeted"
    if selection_mode == "full_suite":
        for test_id, test_node in test_nodes.items():
            selected.setdefault(
                test_id,
                {
                    "project_id": source["project_id"],
                    "test_entity_id": test_id,
                    "title": test_node["title"],
                    "relative_path": test_node["relative_path"],
                    "coverage_score": 0.0,
                    "historical_failure_score": round(
                        _clamp((test_node.get("metadata") or {}).get("historical_failure_score")), 6
                    ),
                    "required": test_id in required_ids,
                    "reason": "included by conservative full_suite fallback",
                },
            )
    selected_tests = sorted(
        selected.values(),
        key=lambda item: (not item["required"], -item["coverage_score"], item["test_entity_id"]),
    )
    for rank, item in enumerate(selected_tests, start=1):
        item["selection_rank"] = rank

    return {
        "status": "success",
        "project_id": source["project_id"],
        "snapshot_id": _text(snapshot_id) or source["snapshot_id"],
        "algorithm_version": IMPACT_VERSION,
        "changed": {
            "paths": requested_paths,
            "entities": requested_entities,
            "resolved_entity_ids": sorted(changed_ids),
            "unresolved": sorted(unresolved_changes),
        },
        "max_depth": depth_limit,
        "impact_items": impact_items,
        "test_selection": {
            "mode": selection_mode,
            "confidence": confidence,
            "fallback_reasons": fallback_reasons,
            "missing_required_tests": missing_required,
            "tests": selected_tests,
        },
        "metrics": {
            "nodes_considered": len(normalized_nodes),
            "edges_considered": len(normalized_edges),
            "impacted": len(impact_items),
            "tests_available": len(test_nodes),
            "tests_selected": len(selected_tests),
            "truncated": truncated,
            "unresolved_edges": source["unresolved_edges"],
        },
    }


assess_change_impact = analyze_impact
compute_graph_impact = analyze_impact
graph_impact = analyze_impact


def select_impacted_tests(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return analyze_impact(*args, **kwargs)["test_selection"]
