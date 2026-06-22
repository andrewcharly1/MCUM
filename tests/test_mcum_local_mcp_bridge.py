from __future__ import annotations

from pathlib import Path


BRIDGE_FILE = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "antigravity"
    / "mcum_local_mcp_stdio.mjs"
)


def test_mcum_local_bridge_exposes_worker_delegation_tool() -> None:
    source = BRIDGE_FILE.read_text(encoding="utf-8")

    assert 'name: "mcum_delegate_worker_task"' in source
    assert "async function mcumDelegateWorkerTask" in source
    assert "mcum_delegate_worker_task: mcumDelegateWorkerTask" in source
    assert '"gemini-cli"' in source
    assert '"spreadsheet-extractor"' in source


def test_mcum_local_bridge_exposes_filtered_code_graph_query() -> None:
    source = BRIDGE_FILE.read_text(encoding="utf-8")

    assert 'name: "mcum_code_graph_query"' in source
    assert 'addIfValue(args, "--path-prefix", input.path_prefix)' in source
    assert 'addIfValue(args, "--language", value)' in source
    assert 'addIfValue(args, "--node-kind", value)' in source


def test_mcum_local_bridge_recent_activity_uses_existing_tables() -> None:
    # Regression: recent_activity queried project_registry.sessions and
    # project_registry.logs, which do not exist (sessions/logs are rows in
    # project_registry.project_logs). Those queries raised at runtime.
    source = BRIDGE_FILE.read_text(encoding="utf-8")

    assert "FROM project_registry.sessions" not in source
    assert "FROM project_registry.logs " not in source
    assert (
        "FROM project_registry.project_logs WHERE log_type IN "
        "('session_start','session_end')" in source
    )
    assert "log_type, title, outcome, created_at FROM project_registry.project_logs" in source


def test_mcum_local_bridge_exposes_federated_graph_tools() -> None:
    source = BRIDGE_FILE.read_text(encoding="utf-8")

    assert 'name: "mcum_graph_sync"' in source
    assert 'name: "mcum_graph_query"' in source
    assert 'name: "mcum_graph_health"' in source
    assert 'name: "mcum_graph_get_node"' in source
    assert 'name: "mcum_graph_neighbors"' in source
    assert 'name: "mcum_graph_explain"' in source
    assert 'name: "mcum_graph_impact"' in source
    assert "mcum_graph_sync: mcumGraphSync" in source
    assert "mcum_graph_query: mcumGraphQuery" in source
    assert "mcum_graph_health: mcumGraphHealth" in source
    assert "mcum_graph_get_node: mcumGraphGetNode" in source
    assert "mcum_graph_neighbors: mcumGraphNeighbors" in source
    assert "mcum_graph_explain: mcumGraphExplain" in source
    assert "mcum_graph_impact: mcumGraphImpact" in source
