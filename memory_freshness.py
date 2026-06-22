"""
Freshness and invalidation helpers for MCUM operational memory.

This module keeps retrieval useful across sessions by penalizing stale evidence,
tracking lightweight source snapshots, and aging dispatch hints over time.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

_SKILL_VERSION_CACHE_TTL_SEC = 300
_SKILL_VERSION_CACHE: dict[str, tuple[str | None, float]] = {}
_STRUCTURAL_CANDIDATES = [
    ("package.json", "package_manifest"),
    ("package-lock.json", "node_lockfile"),
    ("pnpm-lock.yaml", "node_lockfile"),
    ("yarn.lock", "node_lockfile"),
    ("requirements.txt", "python_manifest"),
    ("pyproject.toml", "python_manifest"),
    ("poetry.lock", "python_lockfile"),
    ("Pipfile", "python_manifest"),
    ("Pipfile.lock", "python_lockfile"),
    ("go.mod", "go_manifest"),
    ("go.sum", "go_lockfile"),
    ("Cargo.toml", "rust_manifest"),
    ("Cargo.lock", "rust_lockfile"),
    ("schema.sql", "schema_file"),
    ("db/schema.sql", "schema_file"),
    ("migrations", "migration_dir"),
    ("db/migrations", "migration_dir"),
    ("supabase/migrations", "migration_dir"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_path(path: str | None, project_path: str | None = None) -> str | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute() and project_path:
        candidate = Path(project_path) / candidate
    try:
        return str(candidate.resolve(strict=False)).replace("\\", "/")
    except OSError:
        return str(candidate).replace("\\", "/")


def _safe_exists(path_obj: Path) -> bool:
    try:
        return path_obj.exists()
    except OSError:
        return False


def _safe_is_dir(path_obj: Path) -> bool:
    try:
        return path_obj.is_dir()
    except OSError:
        return False


def _safe_is_file(path_obj: Path) -> bool:
    try:
        return path_obj.is_file()
    except OSError:
        return False


def build_source_snapshots(
    paths: list[str] | None,
    *,
    project_path: str | None = None,
    max_items: int = 8,
    snapshot_type: str = "source_file",
    include_hash: bool = False,
    role: str | None = None,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in paths or []:
        normalized = _normalize_path(raw_path, project_path=project_path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        path_obj = Path(normalized)
        exists = _safe_exists(path_obj)
        snapshot: dict[str, Any] = {
            "snapshot_type": snapshot_type,
            "path": normalized,
            "exists": exists,
            "captured_at": utc_now_iso(),
        }
        if role:
            snapshot["role"] = role
        if exists:
            try:
                stat = path_obj.stat()
            except OSError:
                snapshot["exists"] = False
            else:
                snapshot["kind"] = "directory" if _safe_is_dir(path_obj) else "file"
                snapshot["size"] = int(stat.st_size)
                snapshot["mtime_ns"] = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
                if include_hash and _safe_is_file(path_obj) and stat.st_size <= 2_000_000:
                    try:
                        snapshot["sha1"] = sha1(path_obj.read_bytes()).hexdigest()
                    except OSError:
                        pass
        else:
            snapshot["kind"] = "missing"
        snapshots.append(snapshot)
        if len(snapshots) >= max_items:
            break
    return snapshots


def build_project_structure_snapshots(
    project_path: str | None,
    *,
    extra_paths: list[str] | None = None,
    max_items: int = 10,
) -> list[dict[str, Any]]:
    root = _normalize_path(project_path)
    if not root:
        return []
    root_path = Path(root)
    if not _safe_exists(root_path):
        return []

    candidates: list[tuple[str, str]] = list(_STRUCTURAL_CANDIDATES)
    seen_relative = {relative for relative, _ in candidates}
    for extra in extra_paths or []:
        normalized = _normalize_path(extra, project_path=root)
        if not normalized:
            continue
        try:
            relative = str(Path(normalized).resolve(strict=False).relative_to(root_path.resolve(strict=False))).replace("\\", "/")
        except ValueError:
            continue
        lowered = relative.lower()
        if lowered in seen_relative:
            continue
        if any(token in lowered for token in ("migration", "schema", "lock", "package", "requirements", "pyproject", "go.mod", "cargo")):
            role = "touched_structure"
            candidates.append((relative, role))
            seen_relative.add(lowered)

    structural_paths: list[str] = []
    roles_by_path: dict[str, str] = {}
    for relative, role in candidates:
        normalized = _normalize_path(relative, project_path=root)
        if not normalized:
            continue
        path_obj = Path(normalized)
        if not path_obj.exists():
            continue
        structural_paths.append(normalized)
        roles_by_path[normalized] = role
        if len(structural_paths) >= max_items:
            break

    snapshots: list[dict[str, Any]] = []
    for normalized in structural_paths:
        role = roles_by_path.get(normalized)
        snapshots.extend(
            build_source_snapshots(
                [normalized],
                max_items=1,
                snapshot_type="project_structure",
                include_hash=True,
                role=role,
            )
        )
    return snapshots[:max_items]


def _snapshot_entries(item: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "experience":
        raw = item.get("source_artifacts") or []
        return [entry for entry in raw if isinstance(entry, dict)]
    if kind == "playbook":
        artifacts = item.get("artifacts") or []
        return [
            entry
            for entry in artifacts
            if isinstance(entry, dict)
            and (
                entry.get("snapshot_type") in {"source_file", "project_structure"}
                or ("path" in entry and "mtime_ns" in entry)
            )
        ]
    return []


def _snapshot_penalty(
    snapshots: list[dict[str, Any]],
    *,
    project_path: str | None = None,
) -> tuple[float, list[str], dict[str, int]]:
    if not snapshots:
        return 0.0, [], {"checked": 0, "changed": 0}

    checked = 0
    changed = 0
    reasons: list[str] = []
    for snapshot in snapshots:
        normalized = _normalize_path(snapshot.get("path"), project_path=project_path)
        if not normalized:
            continue
        checked += 1
        path_obj = Path(normalized)
        current_exists = _safe_exists(path_obj)
        saved_exists = bool(snapshot.get("exists"))
        if current_exists != saved_exists:
            changed += 1
            reasons.append(f"path existence changed: {normalized}")
            continue
        if not current_exists:
            continue
        try:
            stat = path_obj.stat()
        except OSError:
            changed += 1
            reasons.append(f"path stat failed: {normalized}")
            continue
        saved_size = snapshot.get("size")
        saved_mtime = snapshot.get("mtime_ns")
        saved_hash = snapshot.get("sha1")
        current_mtime = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
        label = "project structure" if snapshot.get("snapshot_type") == "project_structure" else "path"
        if saved_hash and _safe_is_file(path_obj):
            try:
                current_hash = sha1(path_obj.read_bytes()).hexdigest()
            except OSError:
                current_hash = None
            if current_hash and str(saved_hash) != current_hash:
                changed += 1
                reasons.append(f"{label} content changed: {normalized}")
                continue
        if saved_size is not None and int(saved_size) != int(stat.st_size):
            changed += 1
            reasons.append(f"{label} size changed: {normalized}")
            continue
        if saved_mtime is not None and int(saved_mtime) != current_mtime:
            changed += 1
            reasons.append(f"{label} mtime changed: {normalized}")

    if checked == 0 or changed == 0:
        return 0.0, reasons[:3], {"checked": checked, "changed": changed}

    ratio = changed / checked
    if ratio >= 0.75:
        penalty = 0.45
    elif ratio >= 0.40:
        penalty = 0.28
    else:
        penalty = 0.16
    return penalty, reasons[:3], {"checked": checked, "changed": changed}


def _age_score(age_days: float) -> float:
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.92
    if age_days <= 90:
        return 0.80
    if age_days <= 180:
        return 0.65
    if age_days <= 365:
        return 0.50
    return 0.35


def _version_penalty(saved_version: str | None, current_version: str | None) -> tuple[float, str | None]:
    saved = str(saved_version or "").strip()
    current = str(current_version or "").strip()
    if not saved or not current or saved == current:
        return 0.0, None

    saved_parts = saved.split(".")
    current_parts = current.split(".")
    if saved_parts[:2] == current_parts[:2]:
        return 0.08, f"skill version drift: {saved} -> {current}"
    if saved_parts[:1] == current_parts[:1]:
        return 0.16, f"skill version drift: {saved} -> {current}"
    return 0.28, f"skill version drift: {saved} -> {current}"


def get_active_skill_version(skill_name: str | None) -> str | None:
    key = str(skill_name or "").strip()
    if not key:
        return None
    cached = _SKILL_VERSION_CACHE.get(key)
    now = time.time()
    if cached and (now - cached[1]) < _SKILL_VERSION_CACHE_TTL_SEC:
        return cached[0]

    version: str | None = None
    try:
        from .db.connection import get_db, get_cursor

        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT version_semver
                    FROM core_brain.skill_versions
                    WHERE skill_name = %s
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if row:
                    version = str(row.get("version_semver") or "").strip() or None
    except Exception:
        version = None

    _SKILL_VERSION_CACHE[key] = (version, now)
    return version


def score_memory_freshness(
    item: dict[str, Any],
    *,
    kind: str,
    current_skill_version: str | None = None,
    project_path: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference_time = now or utc_now()
    timestamp = (
        _parse_datetime(item.get("last_validated_at"))
        or _parse_datetime(item.get("last_reused_at"))
        or _parse_datetime(item.get("updated_at"))
        or _parse_datetime(item.get("created_at"))
        or reference_time
    )
    age_days = max(0.0, (reference_time - timestamp).total_seconds() / 86400)
    score = _age_score(age_days)
    reasons: list[str] = []

    version_penalty = 0.0
    version_reason = None
    if kind == "experience":
        version_penalty, version_reason = _version_penalty(
            item.get("skill_version"),
            current_skill_version or get_active_skill_version(item.get("skill_name")),
        )
        score -= version_penalty
        if version_reason:
            reasons.append(version_reason)

    snapshots = _snapshot_entries(item, kind)
    snapshot_penalty, snapshot_reasons, snapshot_stats = _snapshot_penalty(
        snapshots,
        project_path=project_path,
    )
    score -= snapshot_penalty
    reasons.extend(snapshot_reasons)

    if kind == "playbook":
        last_reused_at = _parse_datetime(item.get("last_reused_at"))
        if last_reused_at and (reference_time - last_reused_at).total_seconds() <= 14 * 86400:
            score += 0.05

    score = max(0.0, min(1.0, round(score, 4)))
    if score >= 0.85:
        state = "fresh"
    elif score >= 0.65:
        state = "aging"
    elif score >= 0.45:
        state = "stale"
    else:
        state = "invalidated"

    if age_days > 90:
        reasons.append(f"age={int(age_days)}d")

    return {
        "freshness_score": score,
        "freshness_state": state,
        "freshness_multiplier": round(0.35 + (0.65 * score), 4),
        "freshness_reasons": reasons[:4],
        "freshness_stats": {
            "age_days": round(age_days, 1),
            "snapshot_checked": snapshot_stats["checked"],
            "snapshot_changed": snapshot_stats["changed"],
            "version_penalty": version_penalty,
            "snapshot_penalty": snapshot_penalty,
        },
    }


def apply_memory_freshness(
    items: list[dict[str, Any]],
    *,
    kind: str,
    current_skill_version: str | None = None,
    project_path: str | None = None,
    score_key: str | None = None,
) -> list[dict[str, Any]]:
    enriched_items: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        freshness = score_memory_freshness(
            item,
            kind=kind,
            current_skill_version=current_skill_version,
            project_path=project_path,
        )
        enriched = dict(item)
        enriched["_freshness_score"] = freshness["freshness_score"]
        enriched["_freshness_state"] = freshness["freshness_state"]
        enriched["_freshness_multiplier"] = freshness["freshness_multiplier"]
        enriched["_freshness_reasons"] = freshness["freshness_reasons"]
        enriched["_freshness_stats"] = freshness["freshness_stats"]
        if score_key and enriched.get(score_key) is not None:
            base_score = float(enriched.get(score_key) or 0.0)
            enriched["_base_score"] = round(base_score, 4)
            enriched[score_key] = round(base_score * freshness["freshness_multiplier"], 4)
        enriched_items.append(enriched)

    if score_key:
        enriched_items.sort(
            key=lambda item: (
                float(item.get(score_key) or 0.0),
                float(item.get("_freshness_score") or 0.0),
            ),
            reverse=True,
        )
    return enriched_items


def summarize_freshness_warnings(items: list[dict[str, Any]], *, label: str) -> list[str]:
    stale = [item for item in items or [] if item.get("_freshness_state") in {"stale", "invalidated"}]
    if not stale:
        return []
    top = stale[:2]
    summaries = []
    for item in top:
        title = item.get("title") or item.get("id") or "untitled"
        state = item.get("_freshness_state")
        reasons = ", ".join(item.get("_freshness_reasons") or [])
        summaries.append(f"{title} [{state}] {reasons}".strip())
    return [f"{label} include stale memory: {' | '.join(summaries)}"]


def apply_dispatch_hint_freshness(dispatch_hints: dict[str, Any] | None) -> dict[str, Any]:
    hints = dict(dispatch_hints or {})
    updated_at = _parse_datetime(hints.get("updated_at") or hints.get("last_applied_at"))
    if updated_at is None:
        hints["_freshness_score"] = 1.0
        hints["_freshness_state"] = "fresh"
        return hints

    age_days = max(0.0, (utc_now() - updated_at).total_seconds() / 86400)
    freshness = _age_score(age_days)
    if age_days > 120:
        freshness *= 0.35
    elif age_days > 60:
        freshness *= 0.60
    elif age_days > 30:
        freshness *= 0.80

    raw_delta = int(hints.get("priority_delta", 0) or 0)
    scaled_delta = int(round(raw_delta * freshness)) if raw_delta else 0
    hints["priority_delta"] = scaled_delta
    hints["_freshness_score"] = round(freshness, 4)
    hints["_freshness_state"] = (
        "fresh" if freshness >= 0.85 else "aging" if freshness >= 0.65 else "stale"
    )
    hints["_age_days"] = round(age_days, 1)
    return hints
