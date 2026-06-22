"""Persistence helpers for project design systems in MCUM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .connection import get_cursor, get_db
from .project_registry import get_or_create_project, normalize_project_path


VALID_STATUSES = {"proposed", "approved", "deprecated", "rejected"}
VALID_SOURCE_KINDS = {"manual", "reference_image", "screenshot", "existing_product", "mixed"}

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS project_registry.design_system_profiles (
        id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        project_id              UUID NOT NULL UNIQUE REFERENCES project_registry.projects(id) ON DELETE CASCADE,
        product_name            TEXT NOT NULL,
        audience                TEXT,
        platform_targets        TEXT[] DEFAULT '{}',
        design_maturity         TEXT DEFAULT 'draft'
            CHECK (design_maturity IN ('draft','proposed','approved','deprecated')),
        source_summary          TEXT,
        created_at              TIMESTAMPTZ DEFAULT NOW(),
        updated_at              TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_registry.design_system_versions (
        id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        profile_id                  UUID NOT NULL REFERENCES project_registry.design_system_profiles(id) ON DELETE CASCADE,
        version_number              INT NOT NULL CHECK (version_number > 0),
        status                      TEXT DEFAULT 'proposed'
            CHECK (status IN ('proposed','approved','deprecated','rejected')),
        source_kind                 TEXT DEFAULT 'manual'
            CHECK (source_kind IN ('manual','reference_image','screenshot','existing_product','mixed')),
        design_brief                JSONB NOT NULL DEFAULT '{}'::jsonb,
        design_tokens               JSONB NOT NULL DEFAULT '{}'::jsonb,
        layout_system               JSONB NOT NULL DEFAULT '{}'::jsonb,
        component_guidelines        JSONB NOT NULL DEFAULT '{}'::jsonb,
        interaction_guidelines      JSONB NOT NULL DEFAULT '{}'::jsonb,
        accessibility_guidelines    JSONB NOT NULL DEFAULT '{}'::jsonb,
        content_voice               JSONB NOT NULL DEFAULT '{}'::jsonb,
        reference_artifacts         JSONB NOT NULL DEFAULT '[]'::jsonb,
        approval_metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by_skill            TEXT DEFAULT 'design-system-orchestrator',
        source_task_log_id          UUID REFERENCES project_registry.project_logs(id),
        created_at                  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (profile_id, version_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_design_system_profiles_project
        ON project_registry.design_system_profiles (project_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_design_system_versions_profile_status
        ON project_registry.design_system_versions (profile_id, status, version_number DESC)
    """,
]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(loaded) if isinstance(loaded, list) else []
    return []


def _coerce_text_list(value: Any) -> list[str]:
    items = _json_list(value)
    if not items and isinstance(value, str) and value.strip():
        items = [part.strip() for part in value.split(",")]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def ensure_design_system_schema() -> None:
    """Install only the design-system tables/indexes needed by this module."""

    with get_db() as conn:
        with get_cursor(conn) as cur:
            for statement in _DDL_STATEMENTS:
                cur.execute(statement)


def normalize_design_system_spec(spec: dict[str, Any] | str | None) -> dict[str, Any]:
    payload = _json_object(spec)
    identity = _json_object(payload.get("product_identity"))

    return {
        "product_identity": identity,
        "design_brief": {
            "product_identity": identity,
            "design_principles": _json_list(identity.get("design_principles")),
            "open_questions": _json_list(payload.get("open_questions")),
            "confidence_score": payload.get("confidence_score"),
        },
        "design_tokens": _json_object(payload.get("design_tokens")),
        "layout_system": _json_object(payload.get("layout_system")),
        "component_guidelines": _json_object(payload.get("component_guidelines")),
        "interaction_guidelines": _json_object(payload.get("interaction_guidelines")),
        "accessibility_guidelines": _json_object(payload.get("accessibility_guidelines")),
        "content_voice": _json_object(payload.get("content_voice")),
        "reference_artifacts": _json_list(payload.get("reference_artifacts")),
    }


def _extract_profile_fields(
    *,
    project_name: str | None,
    product_name: str | None,
    audience: str | None,
    platform_targets: list[str] | None,
    normalized_spec: dict[str, Any],
) -> dict[str, Any]:
    identity = _json_object(normalized_spec.get("product_identity"))
    resolved_product_name = (
        str(product_name or "").strip()
        or str(identity.get("product_name") or "").strip()
        or str(project_name or "").strip()
        or "Untitled product"
    )
    resolved_audience = (
        str(audience or "").strip()
        or str(identity.get("target_users") or "").strip()
        or None
    )
    resolved_platforms = platform_targets or _coerce_text_list(identity.get("platforms"))
    return {
        "product_name": resolved_product_name,
        "audience": resolved_audience,
        "platform_targets": resolved_platforms,
    }


