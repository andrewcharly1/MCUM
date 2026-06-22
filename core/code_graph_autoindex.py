"""
Gated automatic code-graph freshness + ensure logic for MCUM.

Goal: before any analysis on a repository, make sure the PostgreSQL code graph
exists and is reasonably fresh -- *without ever blocking intake* on a huge
first-time scan.

Design principles:
  * The freshness decision is cheap (git fingerprint, or a stat-only walk that
    prunes the same vendored directories the indexer skips).
  * Re-indexing is incremental (only changed files), reusing the existing
    sync_project_code_graph pipeline.
  * A large *first* build is deferred with a clear recommendation instead of
    being run inline, so the agent is never stuck waiting minutes.
  * The pure decision helpers (is_code_relevant / compute_source_fingerprint /
    decide_action) carry no DB or filesystem mutation so they can be unit
    tested in isolation. ensure_code_graph is the thin side-effecting wrapper.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..db.connection import get_db, get_cursor
from .code_graph_indexer import DEFAULT_EXCLUDED_DIRS, LANGUAGE_BY_SUFFIX

FINGERPRINT_VERSION = "mcum-autograph-fp-v1"

# Task types that do NOT benefit from a code graph. MCUM task types are written
# in Spanish; everything *not* in this denylist is treated as code-relevant
# because MCUM is meant to audit broadly.
NON_CODE_TASK_TYPES = {
    "documentar",
    "redactar",
    "responder",
    "consultar",
    "conversar",
}

DEFAULT_MAX_FINGERPRINT_FILES = 20000
DEFAULT_MAX_FIRST_BUILD_FILES = 4000
DEFAULT_MAX_FIRST_BUILD_BYTES = 80 * 1024 * 1024  # 80 MB of *source* (vendored excluded)

SOURCE_SUFFIXES = set(LANGUAGE_BY_SUFFIX)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _no_window_kwargs() -> dict[str, Any]:
    """Keep subprocess children from flashing a console window on Windows."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


# ---------------------------------------------------------------------------
# Pure helpers (no DB, no mutation) -- unit testable
# ---------------------------------------------------------------------------
def is_code_relevant(task_type: str | None, *, force: bool = False) -> bool:
    """Whether a task warrants ensuring the code graph is fresh."""
    if force:
        return True
    if not task_type:
        return True
    return str(task_type).strip().lower() not in NON_CODE_TASK_TYPES


def _git_fingerprint(root: Path) -> str | None:
    """Precise + fast fingerprint via git HEAD plus working-tree status."""
    if not (root / ".git").exists():
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, **_no_window_kwargs(),
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20, **_no_window_kwargs(),
        )
        head_sha = head.stdout.strip()
        status_hash = hashlib.sha1(
            status.stdout.encode("utf-8", "replace")
        ).hexdigest()[:16]
        return f"{head_sha}.{status_hash}"
    except (OSError, subprocess.SubprocessError):
        return None


def _iter_source_files(
    root: Path, excluded_dirs: set[str], suffixes: set[str]
) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so os.walk never descends them.
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for filename in filenames:
            if Path(filename).suffix.lower() in suffixes:
                yield Path(dirpath) / filename


def compute_source_fingerprint(
    project_path: str | Path,
    *,
    excluded_dirs: list[str] | set[str] | None = None,
    max_files: int = DEFAULT_MAX_FINGERPRINT_FILES,
) -> dict[str, Any]:
    """A cheap, deterministic signal that changes whenever source changes.

    Walks source files (pruning vendored dirs the indexer already skips),
    stat-only, and prefers a git fingerprint when the repo is a git checkout.
    """
    root = Path(project_path)
    excluded = set(excluded_dirs or set()) | set(DEFAULT_EXCLUDED_DIRS)
    git_fp = _git_fingerprint(root)

    entries: list[str] = []
    file_count = 0
    total_bytes = 0
    truncated = False
    for path in _iter_source_files(root, excluded, SOURCE_SUFFIXES):
        try:
            st = path.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += int(st.st_size)
        if git_fp is None:
            if len(entries) < max_files:
                rel = path.relative_to(root).as_posix()
                entries.append(f"{rel}:{int(st.st_mtime_ns)}:{int(st.st_size)}")
            else:
                truncated = True

    if git_fp is not None:
        method = "git"
        digest = git_fp
    else:
        method = "mtime"
        entries.sort()
        hasher = hashlib.sha1()
        hasher.update(FINGERPRINT_VERSION.encode("utf-8"))
        for entry in entries:
            hasher.update(entry.encode("utf-8"))
            hasher.update(b"\n")
        if truncated:
            hasher.update(f"+count={file_count}".encode("utf-8"))
        digest = hasher.hexdigest()

    return {
        "fingerprint": f"{FINGERPRINT_VERSION}:{method}:{digest}",
        "method": method,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "truncated": truncated,
    }


