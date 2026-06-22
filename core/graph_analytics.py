"""Pure, deterministic analytics for bounded MCUM graph snapshots."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import math
from pathlib import PurePosixPath
from typing import Any, Iterable


ANALYTICS_VERSION = "mcum-graph-analytics-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _require_project_id(project_id: str, graph: dict[str, Any] | None = None) -> str:
    project = _text(project_id)
    if not project:
        raise ValueError("project_id is required")
    graph_project = _text((graph or {}).get("project_id"))
    if graph_project and graph_project != project:
        raise ValueError(f"graph project_id {graph_project!r} does not match {project!r}")
    return project


def _node_identity(node: dict[str, Any]) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    for value in (
        node.get("id"),
        node.get("entity_id"),
        node.get("canonical_key"),
        node.get("qualified_name"),
        metadata.get("canonical_key"),
        node.get("name"),
        node.get("title"),
    ):
        if _text(value):
            return _text(value)
    raise ValueError("every graph node requires an id, canonical_key, or name")


def _normalize_graph(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Normalize code_graph and mcum_graph payloads without mutating input."""
    source = dict(graph or {})
    project = _require_project_id(project_id, source)
    raw_nodes = list(nodes if nodes is not None else source.get("nodes") or source.get("entities") or [])
    raw_edges = list(edges if edges is not None else source.get("edges") or source.get("relations") or [])
    normalized_nodes: list[dict[str, Any]] = []
    aliases: dict[str, str | None] = {}
    seen: set[str] = set()

    for raw in raw_nodes:
        item = dict(raw or {})
        item_project = _text(item.get("project_id"))
        if item_project and item_project != project:
            raise ValueError(f"node belongs to project_id {item_project!r}, expected {project!r}")
        node_id = _node_identity(item)
        if node_id in seen:
            raise ValueError(f"duplicate node identity: {node_id}")
        seen.add(node_id)
        metadata = dict(item.get("metadata") or {})
        canonical_key = _text(item.get("canonical_key") or metadata.get("canonical_key") or node_id)
        qualified_name = _text(item.get("qualified_name") or item.get("title") or item.get("name") or node_id)
        relative_path = _text(item.get("relative_path") or metadata.get("relative_path"))
        normalized = {
            **item,
            "id": node_id,
            "project_id": project,
            "canonical_key": canonical_key,
            "qualified_name": qualified_name,
            "title": _text(item.get("title") or qualified_name),
            "name": _text(item.get("name") or qualified_name.rsplit(".", 1)[-1]),
            "entity_type": _text(item.get("entity_type") or item.get("node_kind") or "entity"),
            "node_kind": _text(item.get("node_kind") or item.get("entity_type") or "entity"),
            "relative_path": relative_path.replace("\\", "/"),
            "signature": _text(item.get("signature") or metadata.get("signature")),
            "confidence": _clamp(item.get("confidence"), 1.0),
            "metadata": metadata,
        }
        normalized_nodes.append(normalized)
        for alias in {
            node_id,
            canonical_key,
            qualified_name,
            _text(item.get("entity_id")),
            _text(item.get("name")),
            _text(item.get("title")),
        }:
            if not alias:
                continue
            aliases[alias] = node_id if alias not in aliases else None

    normalized_edges: list[dict[str, Any]] = []
    unresolved = 0
    for index, raw in enumerate(raw_edges):
        item = dict(raw or {})
        item_project = _text(item.get("project_id"))
        if item_project and item_project != project:
            raise ValueError(f"edge belongs to project_id {item_project!r}, expected {project!r}")
        source_ref = _text(
            item.get("source_id")
            or item.get("source_entity_id")
            or item.get("source_node_id")
            or item.get("source_ref")
            or item.get("source")
        )
        target_ref = _text(
            item.get("target_id")
            or item.get("target_entity_id")
            or item.get("target_node_id")
            or item.get("target_ref")
            or item.get("target")
        )
        source_id = aliases.get(source_ref)
        target_id = aliases.get(target_ref)
        if not source_id or not target_id:
            unresolved += 1
            continue
        relation_type = _text(item.get("relation_type") or item.get("edge_kind") or item.get("type") or "RELATED_TO")
        confidence = _clamp(item.get("confidence"), 1.0)
        try:
            weight = max(0.0, float(item.get("weight", 1.0))) * confidence
        except (TypeError, ValueError):
            weight = confidence
        edge_id = _text(item.get("id")) or hashlib.sha256(
            f"{source_id}|{relation_type}|{target_id}|{index}".encode("utf-8")
        ).hexdigest()[:16]
        normalized_edges.append(
            {
                **item,
                "id": edge_id,
                "project_id": project,
                "source_id": source_id,
                "target_id": target_id,
                "relation_type": relation_type,
                "confidence": confidence,
                "weight": weight,
                "metadata": dict(item.get("metadata") or {}),
            }
        )

    normalized_nodes.sort(key=lambda item: item["id"])
    normalized_edges.sort(key=lambda item: (item["source_id"], item["target_id"], item["relation_type"], item["id"]))
    return normalized_nodes, normalized_edges, {
        "project_id": project,
        "snapshot_id": _text(source.get("snapshot_id") or source.get("id")),
        "unresolved_edges": unresolved,
    }


