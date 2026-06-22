"""Explicit, traceable comparison of two project or snapshot graphs."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from .graph_analytics import _normalize_graph, _text


COMPARE_VERSION = "mcum-graph-compare-v1"


def _normalized_path(value: Any) -> str:
    path = _text(value).replace("\\", "/").lower().strip("/")
    parts = PurePosixPath(path).parts
    while parts and parts[0] in {"src", "app", "lib", "source"}:
        parts = parts[1:]
    return "/".join(parts)


def _ratio(left: Any, right: Any) -> float:
    left_text, right_text = _text(left).lower(), _text(right).lower()
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def _similarity(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, dict[str, float]]:
    canonical = 1.0 if left["canonical_key"] == right["canonical_key"] else _ratio(left["canonical_key"], right["canonical_key"])
    signature = _ratio(left.get("signature"), right.get("signature"))
    path = _ratio(_normalized_path(left.get("relative_path")), _normalized_path(right.get("relative_path")))
    title = _ratio(left.get("qualified_name") or left.get("title"), right.get("qualified_name") or right.get("title"))
    kind = 1.0 if left.get("entity_type") == right.get("entity_type") else 0.0
    score = 0.30 * canonical + 0.25 * signature + 0.20 * path + 0.15 * title + 0.10 * kind
    evidence = {
        "canonical_key": round(canonical, 6),
        "signature": round(signature, 6),
        "normalized_path": round(path, 6),
        "title": round(title, 6),
        "entity_type": kind,
    }
    return round(score, 6), evidence


def _fingerprint(node: dict[str, Any]) -> str:
    material = {
        "entity_type": node.get("entity_type"),
        "canonical_key": node.get("canonical_key"),
        "relative_path": _normalized_path(node.get("relative_path")),
        "signature": node.get("signature"),
        "title": node.get("title"),
        "metadata": node.get("metadata"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _match_payload(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_project_id: str,
    right_project_id: str,
    match_type: str,
    similarity: float,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "left_project_id": left_project_id,
        "right_project_id": right_project_id,
        "left_entity_id": left["id"],
        "right_entity_id": right["id"],
        "match_type": match_type,
        "similarity": round(similarity, 6),
        "summary": f"{left['title']} -> {right['title']}",
        "evidence": evidence,
    }


def _analytics_diff(left_graph: dict[str, Any], right_graph: dict[str, Any]) -> dict[str, Any]:
    def summary(graph: dict[str, Any]) -> dict[str, Any]:
        communities = list(graph.get("communities") or [])
        hubs = list(graph.get("hubs") or [])
        return {
            "community_count": len(communities),
            "community_labels": sorted(_text(item.get("label")) for item in communities if _text(item.get("label"))),
            "hub_ids": sorted(_text(item.get("entity_id")) for item in hubs if _text(item.get("entity_id"))),
        }

    left, right = summary(left_graph), summary(right_graph)
    return {
        "left": left,
        "right": right,
        "community_count_delta": right["community_count"] - left["community_count"],
        "added_hubs": sorted(set(right["hub_ids"]) - set(left["hub_ids"])),
        "removed_hubs": sorted(set(left["hub_ids"]) - set(right["hub_ids"])),
    }


def compare_graphs(
    left_graph: dict[str, Any],
    right_graph: dict[str, Any],
    *,
    left_project_id: str,
    right_project_id: str,
    left_snapshot_id: str | None = None,
    right_snapshot_id: str | None = None,
    probable_threshold: float = 0.72,
    ambiguity_margin: float = 0.04,
) -> dict[str, Any]:
    """Compare graphs only when both project scopes are explicitly supplied."""
    left_nodes, left_edges, left_source = _normalize_graph(left_graph, project_id=left_project_id)
    right_nodes, right_edges, right_source = _normalize_graph(right_graph, project_id=right_project_id)
    threshold = max(0.0, min(1.0, float(probable_threshold)))
    margin = max(0.0, min(0.25, float(ambiguity_margin)))
    right_by_canonical: dict[str, list[dict[str, Any]]] = {}
    for node in right_nodes:
        right_by_canonical.setdefault(node["canonical_key"], []).append(node)
    exact: list[dict[str, Any]] = []
    probable: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    matched_left: set[str] = set()
    matched_right: set[str] = set()
    match_map: dict[str, str] = {}

    for left in left_nodes:
        candidates = [item for item in right_by_canonical.get(left["canonical_key"], []) if item["id"] not in matched_right]
        if len(candidates) == 1:
            right = candidates[0]
            payload = _match_payload(
                left,
                right,
                left_project_id=left_source["project_id"],
                right_project_id=right_source["project_id"],
                match_type="exact",
                similarity=1.0,
                evidence={"rule": "canonical_key", "canonical_key": left["canonical_key"]},
            )
            exact.append(payload)
            matched_left.add(left["id"])
            matched_right.add(right["id"])
            match_map[left["id"]] = right["id"]
        elif len(candidates) > 1:
            ambiguous.append(
                {
                    "left_project_id": left_source["project_id"],
                    "right_project_id": right_source["project_id"],
                    "left_entity_id": left["id"],
                    "match_type": "ambiguous",
                    "similarity": 1.0,
                    "candidate_right_entity_ids": sorted(item["id"] for item in candidates),
                    "summary": f"Multiple exact canonical matches for {left['title']}",
                    "evidence": {"rule": "duplicate_canonical_key", "canonical_key": left["canonical_key"]},
                }
            )
            matched_left.add(left["id"])

    for left in (item for item in left_nodes if item["id"] not in matched_left):
        scores: list[tuple[float, str, dict[str, float], dict[str, Any]]] = []
        for right in right_nodes:
            if right["id"] in matched_right:
                continue
            score, evidence = _similarity(left, right)
            if score >= threshold:
                scores.append((score, right["id"], evidence, right))
        scores.sort(key=lambda item: (-item[0], item[1]))
        if not scores:
            continue
        top = scores[0]
        competing = [item for item in scores if top[0] - item[0] <= margin]
        if len(competing) > 1:
            ambiguous.append(
                {
                    "left_project_id": left_source["project_id"],
                    "right_project_id": right_source["project_id"],
                    "left_entity_id": left["id"],
                    "match_type": "ambiguous",
                    "similarity": top[0],
                    "candidate_right_entity_ids": [item[1] for item in competing],
                    "summary": f"Several probable matches are too close for {left['title']}",
                    "evidence": {"candidates": [{"right_entity_id": item[1], **item[2]} for item in competing]},
                }
            )
            matched_left.add(left["id"])
            continue
        right = top[3]
        probable.append(
            _match_payload(
                left,
                right,
                left_project_id=left_source["project_id"],
                right_project_id=right_source["project_id"],
                match_type="probable",
                similarity=top[0],
                evidence={"rule": "weighted_similarity", **top[2]},
            )
        )
        matched_left.add(left["id"])
        matched_right.add(right["id"])
        match_map[left["id"]] = right["id"]

    left_by_id = {item["id"]: item for item in left_nodes}
    right_by_id = {item["id"]: item for item in right_nodes}
    all_matches = exact + probable
    changed: list[dict[str, Any]] = []
    for match in all_matches:
        left, right = left_by_id[match["left_entity_id"]], right_by_id[match["right_entity_id"]]
        if _fingerprint(left) != _fingerprint(right):
            changed_fields = [
                field
                for field in ("entity_type", "relative_path", "signature", "title", "metadata")
                if left.get(field) != right.get(field)
            ]
            changed.append(
                {
                    **match,
                    "item_kind": "entity_changed",
                    "severity": "high" if "signature" in changed_fields else "medium",
                    "changed_fields": changed_fields,
                }
            )

    removed = [
        {
            "project_id": left_source["project_id"],
            "entity_id": node["id"],
            "title": node["title"],
            "entity_type": node["entity_type"],
        }
        for node in left_nodes
        if node["id"] not in matched_left
    ]
    added = [
        {
            "project_id": right_source["project_id"],
            "entity_id": node["id"],
            "title": node["title"],
            "entity_type": node["entity_type"],
        }
        for node in right_nodes
        if node["id"] not in matched_right
    ]

    def edge_key(edge: dict[str, Any], mapping: dict[str, str] | None = None) -> tuple[str, str, str]:
        source = (mapping or {}).get(edge["source_id"], edge["source_id"])
        target = (mapping or {}).get(edge["target_id"], edge["target_id"])
        return source, target, edge["relation_type"]

    left_relation_keys = {edge_key(edge, match_map): edge for edge in left_edges if edge["source_id"] in match_map and edge["target_id"] in match_map}
    right_relation_keys = {edge_key(edge): edge for edge in right_edges}
    relations_added = [
        {
            "project_id": right_source["project_id"],
            "source_entity_id": key[0],
            "target_entity_id": key[1],
            "relation_type": key[2],
        }
        for key in sorted(set(right_relation_keys) - set(left_relation_keys))
    ]
    relations_removed = [
        {
            "project_id": left_source["project_id"],
            "source_entity_id": edge["source_id"],
            "target_entity_id": edge["target_id"],
            "relation_type": edge["relation_type"],
        }
        for key, edge in sorted(left_relation_keys.items())
        if key not in right_relation_keys
    ]
    changed.sort(key=lambda item: (item["left_entity_id"], item["right_entity_id"]))
    exact.sort(key=lambda item: item["left_entity_id"])
    probable.sort(key=lambda item: item["left_entity_id"])
    ambiguous.sort(key=lambda item: item["left_entity_id"])
    added.sort(key=lambda item: item["entity_id"])
    removed.sort(key=lambda item: item["entity_id"])
    type_counts_left = Counter(node["entity_type"] for node in left_nodes)
    type_counts_right = Counter(node["entity_type"] for node in right_nodes)
    contract_types = {"spec_contract", "api_contract", "contract", "database"}
    test_types = {"test", "test_case", "test_suite", "unit_test", "integration_test", "e2e_test"}

    return {
        "status": "success",
        "algorithm_version": COMPARE_VERSION,
        "left_project_id": left_source["project_id"],
        "right_project_id": right_source["project_id"],
        "comparison_scope": {
            "explicit": True,
            "cross_project": left_source["project_id"] != right_source["project_id"],
            "left_project_id": left_source["project_id"],
            "right_project_id": right_source["project_id"],
            "left_snapshot_id": _text(left_snapshot_id) or left_source["snapshot_id"],
            "right_snapshot_id": _text(right_snapshot_id) or right_source["snapshot_id"],
        },
        "matches": {"exact": exact, "probable": probable, "ambiguous": ambiguous},
        "entities": {"added": added, "removed": removed, "changed": changed},
        "relations": {"added": relations_added, "removed": relations_removed},
        "contract_changes": [item for item in changed if left_by_id[item["left_entity_id"]]["entity_type"] in contract_types],
        "test_changes": [item for item in changed if left_by_id[item["left_entity_id"]]["entity_type"] in test_types],
        "analytics_differences": _analytics_diff(left_graph, right_graph),
        "metrics": {
            "left_nodes": len(left_nodes),
            "right_nodes": len(right_nodes),
            "exact_matches": len(exact),
            "probable_matches": len(probable),
            "ambiguous_matches": len(ambiguous),
            "added_entities": len(added),
            "removed_entities": len(removed),
            "changed_entities": len(changed),
            "left_entity_types": dict(sorted(type_counts_left.items())),
            "right_entity_types": dict(sorted(type_counts_right.items())),
        },
    }


compare_snapshots = compare_graphs
compare_projects = compare_graphs
graph_compare = compare_graphs
