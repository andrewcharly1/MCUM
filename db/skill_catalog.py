"""
Workspace skill catalog and lifecycle metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .connection import get_db, get_cursor


SKILLS_ROOT = Path(__file__).resolve().parents[2]
VALID_STATUSES = {"candidate", "active", "degraded", "deprecated", "blocked"}


def _parse_frontmatter(skill_md: Path) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "|":
                idx += 1
                block: list[str] = []
                while idx < len(lines):
                    block_line = lines[idx]
                    if block_line.strip() == "---":
                        idx -= 1
                        break
                    if block_line and not block_line.startswith(" "):
                        idx -= 1
                        break
                    block.append(block_line.strip())
                    idx += 1
                metadata[key] = " ".join(part for part in block if part)
            else:
                metadata[key] = value.strip('"').strip("'")
        idx += 1
    return metadata


def discover_local_skills(skills_root: Path | None = None) -> list[dict]:
    root = skills_root or SKILLS_ROOT
    discovered: list[dict] = []

    for skill_dir in root.iterdir():
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith(".") or skill_dir.name == "__pycache__":
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        metadata = _parse_frontmatter(skill_md)
        description = metadata.get("description", "").strip()
        if not description:
            body = skill_md.read_text(encoding="utf-8").split("---")[-1]
            description = " ".join(line.strip() for line in body.splitlines()[:8] if line.strip())

        discovered.append(
            {
                "skill_name": metadata.get("name", skill_dir.name),
                "skill_dir_name": skill_dir.name,
                "skill_path": str(skill_dir),
                "description": description or f"Local skill from {skill_dir.name}",
                "source": "local",
                "metadata": {
                    "frontmatter": metadata,
                    "has_frontmatter": bool(metadata),
                },
            }
        )

    return discovered


def _load_skill_stats() -> dict[str, dict]:
    stats: dict[str, dict] = {}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    skill_name,
                    COUNT(*) FILTER (WHERE superseded_by IS NULL) AS experience_count,
                    COUNT(DISTINCT project_id) FILTER (WHERE project_id IS NOT NULL) AS project_count,
                    AVG(current_confidence) FILTER (WHERE superseded_by IS NULL) AS avg_confidence
                FROM core_brain.experiences
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                stats.setdefault(row["skill_name"], {}).update(
                    {
                        "experience_count": int(row.get("experience_count") or 0),
                        "project_count": int(row.get("project_count") or 0),
                        "avg_confidence": float(row["avg_confidence"]) if row.get("avg_confidence") is not None else None,
                    }
                )

            cur.execute(
                """
                SELECT skill_name, COUNT(*) FILTER (WHERE is_active = TRUE) AS active_test_count
                FROM core_brain.test_suite
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                stats.setdefault(row["skill_name"], {}).update(
                    {"active_test_count": int(row.get("active_test_count") or 0)}
                )

            cur.execute(
                """
                SELECT skill_used AS skill_name, MAX(created_at) AS last_used_at
                FROM project_registry.project_logs
                WHERE skill_used IS NOT NULL
                GROUP BY skill_used
                """
            )
            for row in cur.fetchall():
                stats.setdefault(row["skill_name"], {}).update({"last_used_at": row.get("last_used_at")})

            cur.execute(
                """
                SELECT skill_name, MAX(created_at) AS last_improved_at
                FROM core_brain.skill_versions
                GROUP BY skill_name
                """
            )
            for row in cur.fetchall():
                stats.setdefault(row["skill_name"], {}).update({"last_improved_at": row.get("last_improved_at")})

    return stats


