from __future__ import annotations

import json

import pytest

from MCUM.core.graph_exports import (
    export_graph,
    export_graph_html,
    export_graph_json,
    export_graph_ndjson,
    export_mermaid_call_flow,
    export_wiki_markdown,
)


def _graph() -> dict:
    return {
        "project_id": "project-1",
        "snapshot_id": "snapshot-1",
        "nodes": [
            {
                "id": "a",
                "project_id": "project-1",
                "title": "</script><script>alert(1)</script>",
                "entity_type": "function",
                "relative_path": "src/a.py",
                "metadata": {"api_token": "do-not-export", "owner": "team-a"},
            },
            {
                "id": "b",
                "project_id": "project-1",
                "title": 'worker "quoted"',
                "entity_type": "function",
                "relative_path": "src/b.py",
            },
            {
                "id": "c",
                "project_id": "project-1",
                "title": "unused",
                "entity_type": "function",
                "relative_path": "src/c.py",
            },
        ],
        "edges": [
            {
                "project_id": "project-1",
                "source_id": "a",
                "target_id": "b",
                "relation_type": "CALLS",
                "confidence": 0.9,
            },
            {
                "project_id": "project-1",
                "source_id": "b",
                "target_id": "c",
                "relation_type": "DEPENDS_ON",
                "confidence": 0.8,
            },
        ],
    }


def test_json_and_ndjson_exports_are_deterministic_redacted_and_budgeted() -> None:
    first = export_graph_json(_graph(), project_id="project-1", max_nodes=2, max_edges=10)
    second = export_graph_json(_graph(), project_id="project-1", max_nodes=2, max_edges=10)
    payload = json.loads(first)

    assert first == second
    assert payload["project_id"] == "project-1"
    assert payload["budget"]["truncated"] is True
    assert len(payload["nodes"]) == 2
    assert payload["nodes"][0]["metadata"]["api_token"] == "[redacted]"
    records = [json.loads(line) for line in export_graph_ndjson(_graph(), project_id="project-1").splitlines()]
    assert records[0]["record_type"] == "manifest"
    assert {record["project_id"] for record in records} == {"project-1"}
    bounded = [
        json.loads(line)
        for line in export_graph_ndjson(_graph(), project_id="project-1", max_bytes=300).splitlines()
    ]
    assert bounded[0]["budget"]["byte_truncated"] is True


def test_wiki_mermaid_and_dispatch_exports_are_pure() -> None:
    wiki = export_wiki_markdown(_graph(), project_id="project-1")
    mermaid = export_mermaid_call_flow(_graph(), project_id="project-1")

    assert wiki.startswith("# Graph Wiki: project-1")
    assert "## Relations" in wiki
    assert "</script>" not in wiki
    assert "&lt;/script&gt;" in wiki
    assert mermaid.startswith("flowchart LR")
    assert "CALLS" in mermaid
    assert "DEPENDS_ON" not in mermaid
    assert export_graph(_graph(), project_id="project-1", export_format="wiki") == wiki


def test_html_export_is_self_contained_sanitized_and_budgeted() -> None:
    html = export_graph_html(_graph(), project_id="project-1", max_nodes=2, max_edges=1)

    assert "<!doctype html>" in html
    assert "Content-Security-Policy" in html
    assert "https://" not in html
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html
    assert '"truncated":true' in html


def test_exports_reject_cross_project_mixing_and_unknown_format() -> None:
    graph = _graph()
    graph["edges"][0]["project_id"] = "project-2"
    with pytest.raises(ValueError, match="edge belongs to project_id"):
        export_graph_json(graph, project_id="project-1")
    with pytest.raises(ValueError, match="unsupported export_format"):
        export_graph(_graph(), project_id="project-1", export_format="xml")
