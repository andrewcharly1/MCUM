from __future__ import annotations

import json

from MCUM.core.graph_policy import DEFAULT_GRAPH_POLICY, load_graph_policy, validate_graph_policy


def test_runtime_graph_policy_enables_governed_capabilities() -> None:
    policy = load_graph_policy()

    assert all(value is True for value in policy.features.__dict__.values())
    assert policy.query.default_depth <= policy.query.max_depth
    assert policy.query.default_page_size <= policy.query.max_page_size
    assert policy.budgets.cross_project.max_projects == 1


def test_policy_loader_tolerates_invalid_types_and_clamps_limits(tmp_path) -> None:
    path = tmp_path / "graph_policy.json"
    path.write_text(
        json.dumps(
            {
                "features": {"analytics": "yes", "impact": True},
                "priority_languages": [],
                "query": {"max_depth": 999, "max_nodes": -4},
                "budgets": {"exports": {"max_nodes": 999999999}},
            }
        ),
        encoding="utf-8",
    )

    policy = load_graph_policy(path)

    assert policy.features.analytics is False
    assert policy.features.impact is True
    assert policy.query.max_depth == 8
    assert policy.query.max_nodes == 1
    assert policy.budgets.exports.max_nodes == 1000000
    assert policy.priority_languages == DEFAULT_GRAPH_POLICY.priority_languages
    assert policy.warnings


def test_policy_loader_uses_safe_defaults_for_malformed_json(tmp_path) -> None:
    path = tmp_path / "graph_policy.json"
    path.write_text("{invalid", encoding="utf-8")

    policy = load_graph_policy(path)

    assert policy.features == DEFAULT_GRAPH_POLICY.features
    assert policy.query == DEFAULT_GRAPH_POLICY.query
    assert "safe defaults applied" in policy.warnings[0]


def test_policy_validation_repairs_inconsistent_default_limits() -> None:
    policy = validate_graph_policy(
        {
            "query": {
                "default_depth": 6,
                "max_depth": 2,
                "default_page_size": 80,
                "max_page_size": 20,
            }
        }
    )

    assert policy.query.max_depth == 6
    assert policy.query.max_page_size == 80
    assert policy.to_dict()["features"]["cross_project"] is False
