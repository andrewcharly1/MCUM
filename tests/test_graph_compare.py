from __future__ import annotations

import pytest

from MCUM.core.graph_compare import compare_graphs


def _left() -> dict:
    return {
        "project_id": "left-project",
        "snapshot_id": "left-snapshot",
        "nodes": [
            {
                "id": "left-auth",
                "project_id": "left-project",
                "canonical_key": "symbol:auth.login",
                "title": "auth.login",
                "entity_type": "function",
                "signature": "login(user)",
                "relative_path": "src/auth.py",
            },
            {
                "id": "left-service",
                "project_id": "left-project",
                "canonical_key": "old:billing.service",
                "title": "billing.service",
                "entity_type": "function",
                "signature": "charge(invoice)",
                "relative_path": "src/billing/service.py",
            },
            {
                "id": "left-worker",
                "project_id": "left-project",
                "canonical_key": "old:worker",
                "title": "worker",
                "entity_type": "function",
                "signature": "run(job)",
                "relative_path": "src/jobs/worker.py",
            },
        ],
        "edges": [
            {
                "project_id": "left-project",
                "source_id": "left-auth",
                "target_id": "left-service",
                "relation_type": "CALLS",
            }
        ],
    }


def _right() -> dict:
    return {
        "project_id": "right-project",
        "snapshot_id": "right-snapshot",
        "nodes": [
            {
                "id": "right-auth",
                "project_id": "right-project",
                "canonical_key": "symbol:auth.login",
                "title": "auth.login",
                "entity_type": "function",
                "signature": "login(user, otp)",
                "relative_path": "src/auth.py",
            },
            {
                "id": "right-service",
                "project_id": "right-project",
                "canonical_key": "new:billing.service",
                "title": "billing.service",
                "entity_type": "function",
                "signature": "charge(invoice)",
                "relative_path": "lib/billing/service.py",
            },
            {
                "id": "right-worker-a",
                "project_id": "right-project",
                "canonical_key": "new:worker:a",
                "title": "worker",
                "entity_type": "function",
                "signature": "run(job)",
                "relative_path": "lib/jobs/worker.py",
            },
            {
                "id": "right-worker-b",
                "project_id": "right-project",
                "canonical_key": "new:worker:b",
                "title": "worker",
                "entity_type": "function",
                "signature": "run(job)",
                "relative_path": "app/jobs/worker.py",
            },
        ],
        "edges": [],
    }


def test_compare_exposes_exact_probable_and_ambiguous_matches() -> None:
    result = compare_graphs(
        _left(),
        _right(),
        left_project_id="left-project",
        right_project_id="right-project",
        probable_threshold=0.60,
    )

    assert result["comparison_scope"]["explicit"] is True
    assert result["comparison_scope"]["cross_project"] is True
    assert result["left_project_id"] == "left-project"
    assert result["right_project_id"] == "right-project"
    assert result["matches"]["exact"][0]["left_entity_id"] == "left-auth"
    assert result["matches"]["probable"][0]["left_entity_id"] == "left-service"
    assert result["matches"]["ambiguous"][0]["left_entity_id"] == "left-worker"
    assert len(result["matches"]["ambiguous"][0]["candidate_right_entity_ids"]) == 2
    assert result["entities"]["changed"][0]["severity"] == "high"
    assert result["relations"]["removed"][0]["relation_type"] == "CALLS"


def test_compare_rejects_graph_scope_mismatch() -> None:
    with pytest.raises(ValueError, match="graph project_id"):
        compare_graphs(
            _left(),
            _right(),
            left_project_id="wrong-left",
            right_project_id="right-project",
        )