def _adjacency(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, float]], Counter[str], Counter[str]]:
    adjacency: dict[str, dict[str, float]] = {item["id"]: {} for item in nodes}
    degree_in: Counter[str] = Counter()
    degree_out: Counter[str] = Counter()
    for edge in edges:
        source, target = edge["source_id"], edge["target_id"]
        weight = max(0.0001, float(edge["weight"]))
        adjacency[source][target] = adjacency[source].get(target, 0.0) + weight
        adjacency[target][source] = adjacency[target].get(source, 0.0) + weight
        degree_out[source] += 1
        degree_in[target] += 1
    return adjacency, degree_in, degree_out


def _communities(adjacency: dict[str, dict[str, float]], max_iterations: int) -> dict[str, str]:
    labels = {node_id: node_id for node_id in adjacency}
    neighbor_sets = {node_id: set(neighbors) for node_id, neighbors in adjacency.items()}
    for _ in range(max(1, max_iterations)):
        changed = False
        for node_id in sorted(adjacency):
            if not adjacency[node_id]:
                continue
            scores: defaultdict[str, float] = defaultdict(float)
            for neighbor, weight in adjacency[node_id].items():
                common = len(neighbor_sets[node_id] & neighbor_sets[neighbor])
                scores[labels[neighbor]] += weight * (1.0 + common)
            current = labels[node_id]
            best = min(scores, key=lambda label: (-scores[label], label))
            if scores[best] > scores.get(current, 0.0) + 1e-12 and best != current:
                labels[node_id] = best
                changed = True
        if not changed:
            break
    # Canonicalize labels so implementation details never leak into stable keys.
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for node_id, label in labels.items():
        groups[label].append(node_id)
    canonical: dict[str, str] = {}
    for members in groups.values():
        community_key = hashlib.sha256("|".join(sorted(members)).encode("utf-8")).hexdigest()[:12]
        for member in members:
            canonical[member] = community_key
    return canonical


def _leiden_communities(
    adjacency: dict[str, dict[str, float]],
    *,
    seed: int,
    resolution: float,
) -> dict[str, str] | None:
    """Run real Leiden when optional native dependencies are available."""
    try:
        import igraph as ig
        import leidenalg
    except ImportError:
        return None
    node_ids = sorted(adjacency)
    index = {node_id: position for position, node_id in enumerate(node_ids)}
    edge_weights: dict[tuple[int, int], float] = {}
    for source in node_ids:
        for target, weight in adjacency[source].items():
            left, right = sorted((index[source], index[target]))
            if left == right:
                continue
            edge_weights[(left, right)] = max(edge_weights.get((left, right), 0.0), float(weight))
    graph = ig.Graph(n=len(node_ids), edges=sorted(edge_weights), directed=False)
    weights = [edge_weights[edge] for edge in sorted(edge_weights)]
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights=weights or None,
        seed=int(seed),
        resolution_parameter=max(0.01, float(resolution)),
    )
    labels: dict[str, str] = {}
    for members in partition:
        member_ids = sorted(node_ids[index_value] for index_value in members)
        key = hashlib.sha256("|".join(member_ids).encode("utf-8")).hexdigest()[:12]
        for node_id in member_ids:
            labels[node_id] = key
    return labels


def _pagerank(adjacency: dict[str, dict[str, float]], iterations: int = 40) -> dict[str, float]:
    count = len(adjacency)
    if not count:
        return {}
    ranks = {node_id: 1.0 / count for node_id in adjacency}
    for _ in range(iterations):
        updated = {node_id: 0.15 / count for node_id in adjacency}
        dangling = sum(ranks[node_id] for node_id, neighbors in adjacency.items() if not neighbors)
        for node_id, neighbors in adjacency.items():
            total = sum(neighbors.values())
            if total:
                for target, weight in neighbors.items():
                    updated[target] += 0.85 * ranks[node_id] * weight / total
        bonus = 0.85 * dangling / count
        ranks = {node_id: value + bonus for node_id, value in updated.items()}
    return ranks