def sync_skill_catalog(skills_root: Path | None = None) -> dict:
    discovered = discover_local_skills(skills_root)
    stats_by_skill = _load_skill_stats()
    discovered_names = {entry["skill_name"] for entry in discovered}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            for entry in discovered:
                stats = stats_by_skill.get(entry["skill_name"], {})
                cur.execute(
                    """
                    INSERT INTO project_registry.skill_catalog (
                        skill_name, skill_dir_name, skill_path, source, status,
                        description, metadata, discovered_at, last_synced_at,
                        last_used_at, last_improved_at, experience_count,
                        active_test_count, avg_confidence, project_count
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, NOW(), NOW(),
                        %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT (skill_name) DO UPDATE
                    SET skill_dir_name = EXCLUDED.skill_dir_name,
                        skill_path = EXCLUDED.skill_path,
                        source = EXCLUDED.source,
                        description = EXCLUDED.description,
                        metadata = COALESCE(project_registry.skill_catalog.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                        last_synced_at = NOW(),
                        last_used_at = COALESCE(EXCLUDED.last_used_at, project_registry.skill_catalog.last_used_at),
                        last_improved_at = COALESCE(EXCLUDED.last_improved_at, project_registry.skill_catalog.last_improved_at),
                        experience_count = EXCLUDED.experience_count,
                        active_test_count = EXCLUDED.active_test_count,
                        avg_confidence = EXCLUDED.avg_confidence,
                        project_count = EXCLUDED.project_count
                    """,
                    (
                        entry["skill_name"],
                        entry["skill_dir_name"],
                        entry["skill_path"],
                        entry["source"],
                        "active",
                        entry["description"],
                        json.dumps({**entry["metadata"], "missing_on_disk": False}),
                        stats.get("last_used_at"),
                        stats.get("last_improved_at"),
                        stats.get("experience_count", 0),
                        stats.get("active_test_count", 0),
                        stats.get("avg_confidence"),
                        stats.get("project_count", 0),
                    ),
                )

            if discovered_names:
                cur.execute(
                    """
                    UPDATE project_registry.skill_catalog
                    SET last_synced_at = NOW(),
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                    WHERE skill_name <> ALL(%s::text[])
                    """,
                    (json.dumps({"missing_on_disk": True}), list(discovered_names)),
                )

    return {
        "skills_discovered": len(discovered),
        "skills_synced": len(discovered_names),
    }


def upsert_skill_record(
    *,
    skill_name: str,
    skill_dir_name: str,
    skill_path: str,
    source: str = "local",
    status: str = "active",
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    normalized_status = status if status in VALID_STATUSES else "active"
    payload = json.dumps(metadata or {})

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.skill_catalog (
                    skill_name, skill_dir_name, skill_path, source, status,
                    description, metadata, discovered_at, last_synced_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, NOW(), NOW()
                )
                ON CONFLICT (skill_name) DO UPDATE
                SET skill_dir_name = EXCLUDED.skill_dir_name,
                    skill_path = EXCLUDED.skill_path,
                    source = EXCLUDED.source,
                    status = COALESCE(project_registry.skill_catalog.status, EXCLUDED.status),
                    description = COALESCE(EXCLUDED.description, project_registry.skill_catalog.description),
                    metadata = COALESCE(project_registry.skill_catalog.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    last_synced_at = NOW()
                RETURNING *
                """,
                (
                    skill_name,
                    skill_dir_name,
                    skill_path,
                    source,
                    normalized_status,
                    description,
                    payload,
                ),
            )
            row = cur.fetchone()
            return dict(row) if row else {
                "skill_name": skill_name,
                "skill_path": skill_path,
                "status": normalized_status,
            }


def update_skill_status(
    skill_name: str,
    status: str,
    metadata_update: dict[str, Any] | None = None,
) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported skill status: {status}")

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE project_registry.skill_catalog
                SET status = %s,
                    metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                    last_synced_at = NOW()
                WHERE skill_name = %s
                """,
                (status, json.dumps(metadata_update or {}), skill_name),
            )
            return cur.rowcount > 0


def merge_skill_metadata(skill_name: str, metadata_update: dict[str, Any]) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE project_registry.skill_catalog
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                    last_synced_at = NOW()
                WHERE skill_name = %s
                """,
                (json.dumps(metadata_update or {}), skill_name),
            )
            return cur.rowcount > 0


def mark_skill_used(skill_name: str) -> bool:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE project_registry.skill_catalog
                SET last_used_at = NOW(),
                    last_synced_at = NOW()
                WHERE skill_name = %s
                """,
                (skill_name,),
            )
            return cur.rowcount > 0


def get_skill_record(skill_name: str) -> dict | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT *
                FROM project_registry.skill_catalog
                WHERE skill_name = %s
                LIMIT 1
                """,
                (skill_name,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def list_skill_catalog(status: str | None = None) -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if status:
                cur.execute(
                    """
                    SELECT *
                    FROM project_registry.skill_catalog
                    WHERE status = %s
                    ORDER BY last_used_at DESC NULLS LAST, skill_name
                    """,
                    (status,),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM project_registry.skill_catalog
                    ORDER BY last_used_at DESC NULLS LAST, skill_name
                    """
                )
            return [dict(row) for row in cur.fetchall()]
