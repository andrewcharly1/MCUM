from __future__ import annotations

import pytest

from MCUM.core.graph_impact import analyze_impact, select_impacted_tests


def _graph() -> dict:
    return {
        "project_id": "project-1",
        "snapshot_id": "snapshot-1",
        "nodes": [
            {
                "id": "auth.login",
                "project_id": "project-1",
                "title": "auth.login",
                "relative_path": "src/auth.py",
                "entity_type": "function",
                "metadata": {"criticality": "security"},
            },
            {
                "id": "session.validate",
                "project_id": "project-1",
                "title": "session.validate",
                "relative_path": "src/session.py",
                "entity_type": "function",
            },
            {
                "id": "test_login",
                "project_id": "project-1",
                "title": "test_login",
                "relative_path": "tests/test_auth.py",
                "entity_type": "test_case",
            },
            {
                "id": "test_unrelated",
                "project_id": "project-1",
                "title": "test_unrelated",
                "relative_path": "tests/test_other.py",
                "entity_type": "test_case",
            },
        ],
        "edges": [
            {
                "project_id": "project-1",
                "source_id": "auth.login",
                "target_id": "session.validate",
                "relation_type": "CALLS",
                "confidence": 0.95,
            },
            {
                "project_id": "project-1",
                "source_id": "test_login",
                "target_id": "auth.login",
                "relation_type": "TESTS",
                "confidence": 1.0,
            },
        ],
    }


def test_impact_walks_bounded_graph_scores_risk_and_selects_tests() -> None:
    result = analyze_impact(
        _graph(),
        project_id="project-1",
        changed_paths=["src/auth.py"],
        max_depth=2,
    )

    impacted = {item["entity_id"]: item for item in result["impact_items"]}
    assert impacted["auth.login"]["risk_score"] == 1.0
    assert impacted["session.validate"]["distance"] == 1
    assert "outgoing dependency" in impacted["session.validate"]["reason"]
    assert result["test_selection"]["mode"] == "targeted"
    assert [item["test_entity_id"] for item in result["test_selection"]["tests"]] == ["test_login"]
    assert result["project_id"] == "project-1"


def test_impact_uses_full_suite_when_change_resolution_is_uncertain() -> None:
    result = analyze_impact(
        _graph(),
        project_id="project-1",
        changed_entities=["missing.symbol"],
        max_depth=1,
    )

    assert result["test_selection"]["mode"] == "full_suite"
    assert {item["test_entity_id"] for item in result["test_selection"]["tests"]} == {
        "test_login",
        "test_unrelated",
    }
    assert result["changed"]["unresolved"] == ["missing.symbol"]
    assert result["test_selection"]["fallback_reasons"]


def test_mandatory_spec_test_is_always_selected() -> None:
    selection = select_impacted_tests(
        _graph(),
        project_id="project-1",
        changed_entities=["auth.login"],
        spec_contract={"mandatory_test_ids": ["test_unrelated"]},
    )

    by_id = {item["test_entity_id"]: item for item in selection["tests"]}
    assert by_id["test_unrelated"]["required"] is True
    assert "required by active spec contract" in by_id["test_unrelated"]["reason"]


def test_impact_requires_a_change_input() -> None:
    with pytest.raises(ValueError, match="changed_paths or changed_entities is required"):
        analyze_impact(_graph(), project_id="project-1")


def test_impact_falls_back_when_test_inventory_is_unavailable() -> None:
    graph = _graph()
    graph["nodes"] = [item for item in graph["nodes"] if item["entity_type"] != "test_case"]
    graph["edges"] = [item for item in graph["edges"] if item["relation_type"] != "TESTS"]

    result = analyze_impact(graph, project_id="project-1", changed_entities=["auth.login"])

    assert result["test_selection"]["mode"] == "full_suite"
    assert "test inventory unavailable" in result["test_selection"]["fallback_reasons"]