def _stored_fingerprint(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    meta = state.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if isinstance(meta, dict) and meta.get("autoindex_fingerprint"):
        return str(meta["autoindex_fingerprint"])
    if state.get("source_hash"):
        return str(state["source_hash"])
    return None


def decide_action(
    state: dict[str, Any] | None,
    fingerprint_info: dict[str, Any],
    *,
    force: bool = False,
    allow_large: bool = False,
    max_first_build_files: int = DEFAULT_MAX_FIRST_BUILD_FILES,
    max_first_build_bytes: int = DEFAULT_MAX_FIRST_BUILD_BYTES,
) -> dict[str, Any]:
    """Decide what to do from graph state + a freshly computed fingerprint.

    Returns one of action in {no_code, fresh, deferred, index}.
    """
    file_count = int(fingerprint_info.get("file_count") or 0)
    total_bytes = int(fingerprint_info.get("total_bytes") or 0)
    if file_count == 0:
        return {"action": "no_code", "reason": "no indexable source files found"}

    present = (
        bool(state)
        and str((state or {}).get("status")) == "active"
        and int((state or {}).get("files_indexed") or 0) > 0
    )
    current_fp = fingerprint_info.get("fingerprint")
    stored_fp = _stored_fingerprint(state)

    if present and not force and stored_fp and current_fp and stored_fp == current_fp:
        return {"action": "fresh", "reason": "fingerprint matches stored graph"}

    first_build = not present
    if (
        first_build
        and not allow_large
        and (file_count > max_first_build_files or total_bytes > max_first_build_bytes)
    ):
        return {
            "action": "deferred",
            "reason": (
                f"first build too large for inline indexing "
                f"(files={file_count}, bytes={total_bytes}); "
                f"run mcum_code_graph_index or pass allow_large/force"
            ),
            "recommended_tool": "mcum_code_graph_index",
        }

    return {
        "action": "index",
        "reason": "missing graph" if first_build else "source changed since last index",
        "incremental": present,
    }


# ---------------------------------------------------------------------------
# DB-backed helpers
# ---------------------------------------------------------------------------
def read_graph_state(project_id: str) -> dict[str, Any] | None:
    """Latest code_graph.graphs row for a project, or None if absent/unavailable."""
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT graph_version, status, mode, source_hash,
                           files_indexed, nodes_total, edges_total,
                           finished_at, updated_at, metadata
                    FROM code_graph.graphs
                    WHERE project_id = %s
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception:
        return None


def store_fingerprint(
    project_id: str, fingerprint: str | None, extra: dict[str, Any] | None = None
) -> bool:
    """Persist the autoindex fingerprint into the graph row metadata."""
    if not fingerprint:
        return False
    payload: dict[str, Any] = {
        "autoindex_fingerprint": fingerprint,
        "autoindex_at": _now_iso(),
    }
    if extra:
        for key, value in extra.items():
            payload[f"autoindex_{key}"] = value
    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE code_graph.graphs
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                        updated_at = NOW()
                    WHERE project_id = %s
                    """,
                    (json.dumps(payload), project_id),
                )
        return True
    except Exception:
        return False


def _slim(sync_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sync_result, dict):
        return None
    keep = (
        "status", "trigger", "files_indexed", "nodes_indexed", "edges_indexed",
        "files_scanned", "files_skipped", "graph_version", "wall_clock_ms",
    )
    slim = {key: sync_result[key] for key in keep if key in sync_result}
    delta = sync_result.get("delta")
    if isinstance(delta, dict):
        slim["delta"] = {
            key: delta.get(key)
            for key in ("changed", "added", "removed")
            if key in delta
        }
    return slim


def _finish(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def ensure_code_graph(
    project_path: str | Path,
    project_name: str | None = None,
    *,
    task_type: str | None = None,
    force: bool = False,
    allow_large: bool = False,
    run_unified_sync: bool = True,
    check_only: bool = False,
    excluded_dirs: list[str] | None = None,
    max_file_bytes: int | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ensure the code graph for a project is present and fresh (gated).

    check_only=True computes the decision without indexing (cheap probe).
    """
    started = time.perf_counter()
    root = Path(project_path).resolve()
    project_name = project_name or root.name
    result: dict[str, Any] = {
        "project_path": str(root),
        "project_name": project_name,
    }

    if not is_code_relevant(task_type, force=force):
        result.update(
            {"action": "skipped", "reason": f"task_type '{task_type}' not code-relevant"}
        )
        return _finish(result, started)

    try:
        from ..policy import load_execution_policy
        exec_policy = dict(policy or (load_execution_policy().get("code_graph") or {}))
    except Exception:
        exec_policy = dict(policy or {})
    excluded = list(excluded_dirs or exec_policy.get("excluded_dirs") or [])

    fp_info = compute_source_fingerprint(root, excluded_dirs=excluded)

    try:
        from ..db.project_registry import get_or_create_project
        project = get_or_create_project(project_path=str(root), project_name=project_name)
        project_id = str(project["id"])
    except Exception as exc:
        result.update({"action": "error", "reason": f"project registry unavailable: {exc}"})
        return _finish(result, started)

    state = read_graph_state(project_id)
    decision = decide_action(
        state,
        fp_info,
        force=force,
        allow_large=allow_large,
        max_first_build_files=int(
            exec_policy.get("max_first_build_files") or DEFAULT_MAX_FIRST_BUILD_FILES
        ),
        max_first_build_bytes=int(
            exec_policy.get("max_first_build_bytes") or DEFAULT_MAX_FIRST_BUILD_BYTES
        ),
    )
    result.update(
        {
            "project_id": project_id,
            "fingerprint": fp_info,
            "graph_present": bool(state) and str((state or {}).get("status")) == "active",
            **decision,
        }
    )

    if check_only or decision["action"] in {"fresh", "no_code", "deferred", "skipped"}:
        return _finish(result, started)

    # action == "index": run incremental sync, then optional unified projection.
    try:
        from .code_graph_sync import sync_project_code_graph
        sync_result = sync_project_code_graph(
            project_id=project_id,
            project_path=str(root),
            project_name=project_name,
            trigger="auto_ensure",
            policy={
                **exec_policy,
                "excluded_dirs": excluded,
                "max_file_bytes": int(
                    max_file_bytes or exec_policy.get("max_file_bytes") or 1_000_000
                ),
            },
        )
    except Exception as exc:
        result.update({"action": "error", "reason": f"index failed: {exc}"})
        return _finish(result, started)

    if sync_result.get("status") == "failure":
        result.update(
            {"action": "error", "reason": str(sync_result.get("error") or "sync failed")}
        )
        return _finish(result, started)

    unified = None
    if run_unified_sync:
        try:
            from ..db.unified_graph_store import sync_unified_project_graph
            unified = sync_unified_project_graph(
                project_id=project_id,
                trigger="auto_ensure",
                selected_skill="mcum-orchestrator",
                code_graph_sync={**sync_result, "status": "success"},
            )
        except Exception as exc:
            unified = {"status": "skipped", "reason": str(exc)}

    store_fingerprint(
        project_id, fp_info.get("fingerprint"), {"files": fp_info.get("file_count")}
    )
    result.update(
        {
            "action": "indexed",
            "sync": _slim(sync_result),
            "unified": _slim(unified) if isinstance(unified, dict) else unified,
        }
    )
    return _finish(result, started)