def save_design_system_version(
    *,
    project_path: str,
    project_name: str | None = None,
    design_system: dict[str, Any] | str | None = None,
    status: str = "approved",
    source_kind: str = "manual",
    product_name: str | None = None,
    audience: str | None = None,
    platform_targets: list[str] | None = None,
    approval_metadata: dict[str, Any] | None = None,
    source_task_log_id: str | None = None,
    created_by_skill: str = "design-system-orchestrator",
    ensure_schema: bool = True,
) -> dict[str, Any]:
    if ensure_schema:
        ensure_design_system_schema()

    normalized_status = status if status in VALID_STATUSES else "proposed"
    normalized_source_kind = source_kind if source_kind in VALID_SOURCE_KINDS else "manual"
    normalized_spec = normalize_design_system_spec(design_system)
    project = get_or_create_project(project_path=project_path, project_name=project_name)
    profile_fields = _extract_profile_fields(
        project_name=project_name,
        product_name=product_name,
        audience=audience,
        platform_targets=platform_targets,
        normalized_spec=normalized_spec,
    )
    design_maturity = "approved" if normalized_status == "approved" else "proposed"

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.design_system_profiles (
                    project_id, product_name, audience, platform_targets,
                    design_maturity, source_summary
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id) DO UPDATE
                SET product_name = EXCLUDED.product_name,
                    audience = COALESCE(EXCLUDED.audience, project_registry.design_system_profiles.audience),
                    platform_targets = EXCLUDED.platform_targets,
                    design_maturity = EXCLUDED.design_maturity,
                    source_summary = COALESCE(EXCLUDED.source_summary, project_registry.design_system_profiles.source_summary),
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    project["id"],
                    profile_fields["product_name"],
                    profile_fields["audience"],
                    profile_fields["platform_targets"],
                    design_maturity,
                    f"Captured via {created_by_skill} from {normalized_source_kind}.",
                ),
            )
            profile = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                FROM project_registry.design_system_versions
                WHERE profile_id = %s
                """,
                (profile["id"],),
            )
            next_version = int((cur.fetchone() or {}).get("next_version") or 1)

            if normalized_status == "approved":
                cur.execute(
                    """
                    UPDATE project_registry.design_system_versions
                    SET status = 'deprecated'
                    WHERE profile_id = %s
                      AND status = 'approved'
                    """,
                    (profile["id"],),
                )

            cur.execute(
                """
                INSERT INTO project_registry.design_system_versions (
                    profile_id, version_number, status, source_kind,
                    design_brief, design_tokens, layout_system,
                    component_guidelines, interaction_guidelines,
                    accessibility_guidelines, content_voice, reference_artifacts,
                    approval_metadata, created_by_skill, source_task_log_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING *
                """,
                (
                    profile["id"],
                    next_version,
                    normalized_status,
                    normalized_source_kind,
                    json.dumps(normalized_spec["design_brief"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["design_tokens"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["layout_system"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["component_guidelines"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["interaction_guidelines"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["accessibility_guidelines"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["content_voice"], ensure_ascii=False, default=str),
                    json.dumps(normalized_spec["reference_artifacts"], ensure_ascii=False, default=str),
                    json.dumps(approval_metadata or {}, ensure_ascii=False, default=str),
                    created_by_skill,
                    source_task_log_id,
                ),
            )
            version = dict(cur.fetchone() or {})

    return {
        "project": project,
        "profile": profile,
        "version": version,
        "design_system_profile_id": str(profile.get("id") or ""),
        "design_system_version_id": str(version.get("id") or ""),
        "version_number": int(version.get("version_number") or next_version),
        "status": str(version.get("status") or normalized_status),
        "source_kind": str(version.get("source_kind") or normalized_source_kind),
    }


def get_latest_design_system(
    *,
    project_path: str,
    status: str | None = "approved",
    ensure_schema: bool = True,
) -> dict[str, Any] | None:
    if ensure_schema:
        ensure_design_system_schema()

    normalized_path = normalize_project_path(project_path)
    status_clause = "AND v.status = %s" if status else ""
    params: list[Any] = [normalized_path]
    if status:
        params.append(status)

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                SELECT
                    p.project_name,
                    p.project_path,
                    dsp.*,
                    v.id AS version_id,
                    v.version_number,
                    v.status AS version_status,
                    v.source_kind,
                    v.design_brief,
                    v.design_tokens,
                    v.layout_system,
                    v.component_guidelines,
                    v.interaction_guidelines,
                    v.accessibility_guidelines,
                    v.content_voice,
                    v.reference_artifacts,
                    v.approval_metadata,
                    v.created_by_skill,
                    v.created_at AS version_created_at
                FROM project_registry.projects p
                JOIN project_registry.design_system_profiles dsp
                  ON dsp.project_id = p.id
                JOIN project_registry.design_system_versions v
                  ON v.profile_id = dsp.id
                WHERE p.project_path = %s
                  {status_clause}
                ORDER BY v.version_number DESC
                LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _load_spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.spec_file:
        return json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    if args.spec_json:
        return json.loads(args.spec_json)
    raise ValueError("Provide --spec-json or --spec-file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist and inspect MCUM project design systems.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upsert = subparsers.add_parser("upsert")
    upsert.add_argument("--project-path", required=True)
    upsert.add_argument("--project-name")
    upsert.add_argument("--product-name")
    upsert.add_argument("--audience")
    upsert.add_argument("--platform", action="append", dest="platforms")
    upsert.add_argument("--status", default="approved", choices=sorted(VALID_STATUSES))
    upsert.add_argument("--source-kind", default="manual", choices=sorted(VALID_SOURCE_KINDS))
    upsert.add_argument("--spec-json")
    upsert.add_argument("--spec-file")

    show = subparsers.add_parser("show")
    show.add_argument("--project-path", required=True)
    show.add_argument("--status", default="approved")

    args = parser.parse_args(argv)
    if args.command == "upsert":
        result = save_design_system_version(
            project_path=args.project_path,
            project_name=args.project_name,
            product_name=args.product_name,
            audience=args.audience,
            platform_targets=args.platforms,
            design_system=_load_spec(args),
            status=args.status,
            source_kind=args.source_kind,
        )
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0

    if args.command == "show":
        result = get_latest_design_system(project_path=args.project_path, status=args.status or None)
        print(json.dumps(result or {}, ensure_ascii=False, default=str, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