def _betweenness(adjacency: dict[str, dict[str, float]], max_sources: int = 250) -> dict[str, float]:
    nodes = sorted(adjacency)
    if len(nodes) > max_sources:
        stride = max(1, math.ceil(len(nodes) / max_sources))
        sources = nodes[::stride]
    else:
        sources = nodes
    result = {node_id: 0.0 for node_id in nodes}
    for source in sources:
        stack: list[str] = []
        predecessors: defaultdict[str, list[str]] = defaultdict(list)
        paths = {node_id: 0.0 for node_id in nodes}
        paths[source] = 1.0
        distance = {source: 0}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            stack.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in distance:
                    distance[neighbor] = distance[current] + 1
                    queue.append(neighbor)
                if distance[neighbor] == distance[current] + 1:
                    paths[neighbor] += paths[current]
                    predecessors[neighbor].append(current)
        dependency = {node_id: 0.0 for node_id in nodes}
        while stack:
            child = stack.pop()
            for parent in predecessors[child]:
                dependency[parent] += (paths[parent] / paths[child]) * (1.0 + dependency[child])
            if child != source:
                result[child] += dependency[child]
    scale = 1.0 / max(1, len(sources) * max(1, len(nodes) - 1))
    return {node_id: value * scale for node_id, value in result.items()}


def _core_numbers(adjacency: dict[str, dict[str, float]]) -> dict[str, int]:
    remaining = set(adjacency)
    degree = {node_id: len(neighbors) for node_id, neighbors in adjacency.items()}
    core = {node_id: 0 for node_id in adjacency}
    level = 0
    while remaining:
        removable = sorted(node_id for node_id in remaining if degree[node_id] <= level)
        if not removable:
            level += 1
            continue
        for node_id in removable:
            remaining.remove(node_id)
            core[node_id] = level
            for neighbor in adjacency[node_id]:
                if neighbor in remaining:
                    degree[neighbor] -= 1
    return core


def _community_label(members: list[dict[str, Any]], metrics: dict[str, dict[str, Any]]) -> str:
    representative = min(
        members,
        key=lambda item: (-metrics[item["id"]]["hub_score"], item["id"]),
    )
    path = representative.get("relative_path") or ""
    if path:
        parts = PurePosixPath(path).parts
        if len(parts) > 1:
            return "/".join(parts[:2])
    return representative.get("title") or representative["id"]


