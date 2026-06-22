from __future__ import annotations

import inspect
from pathlib import Path

from MCUM.db import unified_graph_store


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "db" / "schema.sql"


def test_project_health_view_preaggregates_each_source() -> None:
    schema = SCHEMA.read_text(encoding="utf-8", errors="replace")
    view = schema.split("CREATE OR REPLACE VIEW mcum_graph.v_project_health AS", 1)[1]
    view = view.split("CREATE OR REPLACE FUNCTION code_graph.context_pack", 1)[0]

    assert "WITH entity_counts AS" in view
    assert "relation_counts AS" in view
    assert "latest_snapshots AS" in view
    assert "COUNT(DISTINCT e.id)" not in view
    assert "LEFT JOIN mcum_graph.entities e" not in view
    assert "LEFT JOIN mcum_graph.relations r" not in view


def test_runtime_entrypoints_do_not_call_schema_bootstrap() -> None:
    runtime_entrypoints = (
        unified_graph_store.sync_unified_project_graph,
        unified_graph_store.get_unified_graph_health,
        unified_graph_store.query_unified_graph,
        unified_graph_store.find_unified_graph_path,
        unified_graph_store.persist_context_pack,
    )

    for entrypoint in runtime_entrypoints:
        source = inspect.getsource(entrypoint)
        assert "ensure_unified_graph_schema(" not in source


def test_runtime_schema_check_is_read_only() -> None:
    source = inspect.getsource(unified_graph_store._require_unified_graph_schema).upper()

    assert "TO_REGCLASS" in source
    assert "CREATE " not in source
    assert "ALTER " not in source
    assert "DROP " not in source


def test_sync_has_bounded_lock_and_statement_timeouts() -> None:
    source = inspect.getsource(unified_graph_store.sync_unified_project_graph)

    assert "SET LOCAL lock_timeout" in source
    assert "SET LOCAL statement_timeout" in source


class _FakeCursor:
    """Minimal cursor stub that returns queued rows for fetchone()."""

    def __init__(self, rows: list[dict | None]) -> None:
        self._rows = list(rows)
        self.executed: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append(sql)

    def fetchone(self) -> dict | None:
        return self._rows.pop(0) if self._rows else None


def test_code_projection_node_budget_parses_env(monkeypatch) -> None:
    monkeypatch.delenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", raising=False)
    assert unified_graph_store._code_projection_node_budget() == 15000
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "0")
    assert unified_graph_store._code_projection_node_budget() == 0
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "abc")
    assert unified_graph_store._code_projection_node_budget() == 15000


def test_oversized_graph_skips_code_projection(monkeypatch) -> None:
    # Merged-sibling root (node count over budget): skip the heavy code
    # projection so the federated sync completes instead of timing out.
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "15000")
    cur = _FakeCursor([{"n": 28381}])
    result = unified_graph_store._should_project_code(
        cur, "11111111-1111-1111-1111-111111111111", {"status": "success"}
    )
    assert result is False
    assert len(cur.executed) == 1  # only the node-count probe, nothing heavier


def test_small_graph_with_changes_projects_code(monkeypatch) -> None:
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "15000")
    cur = _FakeCursor([{"n": 120}])
    result = unified_graph_store._should_project_code(
        cur, "11111111-1111-1111-1111-111111111111", {"status": "success"}
    )
    assert result is True


def test_budget_zero_disables_guard(monkeypatch) -> None:
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "0")
    cur = _FakeCursor([])  # no count query should run when guard disabled
    result = unified_graph_store._should_project_code(
        cur, "11111111-1111-1111-1111-111111111111", {"status": "success"}
    )
    assert result is True
    assert cur.executed == []


def test_bootstrap_source_does_not_drop_constraints() -> None:
    module_source = inspect.getsource(unified_graph_store.ensure_unified_graph_schema).upper()
    schema_source = SCHEMA.read_text(encoding="utf-8", errors="replace").upper()

    assert "DROP CONSTRAINT" not in module_source
    assert "DROP CONSTRAINT IF EXISTS ENTITIES_PROJECT_ID_CANONICAL_KEY_KEY" not in schema_source


def test_relation_projection_prefilters_unchanged_candidates_before_conflict() -> None:
    source = inspect.getsource(unified_graph_store._upsert_relations)

    assert source.count("WITH candidates AS MATERIALIZED") >= 3
    assert source.count("LEFT JOIN mcum_graph.relations existing") >= 3
    assert source.count("existing.valid_to IS NOT NULL") >= 3
    assert "ON CONFLICT (project_id, source_entity_id, target_entity_id, relation_type)" in source
