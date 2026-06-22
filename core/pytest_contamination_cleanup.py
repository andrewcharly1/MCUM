"""Fail-closed cleanup for projects created by historical pytest runs."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from ..db.connection import get_cursor, get_db


CONFIRM_TOKEN = "DELETE-ISOLATED-PYTEST-PROJECTS"
PYTEST_PROJECT_PATH = re.compile(
    r"^[A-Za-z]:/Users/[^/]+/AppData/Local/Temp/"
    r"pytest-of-[^/]+/pytest-\d+/test_[^/]+$"
)
NO_ACTION_DEPENDENCIES = (
    "experiences",
    "retrieval_runs",
    "session_playbooks",
    "maintenance_runs",
    "project_kpis",
    "project_logs",
)


def classify_project(row: dict[str, Any]) -> tuple[str, list[str]]:
    path = str(row.get("project_path") or "")
    if not PYTEST_PROJECT_PATH.fullmatch(path):
        return "not_pytest", ["path_not_exact_pytest_temp"]

    reasons: list[str] = []
    for field in ("total_sessions", "total_tasks_completed", "total_improvements"):
        if int(row.get(field) or 0) != 0:
            reasons.append(f"{field}_nonzero")
    for field in NO_ACTION_DEPENDENCIES:
        if int(row.get(field) or 0) != 0:
            reasons.append(f"{field}_present")
    return ("safe" if not reasons else "protected"), reasons


def build_cleanup_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    safe: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        classification, reasons = classify_project(row)
        item = {
            "id": str(row.get("id") or ""),
            "project_path": str(row.get("project_path") or ""),
            "reasons": reasons,
            "spec_contracts": int(row.get("spec_contracts") or 0),
        }
        if classification == "safe":
            safe.append(item)
        elif classification == "protected":
            protected.append(item)
        else:
            rejected.append(item)
    return {
        "status": "dry_run",
        "matched_count": len(safe) + len(protected),
        "safe_count": len(safe),
        "protected_count": len(protected),
        "rejected_count": len(rejected),
        "safe_spec_contracts": sum(item["spec_contracts"] for item in safe),
        "safe": safe,
        "protected": protected,
        "rejected": rejected,
    }


def load_cleanup_report() -> dict[str, Any]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    p.id::text,
                    p.project_path,
                    p.total_sessions,
                    p.total_tasks_completed,
                    p.total_improvements,
                    (SELECT COUNT(*) FROM core_brain.experiences x WHERE x.project_id = p.id) AS experiences,
                    (SELECT COUNT(*) FROM core_brain.retrieval_runs x WHERE x.project_id = p.id) AS retrieval_runs,
                    (SELECT COUNT(*) FROM core_brain.session_playbooks x WHERE x.project_id = p.id) AS session_playbooks,
                    (SELECT COUNT(*) FROM project_registry.maintenance_runs x WHERE x.project_id = p.id) AS maintenance_runs,
                    (SELECT COUNT(*) FROM project_registry.project_kpis x WHERE x.project_id = p.id) AS project_kpis,
                    (SELECT COUNT(*) FROM project_registry.project_logs x WHERE x.project_id = p.id) AS project_logs,
                    (SELECT COUNT(*) FROM project_registry.spec_contracts x WHERE x.project_id = p.id) AS spec_contracts
                FROM project_registry.projects p
                WHERE p.project_path ILIKE '%%/AppData/Local/Temp/pytest-of-%%'
                ORDER BY p.project_path
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
    return build_cleanup_report(rows)


def validate_apply_request(
    report: dict[str, Any],
    *,
    expected_count: int | None,
    confirm_token: str | None,
) -> None:
    if expected_count is None or int(expected_count) != int(report.get("safe_count") or 0):
        raise RuntimeError(
            f"safe candidate count drift: expected={expected_count}, "
            f"actual={report.get('safe_count')}"
        )
    if str(confirm_token or "") != CONFIRM_TOKEN:
        raise RuntimeError("invalid cleanup confirmation token")


def apply_cleanup(*, expected_count: int, confirm_token: str) -> dict[str, Any]:
    report = load_cleanup_report()
    validate_apply_request(
        report,
        expected_count=expected_count,
        confirm_token=confirm_token,
    )
    safe_ids = [item["id"] for item in report["safe"]]
    if not safe_ids:
        return {**report, "status": "no_changes", "deleted_projects": 0}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                DELETE FROM project_registry.projects
                WHERE id = ANY(%s::uuid[])
                RETURNING id::text
                """,
                (safe_ids,),
            )
            deleted_ids = [str(row["id"]) for row in cur.fetchall()]
            if len(deleted_ids) != expected_count:
                raise RuntimeError(
                    f"cleanup delete count mismatch: expected={expected_count}, "
                    f"actual={len(deleted_ids)}"
                )
    remaining = load_cleanup_report()
    return {
        "status": "success",
        "deleted_projects": len(deleted_ids),
        "protected_projects": int(remaining.get("protected_count") or 0),
        "remaining_safe_projects": int(remaining.get("safe_count") or 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--confirm-token")
    args = parser.parse_args(argv)

    result = (
        apply_cleanup(
            expected_count=args.expected_count,
            confirm_token=str(args.confirm_token or ""),
        )
        if args.apply
        else load_cleanup_report()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