def analyze_graph(
    graph: dict[str, Any] | None = None,
    *,
    project_id: str,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
    snapshot_id: str | None = None,
    seed: int = 0,
    max_iterations: int = 30,
    resolution: float = 1.0,
    hub_limit: int = 20,
    surprise_limit: int = 20,
) -> dict[str, Any]:
    """Return analytics-only results; callers decide whether and where to persist."""
    normalized_nodes, normalized_edges, source = _normalize_graph(
        graph, project_id=project_id, nodes=nodes, edges=edges
    )
    adjacency, degree_in, degree_out = _adjacency(normalized_nodes, normalized_edges)
    labels = _leiden_communities(adjacency, seed=seed, resolution=resolution)
    algorithm = "leiden" if labels is not None else "deterministic_weighted_label_propagation_fallback"
    labels = labels or _communities(adjacency, max_iterations=max_iterations)
    pagerank = _pagerank(adjacency)
    betweenness = _betweenness(adjacency)
    core = _core_numbers(adjacency)
    max_degree = max((len(value) for value in adjacency.values()), default=1)
    max_rank = max(pagerank.values(), default=1.0)
    max_core = max(core.values(), default=1)
    metrics: dict[str, dict[str, Any]] = {}
    node_by_id = {item["id"]: item for item in normalized_nodes}

    for node_id in sorted(adjacency):
        degree_score = len(adjacency[node_id]) / max(1, max_degree)
        rank_score = pagerank.get(node_id, 0.0) / max(0.000001, max_rank)
        bridge_score = min(1.0, betweenness.get(node_id, 0.0) * 4.0)
        core_score = core.get(node_id, 0) / max(1, max_core)
        hub_score = round(0.40 * degree_score + 0.35 * rank_score + 0.25 * core_score, 6)
        god_score = round(0.45 * hub_score + 0.35 * bridge_score + 0.20 * degree_score, 6)
        metrics[node_id] = {
            "project_id": source["project_id"],
            "entity_id": node_id,
            "degree_in": degree_in[node_id],
            "degree_out": degree_out[node_id],
            "degree": len(adjacency[node_id]),
            "pagerank": round(pagerank.get(node_id, 0.0), 8),
            "betweenness": round(betweenness.get(node_id, 0.0), 8),
            "k_core": core.get(node_id, 0),
            "hub_score": hub_score,
            "god_node_score": god_score,
            "community_key": labels[node_id],
        }

    community_members: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in normalized_nodes:
        community_members[labels[node["id"]]].append(node)
    communities: list[dict[str, Any]] = []
    edge_count = max(1, len(normalized_edges))
    for community_key, members in sorted(community_members.items()):
        member_ids = {item["id"] for item in members}
        internal = sum(1 for edge in normalized_edges if edge["source_id"] in member_ids and edge["target_id"] in member_ids)
        external = sum(
            1
            for edge in normalized_edges
            if (edge["source_id"] in member_ids) != (edge["target_id"] in member_ids)
        )
        possible = max(1, len(member_ids) * max(1, len(member_ids) - 1) / 2)
        representative = min(member_ids, key=lambda item: (-metrics[item]["hub_score"], item))
        degree_sum = sum(len(adjacency[member_id]) for member_id in member_ids)
        memberships = []
        for member_id in sorted(member_ids):
            total_weight = sum(adjacency[member_id].values())
            internal_weight = sum(
                weight for neighbor, weight in adjacency[member_id].items() if neighbor in member_ids
            )
            memberships.append(
                {
                    "project_id": source["project_id"],
                    "entity_id": member_id,
                    "membership_strength": round(internal_weight / max(0.000001, total_weight), 6),
                    "is_representative": member_id == representative,
                }
            )
        communities.append(
            {
                "project_id": source["project_id"],
                "snapshot_id": _text(snapshot_id) or source["snapshot_id"],
                "community_key": community_key,
                "label": _community_label(members, metrics),
                "member_count": len(members),
                "member_ids": sorted(member_ids),
                "members": memberships,
                "representative_id": representative,
                "modularity": round(internal / edge_count - (degree_sum / (2 * edge_count)) ** 2, 6),
                "cohesion": round(min(1.0, internal / possible), 6),
                "conductance": round(external / max(1, 2 * internal + external), 6),
            }
        )

    type_frequency = Counter(edge["relation_type"] for edge in normalized_edges)
    surprising: list[dict[str, Any]] = []
    for edge in normalized_edges:
        source_id, target_id = edge["source_id"], edge["target_id"]
        if labels[source_id] == labels[target_id]:
            continue
        rarity = 1.0 / max(1, type_frequency[edge["relation_type"]])
        importance = (metrics[source_id]["hub_score"] + metrics[target_id]["hub_score"]) / 2
        score = round(min(1.0, 0.45 * edge["confidence"] + 0.30 * rarity + 0.25 * importance), 6)
        surprising.append(
            {
                "project_id": source["project_id"],
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relation_type": edge["relation_type"],
                "surprise_kind": "cross_community_bridge",
                "score": score,
                "confidence": edge["confidence"],
                "explanation": (
                    f"{node_by_id[source_id]['title']} connects community {labels[source_id]} "
                    f"to {node_by_id[target_id]['title']} in community {labels[target_id]} "
                    f"through rare relation {edge['relation_type']}."
                ),
                "evidence": {"edge_id": edge["id"], "relation_frequency": type_frequency[edge["relation_type"]]},
                "review_status": "unreviewed",
            }
        )
    surprising.sort(key=lambda item: (-item["score"], item["source_entity_id"], item["target_entity_id"]))
    ranked_metrics = sorted(metrics.values(), key=lambda item: (-item["hub_score"], item["entity_id"]))
    hubs = [
        {
            **item,
            "classification": (
                "risky_bridge"
                if item["betweenness"] >= 0.15 and item["god_node_score"] >= 0.55
                else "architectural_hub"
                if item["hub_score"] >= 0.55
                else "local_hub"
            ),
            "explanation": (
                f"degree={item['degree']}, pagerank={item['pagerank']:.4f}, "
                f"betweenness={item['betweenness']:.4f}, k_core={item['k_core']}"
            ),
        }
        for item in ranked_metrics[: max(0, hub_limit)]
    ]
    god_nodes = [item for item in hubs if item["god_node_score"] >= 0.60 and item["degree"] >= 2]

    return {
        "status": "success",
        "project_id": source["project_id"],
        "snapshot_id": _text(snapshot_id) or source["snapshot_id"],
        "analysis_channel": "analytics_only",
        "separated_from_retrieval": True,
        "algorithm": algorithm,
        "algorithm_version": ANALYTICS_VERSION,
        "parameters": {
            "seed": int(seed),
            "max_iterations": int(max_iterations),
            "resolution": float(resolution),
        },
        "communities": communities,
        "entity_metrics": ranked_metrics,
        "hubs": hubs,
        "god_nodes": god_nodes,
        "surprising_connections": surprising[: max(0, surprise_limit)],
        "metrics": {
            "nodes": len(normalized_nodes),
            "edges": len(normalized_edges),
            "communities": len(communities),
            "unresolved_edges": source["unresolved_edges"],
        },
    }


graph_analytics = analyze_graph
run_graph_analytics = analyze_graph
