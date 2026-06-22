"""
Workspace skill catalog and lifecycle metadata.
"""

from __future__ import annotations

import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connection import get_db, get_cursor


SKILLS_ROOT = Path(__file__).resolve().parents[2]
VALID_STATUSES = {"candidate", "active", "degraded", "deprecated", "blocked"}


def get_runtime_id() -> str:
    explicit_runtime = str(os.getenv("MCUM_RUNTIME_ID", "")).strip().lower()
    if explicit_runtime:
        return explicit_runtime
    if os.name == "nt":
        return "windows"
    if os.getenv("WSL_DISTRO_NAME"):
        return "wsl"
    return (platform.system() or "unknown").strip().lower()


def _merge_runtime_metadata(
    metadata: dict[str, Any] | None,
    *,
    skill_path: str,
    runtime_id: str | None = None,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    normalized_path = str(skill_path or "").strip()
    if not normalized_path:
        return merged
    resolved_runtime = (runtime_id or get_runtime_id()).strip().lower() or "unknown"
    runtime_paths = dict(merged.get("runtime_paths") or {})
    runtime_paths[resolved_runtime] = normalized_path
    merged["runtime_paths"] = runtime_paths
    merged["last_synced_runtime"] = resolved_runtime
    return merged


def resolve_skill_path(record: dict[str, Any] | None, runtime_id: str | None = None) -> str:
    if not isinstance(record, dict):
        return ""
    metadata = dict(record.get("metadata") or {})
    runtime_paths = dict(metadata.get("runtime_paths") or {})
    resolved_runtime = (runtime_id or get_runtime_id()).strip().lower()
    candidates: list[str] = []
    if resolved_runtime:
        candidates.append(str(runtime_paths.get(resolved_runtime) or "").strip())
    candidates.append(str(record.get("skill_path") or "").strip())
    candidates.extend(
        str(path or "").strip()
        for key, path in runtime_paths.items()
        if str(key or "").strip().lower() != resolved_runtime
    )

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            if Path(candidate).exists():
                return candidate
        except OSError:
            continue

    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _coerce_frontmatter_value(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list):
            return loaded
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text.strip('"').strip("'")


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item.strip() for item in re.split(r"[,;\n]+", text) if item.strip()]
    return []


def _extract_section_terms(text: Any, heading_tokens: tuple[str, ...]) -> list[str]:
    lines = str(text or "").splitlines()
    active = False
    values: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if any(token in upper for token in heading_tokens):
            active = True
            continue
        if active and line.startswith("##"):
            break
        if not active:
            continue
        quoted = re.findall(r'"([^"]+)"', line)
        if quoted:
            values.extend(value.strip() for value in quoted if value.strip())
            continue
        if line.startswith("- "):
            candidate = line[2:].strip()
            if candidate and len(candidate.split()) <= 5:
                values.append(candidate)
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _build_routing_metadata(metadata: dict[str, Any], description: str) -> dict[str, Any]:
    explicit_routing = any(
        key in metadata for key in ("routing_triggers", "routing_anti", "routing_priority", "routing_profile")
    )
    triggers = _normalize_string_list(metadata.get("routing_triggers") or metadata.get("triggers"))
    anti = _normalize_string_list(metadata.get("routing_anti") or metadata.get("anti"))
    description_triggers = _extract_section_terms(description, ("TRIGGER KEYWORDS",))
    description_anti = _extract_section_terms(description, ("ANTI-TRIGGER", "ANTI TRIGGER"))
    if not triggers:
        triggers = description_triggers
    if not anti:
        anti = description_anti
    priority = metadata.get("routing_priority", metadata.get("priority", 6))
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 6
    profile = str(metadata.get("routing_profile") or description or "").strip()
    enabled = metadata.get("routing_enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() != "false"
    source = "description"
    if explicit_routing:
        source = "frontmatter"
    elif description_triggers or description_anti:
        source = "description_sections"
    return {
        "enabled": bool(enabled),
        "triggers": triggers,
        "anti": anti,
        "priority": max(0, priority),
        "profile": profile,
        "has_explicit_routing": explicit_routing,
        "source": source,
    }


def _parse_frontmatter(skill_md: Path) -> dict[str, Any]:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, Any] = {}
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
                    block.append(block_line[1:] if block_line.startswith(" ") else block_line)
                    idx += 1
                metadata[key] = "\n".join(part.rstrip() for part in block).strip()
            else:
                metadata[key] = _coerce_frontmatter_value(value)
        idx += 1
    return metadata


def discover_local_skills(skills_root: Path | None = None) -> list[dict]:
    root = skills_root or SKILLS_ROOT
    discovered: list[dict] = []
    runtime_id = get_runtime_id()

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
                "metadata": _merge_runtime_metadata({
                    "frontmatter": metadata,
                    "has_frontmatter": bool(metadata),
                    "routing": _build_routing_metadata(metadata, description),
                    "runtime_id": runtime_id,
                }, skill_path=str(skill_dir), runtime_id=runtime_id),
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
    existing_records = {record["skill_name"]: record for record in list_skill_catalog()}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            for entry in discovered:
                stats = stats_by_skill.get(entry["skill_name"], {})
                existing_metadata = dict(existing_records.get(entry["skill_name"], {}).get("metadata") or {})
                merged_metadata = _merge_runtime_metadata(
                    {
                        **existing_metadata,
                        **entry["metadata"],
                        "missing_on_disk": False,
                    },
                    skill_path=entry["skill_path"],
                )
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
                        json.dumps(merged_metadata),
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
    existing_record = get_skill_record(skill_name)
    existing_metadata = dict(existing_record.get("metadata") or {}) if existing_record else {}
    payload = json.dumps(
        _merge_runtime_metadata(
            {
                **existing_metadata,
                **dict(metadata or {}),
            },
            skill_path=skill_path,
        )
    )

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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def retire_skill_record(
    skill_name: str,
    *,
    reason: str,
    metadata_update: dict[str, Any] | None = None,
    retired_at: str | None = None,
) -> bool:
    payload = dict(metadata_update or {})
    payload["review_status"] = "retired"
    payload["review_reason"] = reason
    payload["retirement"] = {
        **dict(payload.get("retirement") or {}),
        "reason": reason,
        "retired_at": retired_at or _utc_now_iso(),
    }
    return update_skill_status(skill_name, "deprecated", payload)


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
