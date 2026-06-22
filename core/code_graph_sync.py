"""Incremental code graph synchronization used by MCUM session hooks."""

from __future__ import annotations

import time
from typing import Any

from .code_graph_indexer import EXTRACTOR_VERSION, scan_project_code_graph
from .graph_policy import load_graph_policy
from ..db.code_graph_store import (
    backfill_experience_code_links,
    ensure_code_graph_schema,
    get_code_graph_manifest,
    mark_code_graph_stale,
    persist_index_result,
)


def sync_project_code_graph(
    *,
    project_id: str,
    project_path: str,
    project_name: str | None,
    policy: dict[str, Any] | None = None,
    trigger: str = "manual",
    force_full: bool = False,
) -> dict[str, Any]:
    """Detect source changes and persist only the affected graph fragment."""
    resolved = dict(policy or {})
    if not bool(resolved.get("enabled", True)):
        return {"status": "disabled", "trigger": trigger}

    started = time.perf_counter()
    try:
        ensure_code_graph_schema()
        manifest = get_code_graph_manifest(project_id=project_id, ensure_schema=False)
        graph = dict(manifest.get("graph") or {})
        previous_files = dict(manifest.get("files") or {})
        extractor_changed = bool(graph) and str(graph.get("extractor_version") or "") != EXTRACTOR_VERSION
        full_refresh = force_full or not graph or extractor_changed
        graph_policy = load_graph_policy()
        max_scan_files = resolved.get("max_scan_files")
        max_scan_seconds = resolved.get("max_scan_seconds")
        scan_result = scan_project_code_graph(
            project_path,
            excluded_dirs=list(resolved.get("excluded_dirs") or []),
            max_file_bytes=int(resolved.get("max_file_bytes") or 1_000_000),
            previous_manifest=None if full_refresh else previous_files,
            tree_sitter_enabled=bool(graph_policy.features.tree_sitter),
            tree_sitter_languages=list(graph_policy.priority_languages),
            tree_sitter_max_nodes=int(graph_policy.budgets.analytics.max_nodes),
            max_files=int(max_scan_files) if max_scan_files else None,
            max_seconds=float(max_scan_seconds) if max_scan_seconds else None,
        )
        persist_result = persist_index_result(
            project_id=project_id,
            project_path=project_path,
            project_name=project_name,
            mode="full" if full_refresh else "incremental",
            index_result=scan_result,
            ensure_schema=False,
        )
        backfill = {"status": "skipped", "links_created": 0}
        if persist_result.get("status") != "no_changes":
            backfill = backfill_experience_code_links(
                project_id=project_id,
                limit=int(resolved.get("experience_backfill_limit") or 500),
                ensure_schema=False,
            )
        return {
            **persist_result,
            "trigger": trigger,
            "mode": "full" if full_refresh else "incremental",
            "extractor_changed": extractor_changed,
            "scan_stats": dict(scan_result.get("stats") or {}),
            "backfill": backfill,
            "wall_clock_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        try:
            mark_code_graph_stale(project_id=project_id, error_message=str(exc))
        except Exception:
            pass
        return {
            "status": "failure",
            "trigger": trigger,
            "error": str(exc),
            "wall_clock_ms": int((time.perf_counter() - started) * 1000),
        }
