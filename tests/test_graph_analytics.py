from __future__ import annotations

import pytest

from MCUM.core.graph_analytics import analyze_graph


def _graph() -> dict:
    nodes = [
        {"id": node_id, "project_id": "project-1", "title": node_id, "relative_path": path}
        for node_id, path in (
            ("a", "src/auth/a.py"),
            ("b", "src/auth/b.py"),
            ("c", "src/auth/c.py"),
            ("d", "src/billing/d.py"),
            ("e", "src/billing/e.py"),
            ("f", "src/billing/f.py"),
        )
    ]
    pairs = [
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),
        ("d", "e"),
        ("e", "f"),
        ("f", "d"),
        ("c", "d"),
    ]
    edges = [
        {
            "id": f"{source}-{target}",
            "project_id": "project-1",
            "source_id": source,
            "target_id": target,
            "relation_type": "CALLS" if (source, target) != ("c", "d") else "USES_REMOTE",
            "confidence": 1.0,
        }
        for source, target in pairs
    ]
    return {"project_id": "project-1", "snapshot_id": "snapshot-1", "nodes": nodes, "edges": edges}


def test_analytics_is_deterministic_explainable_and_separate_from_retrieval() -> None:
    first = analyze_graph(_graph(), project_id="project-1", seed=7)
    second = analyze_graph(_graph(), project_id="project-1", seed=7)

    assert first == second
    assert first["project_id"] == "project-1"
    assert first["snapshot_id"] == "snapshot-1"
    assert first["analysis_channel"] == "analytics_only"
    assert first["separated_from_retrieval"] is True
    assert len(first["communities"]) == 2
    assert all("modularity" in community for community in first["communities"])
    assert all(community["members"] for community in first["communities"])
    assert first["hubs"][0]["explanation"].startswith("degree=")
    bridge = first["surprising_connections"][0]
    assert {bridge["source_entity_id"], bridge["target_entity_id"]} == {"c", "d"}
    assert bridge["surprise_kind"] == "cross_community_bridge"
    assert "community" in bridge["explanation"]


def test_analytics_rejects_cross_project_mixing() -> None:
    graph = _graph()
    graph["nodes"][0]["project_id"] = "project-2"

    with pytest.raises(ValueError, match="node belongs to project_id"):
        analyze_graph(graph, project_id="project-1")


def test_analytics_requires_project_id() -> None:
    with pytest.raises(ValueError, match="project_id is required"):
        analyze_graph({"nodes": [], "edges": []}, project_id="")
