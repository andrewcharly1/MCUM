"""
Skill factory and lifecycle promotion helpers.

This module borrows the deterministic scaffolding and validation ideas from the
system skill-creator, while keeping generated skills inside the local workspace.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..memory_freshness import apply_dispatch_hint_freshness, utc_now_iso
from ..db.connection import get_db, get_cursor
from ..db.project_registry import log_entry
from ..db.skill_catalog import (
    discover_local_skills,
    get_skill_record,
    list_skill_catalog,
    merge_skill_metadata,
    resolve_skill_path,
    retire_skill_record,
    sync_skill_catalog,
    update_skill_status,
    upsert_skill_record,
)
from ..logging_utils import get_logger
from ..sisl.skill_writer import rollback_sisl_writeback


LOGGER = get_logger("core.skill_factory")
SKILLS_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_SKILL_CREATOR_ROOT = Path.home() / ".codex" / "skills" / ".system" / "skill-creator"
INIT_SCRIPT = SYSTEM_SKILL_CREATOR_ROOT / "scripts" / "init_skill.py"
VALIDATE_SCRIPT = SYSTEM_SKILL_CREATOR_ROOT / "scripts" / "quick_validate.py"

DEFAULT_STOPWORDS = {
    "a",
    "al",
    "algo",
    "analiza",
    "analizar",
    "and",
    "ante",
    "con",
    "crear",
    "corregir",
    "de",
    "del",
    "despues",
    "ejecutar",
    "el",
    "en",
    "esta",
    "este",
    "for",
    "haz",
    "la",
    "las",
    "lo",
    "los",
    "mcum",
    "mejora",
    "mejorar",
    "mi",
    "necesito",
    "or",
    "para",
    "por",
    "que",
    "quiero",
    "real",
    "revisa",
    "revisar",
    "run",
    "sesion",
    "session",
    "skill",
    "task",
    "the",
    "una",
    "un",
    "usar",
    "use",
    "valida",
    "validar",
    "work",
    "workflow",
    "y",
}
GENERIC_SKILLS = {"kaizen", "mcum-orchestrator"}
DEFAULT_MIN_LIFECYCLE_SCORE = 0.78
DEFAULT_ACTIVATION_SCORE = 0.82
DEFAULT_ROLLBACK_SCORE = 0.55
DEFAULT_MIN_TESTING_USES = 2
DEFAULT_CANDIDATE_RETIREMENT_SCORE = 0.45
DEFAULT_CANDIDATE_RETIREMENT_IDLE_DAYS = 45


def _ascii_slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text.lower()


def normalize_skill_name(skill_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii_slug(skill_name).strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64]


def _title_case(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.split("-") if part)


def _clip(text: str, limit: int = 220) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _extract_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", _ascii_slug(text))
    keywords: list[str] = []
    for token in tokens:
        if len(token) < 4 or token in DEFAULT_STOPWORDS:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def _fingerprint_task(text: str) -> tuple[str, ...]:
    keywords = _extract_keywords(text)
    if not keywords:
        return tuple()
    return tuple(keywords[:4])


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped


def _sanitize_trigger_anti_lists(
    triggers: list[str],
    anti: list[str],
) -> tuple[list[str], list[str]]:
    cleaned_triggers = _dedupe_strings(triggers)
    trigger_keys = {str(value).strip().lower() for value in cleaned_triggers}
    cleaned_anti = [
        value
        for value in _dedupe_strings(anti)
        if str(value).strip().lower() not in trigger_keys
    ]
    return cleaned_triggers, cleaned_anti


def _ensure_signal_bucket(
    grouped: dict[tuple[str, ...], dict[str, Any]],
    fingerprint: tuple[str, ...],
) -> dict[str, Any]:
    return grouped.setdefault(
        fingerprint,
        {
            "fingerprint": list(fingerprint),
            "occurrences": 0,
            "confidence_total": 0.0,
            "confidence_count": 0,
            "failure_count": 0,
            "skills_seen": set(),
            "sample_tasks": [],
            "signal_sources": defaultdict(int),
        },
    )


def _register_signal_event(
    grouped: dict[tuple[str, ...], dict[str, Any]],
    *,
    task_text: str,
    source: str,
    skill_name: str | None = None,
    confidence: float | None = None,
    failed: bool = False,
) -> None:
    fingerprint = _fingerprint_task(task_text)
    if not fingerprint:
        return

    bucket = _ensure_signal_bucket(grouped, fingerprint)
    bucket["occurrences"] += 1
    bucket["signal_sources"][source] += 1
    if confidence is not None:
        bucket["confidence_total"] += float(confidence)
        bucket["confidence_count"] += 1
    if failed:
        bucket["failure_count"] += 1
    if skill_name:
        bucket["skills_seen"].add(str(skill_name))
    if task_text and task_text not in bucket["sample_tasks"] and len(bucket["sample_tasks"]) < 5:
        bucket["sample_tasks"].append(task_text)


def _suggest_skill_name(tokens: tuple[str, ...], existing_names: set[str]) -> str:
    base_tokens = list(tokens[:3]) or ["candidate", "skill"]
    suffix = "specialist"
    if len(base_tokens) == 1:
        suffix = "workflow"
    candidate = normalize_skill_name("-".join(base_tokens + [suffix]))
    if candidate and candidate not in existing_names:
        return candidate

    index = 2
    while True:
        fallback = normalize_skill_name(f"{candidate or 'generated-skill'}-{index}")
        if fallback not in existing_names:
            return fallback
        index += 1


def _family_key(value: str) -> str:
    slug = normalize_skill_name(value)
    if not slug:
        return ""
    slug = re.sub(r"-\d+$", "", slug)
    for suffix in ("-specialist", "-workflow"):
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)]
            break
    slug = re.sub(r"-\d+$", "", slug).strip("-")
    return slug


def _family_matches(skill_names: set[str], family_key: str) -> list[str]:
    if not family_key:
        return []
    return sorted(
        skill_name
        for skill_name in skill_names
        if _family_key(skill_name) == family_key
    )


def _coerce_sort_timestamp(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _candidate_canonical_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    metadata = dict(record.get("metadata") or {})
    validation = dict(metadata.get("validation") or {})
    avg_confidence = _float_or_default(record.get("avg_confidence"), 0.0) or 0.0
    experience_count = int(record.get("experience_count") or 0)
    active_test_count = int(record.get("active_test_count") or 0)
    validation_passed = 1 if validation.get("passed") else 0
    return (
        validation_passed,
        active_test_count,
        experience_count,
        avg_confidence,
        -len(str(record.get("skill_name") or "")),
        _coerce_sort_timestamp(record.get("discovered_at")),
    )


def consolidate_candidate_families(
    *,
    candidate_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records = list(candidate_records or list_skill_catalog(status="candidate"))
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        skill_name = str(record.get("skill_name") or "").strip()
        if not skill_name:
            continue
        family_key = _family_key(skill_name)
        if not family_key:
            continue
        families[family_key].append(record)

    consolidated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for family_key, members in sorted(families.items()):
        if len(members) <= 1:
            continue
        canonical = max(members, key=_candidate_canonical_sort_key)
        canonical_name = str(canonical.get("skill_name") or "").strip()
        duplicates = [
            member for member in members
            if str(member.get("skill_name") or "").strip() != canonical_name
        ]
        if not canonical_name or not duplicates:
            continue

        merged_aliases: list[str] = []
        for duplicate in duplicates:
            duplicate_name = str(duplicate.get("skill_name") or "").strip()
            if not duplicate_name:
                continue
            metadata_update = {
                "family_key": family_key,
                "merged_into": canonical_name,
                "merge_reason": "candidate_family_consolidation",
                "merged_at": utc_now_iso(),
            }
            applied = update_skill_status(duplicate_name, "deprecated", metadata_update)
            if not applied:
                skipped.append(
                    {
                        "family_key": family_key,
                        "canonical_skill": canonical_name,
                        "duplicate_skill": duplicate_name,
                        "reason": "status_update_failed",
                    }
                )
                continue
            merged_aliases.append(duplicate_name)
            consolidated.append(
                {
                    "family_key": family_key,
                    "canonical_skill": canonical_name,
                    "duplicate_skill": duplicate_name,
                    "status": "deprecated",
                }
            )

        if merged_aliases:
            canonical_metadata = dict(canonical.get("metadata") or {})
            existing_aliases = list(canonical_metadata.get("family_aliases") or [])
            family_aliases = _dedupe_strings(existing_aliases + merged_aliases)
            merge_skill_metadata(
                canonical_name,
                {
                    "family_key": family_key,
                    "family_aliases": family_aliases,
                    "family_consolidated_at": utc_now_iso(),
                },
            )

    return {
        "families_seen": len(families),
        "families_with_duplicates": sum(1 for members in families.values() if len(members) > 1),
        "consolidated": consolidated,
        "skipped": skipped,
    }


def _recommend_resources(sample_tasks: list[str]) -> list[str]:
    text = " ".join(sample_tasks).lower()
    resources = {"references"}
    if any(token in text for token in ("script", "batch", "sync", "pipeline", "cli", "automat", "convert")):
        resources.add("scripts")
    if any(token in text for token in ("template", "dashboard", "html", "ui", "ux", "presentacion")):
        resources.add("assets")
    return sorted(resources)


def _build_description(skill_name: str, sample_tasks: list[str]) -> str:
    snippets = "; ".join(_clip(task, 110) for task in sample_tasks[:3])
    topic = _title_case(skill_name).lower()
    return (
        f"Specialized workflow for recurring {topic} tasks. "
        f"Use when Codex needs to solve requests such as: {snippets}"
    )


def _build_skill_document(skill_name: str, description: str, sample_tasks: list[str], resources: list[str]) -> str:
    title = _title_case(skill_name)
    description_yaml = json.dumps(description or "", ensure_ascii=False)
    resource_line = ""
    if "references" in resources:
        resource_line = "\nRead [signals.md](./references/signals.md) when you need the observed task patterns and initial guardrails.\n"

    bullets = "\n".join(f"- {task}" for task in sample_tasks[:4]) or "- Bootstrap from recurring tasks recorded by MCUM."
    return (
        f"---\n"
        f"name: {skill_name}\n"
        f"description: {description_yaml}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"## Overview\n\n"
        f"Use this skill for the recurring workflow captured by MCUM. "
        f"Keep the skill lean, inspect source artifacts before editing, and validate every change.\n"
        f"{resource_line}"
        f"## Workflow\n\n"
        f"1. Confirm the exact artifact, system, or files involved before changing anything.\n"
        f"2. Inspect the current implementation and capture the local constraints.\n"
        f"3. Apply the smallest reliable change that solves the task.\n"
        f"4. Validate with the closest executable check available.\n"
        f"5. Record reusable pitfalls, commands, and outputs back into MCUM.\n\n"
        f"## Trigger Examples\n\n"
        f"{bullets}\n\n"
        f"## Boundaries\n\n"
        f"- Do not use this skill when another local specialist already owns the task.\n"
        f"- Escalate to `mcum-orchestrator` when the task spans multiple domains or needs cross-project memory.\n"
        f"- Keep detailed references in bundled resources instead of bloating this file.\n"
    )


def _build_signals_reference(skill_name: str, signal: dict[str, Any]) -> str:
    samples = "\n".join(f"- {task}" for task in signal.get("sample_tasks", []))
    return (
        f"# Signals For { _title_case(skill_name) }\n\n"
        f"## Why This Skill Exists\n\n"
        f"- occurrences: {signal.get('occurrences', 0)}\n"
        f"- avg_confidence: {signal.get('avg_confidence', 0.0):.2f}\n"
        f"- failure_count: {signal.get('failure_count', 0)}\n"
        f"- prior_skills: {', '.join(signal.get('skills_seen', [])) or 'n/a'}\n\n"
        f"## Sample Tasks\n\n"
        f"{samples or '- No sample tasks recorded.'}\n"
    )


def _manual_validate_skill(skill_dir: Path) -> tuple[bool, str]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md missing"
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return False, "SKILL.md missing YAML frontmatter"
    if "\nname:" not in text or "\ndescription:" not in text:
        return False, "SKILL.md missing required frontmatter fields"
    return True, "manual validation ok"


def _run_skill_creator_init(
    skill_name: str,
    description: str,
    resources: list[str],
) -> tuple[bool, str]:
    if not INIT_SCRIPT.exists():
        return False, "skill-creator init script unavailable"

    command = [
        sys.executable,
        str(INIT_SCRIPT),
        skill_name,
        "--path",
        str(SKILLS_ROOT),
        "--resources",
        ",".join(resources),
        "--interface",
        f"display_name={_title_case(skill_name)}",
        "--interface",
        f"short_description={_clip(description, 60)}",
        "--interface",
        f"default_prompt=Use {skill_name} to solve the task with local workspace context.",
    ]
    completed = subprocess.run(
        command,
        cwd=str(INIT_SCRIPT.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return completed.returncode == 0, output or f"exit_code={completed.returncode}"


def _run_skill_creator_validate(skill_dir: Path) -> tuple[bool, str]:
    if not VALIDATE_SCRIPT.exists():
        return _manual_validate_skill(skill_dir)

    completed = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(skill_dir)],
        cwd=str(VALIDATE_SCRIPT.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return completed.returncode == 0, output or f"exit_code={completed.returncode}"


def collect_skill_gap_signals(
    *,
    project_id: str | None = None,
    lookback_days: int = 30,
    min_occurrences: int = 2,
    low_confidence_threshold: float = 0.72,
) -> list[dict[str, Any]]:
    existing_names = {entry["skill_name"] for entry in discover_local_skills()}
    catalog_records: list[dict[str, Any]] = []
    try:
        catalog_records = list_skill_catalog()
    except Exception:
        catalog_records = []
    catalog_status_by_name = {
        str(record.get("skill_name") or ""): str(record.get("status") or "unknown")
        for record in catalog_records
        if record.get("skill_name")
    }
    candidate_names = {
        skill_name
        for skill_name, status in catalog_status_by_name.items()
        if status == "candidate"
    }
    active_names = {
        skill_name
        for skill_name, status in catalog_status_by_name.items()
        if status == "active"
    }
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            log_filters = [
                "log_type = 'task'",
                "created_at >= NOW() - (%s || ' days')::interval",
                "(confidence_score IS NULL OR confidence_score <= %s OR outcome = 'failure')",
            ]
            log_params: list[Any] = [str(int(lookback_days)), low_confidence_threshold]
            if project_id:
                log_filters.append("project_id = %s")
                log_params.append(project_id)

            cur.execute(
                f"""
                SELECT title, description, skill_used, outcome, confidence_score
                FROM project_registry.project_logs
                WHERE {' AND '.join(log_filters)}
                ORDER BY created_at DESC
                """,
                log_params,
            )

            for row in cur.fetchall():
                task_text = str(row.get("title") or row.get("description") or "").strip()
                _register_signal_event(
                    grouped,
                    task_text=task_text,
                    source="project_logs",
                    skill_name=row.get("skill_used"),
                    confidence=row.get("confidence_score"),
                    failed=row.get("outcome") == "failure",
                )

            retrieval_filters = [
                "created_at >= NOW() - (%s || ' days')::interval",
                "(final_confidence IS NULL OR final_confidence <= %s OR outcome_status = 'failure')",
            ]
            retrieval_params: list[Any] = [str(int(lookback_days)), low_confidence_threshold]
            if project_id:
                retrieval_filters.append("project_id = %s")
                retrieval_params.append(project_id)

            cur.execute(
                f"""
                SELECT input_context, skill_name, final_confidence, outcome_status, failure_reason
                FROM core_brain.retrieval_runs
                WHERE {' AND '.join(retrieval_filters)}
                ORDER BY created_at DESC
                """,
                retrieval_params,
            )
            for row in cur.fetchall():
                task_text = str(row.get("input_context") or row.get("failure_reason") or "").strip()
                _register_signal_event(
                    grouped,
                    task_text=task_text,
                    source="retrieval_runs",
                    skill_name=row.get("skill_name"),
                    confidence=row.get("final_confidence"),
                    failed=row.get("outcome_status") == "failure",
                )

    signals: list[dict[str, Any]] = []
    for fingerprint, data in grouped.items():
        if data["occurrences"] < min_occurrences:
            continue
        if any(skill not in GENERIC_SKILLS for skill in data["skills_seen"]):
            continue

        avg_conf = (
            data["confidence_total"] / data["confidence_count"]
            if data["confidence_count"]
            else 0.0
        )
        if data["failure_count"] == 0 and avg_conf > low_confidence_threshold:
            continue
        if len(data["sample_tasks"]) < min(2, min_occurrences) and data["failure_count"] == 0:
            continue
        suggested_name = _suggest_skill_name(fingerprint, existing_names)
        family_key = _family_key(suggested_name)
        local_family_matches = _family_matches(existing_names, family_key)
        candidate_family_matches = _family_matches(candidate_names, family_key)
        active_family_matches = _family_matches(active_names, family_key)
        family_covered = bool(local_family_matches or candidate_family_matches or active_family_matches)
        exact_match_exists = (
            suggested_name in existing_names
            or suggested_name in candidate_names
            or suggested_name in active_names
        )
        occurrence_signal = _bounded(data["occurrences"] / max(2.0, float(min_occurrences) + 1.0))
        failure_signal = _bounded(data["failure_count"] / max(1.0, float(data["occurrences"])))
        signal = {
            "fingerprint": list(fingerprint),
            "occurrences": data["occurrences"],
            "avg_confidence": round(avg_conf, 3),
            "failure_count": data["failure_count"],
            "skills_seen": sorted(data["skills_seen"]),
            "sample_tasks": data["sample_tasks"],
            "signal_sources": dict(sorted(data["signal_sources"].items())),
            "suggested_skill_name": suggested_name,
            "description": _build_description(suggested_name, data["sample_tasks"]),
            "resources": _recommend_resources(data["sample_tasks"]),
        }
        confidence_gap_signal = _bounded(
            (low_confidence_threshold - avg_conf) / max(low_confidence_threshold, 0.01)
        )
        source_diversity_signal = _bounded(len(signal["signal_sources"]) / 2.0)
        coverage_penalty = 0.0
        recommended_action = "bootstrap_candidate"
        if candidate_family_matches:
            coverage_penalty = 0.45
            recommended_action = "consolidate_existing_candidate"
        elif active_family_matches:
            coverage_penalty = 0.25
            recommended_action = "investigate_existing_active_skill"
        elif local_family_matches:
            coverage_penalty = 0.18
            recommended_action = "inspect_existing_local_skill"
        if exact_match_exists:
            coverage_penalty = max(coverage_penalty, 0.55)
            recommended_action = "reuse_existing_skill"

        actionability_score = round(
            _bounded(
                (occurrence_signal * 0.30)
                + (failure_signal * 0.35)
                + (confidence_gap_signal * 0.20)
                + (source_diversity_signal * 0.15)
                - coverage_penalty
            ),
            3,
        )
        signal["family_key"] = family_key
        signal["coverage"] = {
            "family_covered": family_covered,
            "exact_match_exists": exact_match_exists,
            "local_family_matches": local_family_matches[:5],
            "candidate_family_matches": candidate_family_matches[:5],
            "active_family_matches": active_family_matches[:5],
        }
        signal["actionability"] = {
            "score": actionability_score,
            "occurrence_signal": round(occurrence_signal, 3),
            "failure_signal": round(failure_signal, 3),
            "confidence_gap_signal": round(confidence_gap_signal, 3),
            "source_diversity_signal": round(source_diversity_signal, 3),
            "coverage_penalty": round(coverage_penalty, 3),
            "recommended_action": recommended_action,
        }
        existing_names.add(suggested_name)
        signals.append(signal)

    signals.sort(
        key=lambda item: (
            item.get("actionability", {}).get("score", 0.0),
            item["occurrences"],
            item["failure_count"],
            -item["avg_confidence"],
        ),
        reverse=True,
    )
    return signals


def _new_dispatch_hint_bucket() -> dict[str, Any]:
    return {
        "triggers": [],
        "anti": [],
        "samples": [],
        "negative_samples": [],
        "profile_fragments": [],
        "successful_overrides": 0,
        "overridden_by_forced": 0,
        "successful_implicit_corrections": 0,
        "overridden_implicitly": 0,
    }


def _hint_bucket(hints_by_skill: dict[str, dict[str, Any]], skill_name: str) -> dict[str, Any]:
    return hints_by_skill.setdefault(skill_name, _new_dispatch_hint_bucket())


def _merge_hint_buckets(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["triggers"].extend(source.get("triggers", []))
    target["anti"].extend(source.get("anti", []))
    target["samples"].extend(source.get("samples", []))
    target["negative_samples"].extend(source.get("negative_samples", []))
    target["profile_fragments"].extend(source.get("profile_fragments", []))
    target["successful_overrides"] += int(source.get("successful_overrides") or 0)
    target["overridden_by_forced"] += int(source.get("overridden_by_forced") or 0)
    target["successful_implicit_corrections"] += int(source.get("successful_implicit_corrections") or 0)
    target["overridden_implicitly"] += int(source.get("overridden_implicitly") or 0)


def _normalize_dispatch_hints(
    hints_by_skill: dict[str, dict[str, Any]],
    *,
    min_occurrences: int,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for skill_name, hint in hints_by_skill.items():
        triggers, anti = _sanitize_trigger_anti_lists(
            list(hint["triggers"]),
            list(hint["anti"]),
        )
        samples = _dedupe_strings(hint["samples"])[:5]
        negative_samples = _dedupe_strings(hint["negative_samples"])[:5]
        successful_overrides = int(hint.get("successful_overrides") or 0)
        overridden_by_forced = int(hint.get("overridden_by_forced") or 0)
        successful_implicit_corrections = int(hint.get("successful_implicit_corrections") or 0)
        overridden_implicitly = int(hint.get("overridden_implicitly") or 0)
        if (
            successful_overrides < min_occurrences
            and overridden_by_forced < min_occurrences
            and successful_implicit_corrections < min_occurrences
            and overridden_implicitly < min_occurrences
        ):
            continue
        priority_delta = 0
        if successful_overrides >= min_occurrences:
            priority_delta += min(2, successful_overrides - min_occurrences + 1)
        if overridden_by_forced >= min_occurrences:
            priority_delta -= min(2, overridden_by_forced - min_occurrences + 1)
        if successful_implicit_corrections >= min_occurrences:
            priority_delta += 1
        if overridden_implicitly >= min_occurrences:
            priority_delta -= 1
        sources: list[str] = []
        if successful_overrides or overridden_by_forced:
            sources.append("manual_override_shadow_dispatch")
        if successful_implicit_corrections or overridden_implicitly:
            sources.append("implicit_skill_correction")
        normalized[skill_name] = {
            "triggers": triggers,
            "anti": anti,
            "samples": samples,
            "negative_samples": negative_samples,
            "profile_fragments": _dedupe_strings(hint["profile_fragments"])[:3],
            "successful_overrides": successful_overrides,
            "overridden_by_forced": overridden_by_forced,
            "successful_implicit_corrections": successful_implicit_corrections,
            "overridden_implicitly": overridden_implicitly,
            "priority_delta": priority_delta,
            "updated_from": "+".join(sources),
        }
    return normalized


def _collect_forced_dispatch_hints(
    *,
    lookback_days: int = 30,
) -> dict[str, dict[str, Any]]:
    forced_sessions: dict[str, dict[str, Any]] = {}
    outcomes_by_session: dict[str, dict[str, Any]] = {}
    hints_by_skill: dict[str, dict[str, Any]] = {}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    skill_used,
                    log_metadata->>'session_id' AS session_id,
                    log_metadata->>'task_description' AS task_description,
                    log_metadata->>'dispatch_method' AS dispatch_method,
                    log_metadata->'auto_dispatch' AS auto_dispatch
                FROM project_registry.project_logs
                WHERE log_type = 'session_start'
                  AND created_at >= NOW() - (%s || ' days')::interval
                  AND COALESCE(log_metadata->>'dispatch_method', '') = 'forced_by_user'
                ORDER BY created_at DESC
                """,
                (str(int(lookback_days)),),
            )
            for row in cur.fetchall():
                session_id = str(row.get("session_id") or "").strip()
                skill_name = str(row.get("skill_used") or "").strip()
                task_description = str(row.get("task_description") or "").strip()
                if not session_id or not skill_name or skill_name in GENERIC_SKILLS or not task_description:
                    continue
                auto_dispatch = row.get("auto_dispatch") or {}
                if isinstance(auto_dispatch, str):
                    try:
                        auto_dispatch = json.loads(auto_dispatch)
                    except json.JSONDecodeError:
                        auto_dispatch = {}
                forced_sessions[session_id] = {
                    "skill_name": skill_name,
                    "task_description": task_description,
                    "auto_dispatch_skill": str(auto_dispatch.get("skill_name") or "").strip(),
                    "auto_dispatch_triggered_by": str(auto_dispatch.get("triggered_by") or "").strip(),
                }

            cur.execute(
                """
                SELECT
                    skill_used,
                    outcome,
                    confidence_score,
                    log_metadata->>'session_id' AS session_id
                FROM project_registry.project_logs
                WHERE log_type = 'task'
                  AND created_at >= NOW() - (%s || ' days')::interval
                ORDER BY created_at DESC
                """,
                (str(int(lookback_days)),),
            )
            for row in cur.fetchall():
                session_id = str(row.get("session_id") or "").strip()
                if not session_id:
                    continue
                outcomes_by_session[session_id] = {
                    "skill_used": row.get("skill_used"),
                    "outcome": row.get("outcome"),
                    "confidence_score": row.get("confidence_score"),
                }

    for session_id, forced in forced_sessions.items():
        outcome = outcomes_by_session.get(session_id)
        if not outcome or outcome.get("outcome") not in {"success", "partial"}:
            continue
        if str(outcome.get("skill_used") or "") != forced["skill_name"]:
            continue

        keywords = _extract_keywords(forced["task_description"])[:4]
        if not keywords:
            continue
        positive_hint = _hint_bucket(hints_by_skill, forced["skill_name"])
        positive_hint["successful_overrides"] += 1
        positive_hint["triggers"].extend(keywords[:3])
        positive_hint["samples"].append(forced["task_description"])
        positive_hint["profile_fragments"].append(forced["task_description"])

        auto_skill = forced.get("auto_dispatch_skill") or ""
        if auto_skill and auto_skill != forced["skill_name"]:
            negative_hint = _hint_bucket(hints_by_skill, auto_skill)
            negative_hint["overridden_by_forced"] += 1
            negative_hint["negative_samples"].append(forced["task_description"])
            negative_hint["anti"].extend(keywords[:2])
            trigger_source = forced.get("auto_dispatch_triggered_by") or ""
            if trigger_source and not trigger_source.startswith("semantic_score") and trigger_source != "no_match_found":
                negative_hint["anti"].append(trigger_source)

    return hints_by_skill


def _collect_implicit_dispatch_hints(
    *,
    lookback_days: int = 30,
) -> dict[str, dict[str, Any]]:
    hints_by_skill: dict[str, dict[str, Any]] = {}

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    title,
                    skill_used,
                    outcome,
                    confidence_score,
                    log_metadata->>'task_description' AS task_description,
                    log_metadata->>'selected_skill' AS selected_skill,
                    log_metadata->>'dispatch_method' AS dispatch_method,
                    log_metadata->'skill_correction' AS skill_correction
                FROM project_registry.project_logs
                WHERE log_type = 'task'
                  AND created_at >= NOW() - (%s || ' days')::interval
                ORDER BY created_at DESC
                """,
                (str(int(lookback_days)),),
            )
            for row in cur.fetchall():
                final_skill = str(row.get("skill_used") or "").strip()
                selected_skill = str(row.get("selected_skill") or "").strip()
                task_description = str(row.get("task_description") or row.get("title") or "").strip()
                dispatch_method = str(row.get("dispatch_method") or "").strip()
                if not final_skill or not selected_skill or not task_description:
                    continue
                if final_skill == selected_skill or final_skill in GENERIC_SKILLS:
                    continue
                if dispatch_method == "forced_by_user" or row.get("outcome") not in {"success", "partial"}:
                    continue

                skill_correction = row.get("skill_correction") or {}
                if isinstance(skill_correction, str):
                    try:
                        skill_correction = json.loads(skill_correction)
                    except json.JSONDecodeError:
                        skill_correction = {}
                if not skill_correction.get("implicit"):
                    continue

                keywords = _extract_keywords(task_description)[:4]
                if not keywords:
                    continue

                positive_hint = _hint_bucket(hints_by_skill, final_skill)
                positive_hint["successful_implicit_corrections"] += 1
                positive_hint["triggers"].extend(keywords[:3])
                positive_hint["samples"].append(task_description)
                positive_hint["profile_fragments"].append(task_description)

                negative_hint = _hint_bucket(hints_by_skill, selected_skill)
                negative_hint["overridden_implicitly"] += 1
                negative_hint["negative_samples"].append(task_description)
                negative_hint["anti"].extend(keywords[:3])

    return hints_by_skill


def collect_dispatch_hints(
    *,
    lookback_days: int = 30,
    min_occurrences: int = 2,
) -> dict[str, dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for source_hints in (
        _collect_forced_dispatch_hints(lookback_days=lookback_days),
        _collect_implicit_dispatch_hints(lookback_days=lookback_days),
    ):
        for skill_name, hint in source_hints.items():
            _merge_hint_buckets(_hint_bucket(combined, skill_name), hint)
    return _normalize_dispatch_hints(combined, min_occurrences=min_occurrences)


def apply_dispatch_hints(
    *,
    lookback_days: int = 30,
    min_occurrences: int = 2,
) -> list[dict[str, Any]]:
    hints = collect_dispatch_hints(
        lookback_days=lookback_days,
        min_occurrences=min_occurrences,
    )
    applied: list[dict[str, Any]] = []

    for skill_name, hint in hints.items():
        record = get_skill_record(skill_name)
        if not record:
            continue

        metadata = dict(record.get("metadata") or {})
        existing_hints = dict(metadata.get("dispatch_hints") or {})
        merged_triggers, merged_anti = _sanitize_trigger_anti_lists(
            list(existing_hints.get("triggers") or []) + list(hint.get("triggers") or []),
            list(existing_hints.get("anti") or []) + list(hint.get("anti") or []),
        )
        dispatch_hints = {
            "triggers": merged_triggers,
            "anti": merged_anti,
            "samples": _dedupe_strings(
                list(existing_hints.get("samples") or []) + list(hint.get("samples") or [])
            )[:5],
            "negative_samples": _dedupe_strings(
                list(existing_hints.get("negative_samples") or []) + list(hint.get("negative_samples") or [])
            )[:5],
            "profile_fragments": _dedupe_strings(
                list(existing_hints.get("profile_fragments") or []) + list(hint.get("profile_fragments") or [])
            )[:3],
            "successful_overrides": int(hint.get("successful_overrides") or 0),
            "overridden_by_forced": int(hint.get("overridden_by_forced") or 0),
            "successful_implicit_corrections": int(hint.get("successful_implicit_corrections") or 0),
            "overridden_implicitly": int(hint.get("overridden_implicitly") or 0),
            "priority_delta": int(hint.get("priority_delta") or 0),
            "updated_from": hint.get("updated_from") or "dispatch_learning",
            "updated_at": utc_now_iso(),
            "last_applied_at": utc_now_iso(),
        }
        if not merge_skill_metadata(skill_name, {"dispatch_hints": dispatch_hints}):
            continue

        applied.append(
            {
                "skill_name": skill_name,
                "triggers": dispatch_hints["triggers"],
                "anti": dispatch_hints["anti"],
                "samples": dispatch_hints["samples"],
                "negative_samples": dispatch_hints["negative_samples"],
                "successful_overrides": dispatch_hints["successful_overrides"],
                "overridden_by_forced": dispatch_hints["overridden_by_forced"],
                "successful_implicit_corrections": dispatch_hints["successful_implicit_corrections"],
                "overridden_implicitly": dispatch_hints["overridden_implicitly"],
                "priority_delta": dispatch_hints["priority_delta"],
            }
        )

    return applied


def bootstrap_candidate_skill(signal: dict[str, Any]) -> dict[str, Any]:
    skill_name = normalize_skill_name(signal["suggested_skill_name"])
    if not skill_name:
        return {"created": False, "reason": "invalid_skill_name", "signal": signal}

    skill_dir = SKILLS_ROOT / skill_name
    existing = get_skill_record(skill_name)
    if existing and skill_dir.exists():
        return {
            "created": False,
            "reason": "already_exists",
            "skill_name": skill_name,
            "skill_path": str(skill_dir),
            "status": existing.get("status", "unknown"),
        }

    resources = signal.get("resources") or ["references"]
    initialized, init_output = _run_skill_creator_init(
        skill_name,
        signal.get("description", ""),
        resources,
    )

    if not skill_dir.exists():
        skill_dir.mkdir(parents=True, exist_ok=True)

    if not initialized:
        for resource in resources:
            (skill_dir / resource).mkdir(exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        _build_skill_document(
            skill_name,
            signal.get("description", ""),
            signal.get("sample_tasks", []),
            resources,
        ),
        encoding="utf-8",
    )

    if "references" in resources:
        references_dir = skill_dir / "references"
        references_dir.mkdir(exist_ok=True)
        (references_dir / "signals.md").write_text(
            _build_signals_reference(skill_name, signal),
            encoding="utf-8",
        )

    is_valid, validation_output = _run_skill_creator_validate(skill_dir)
    metadata = {
        "generated_by": "skill-factory",
        "seed_source": "skill-creator",
        "initial_signal": signal,
        "validation": {
            "passed": is_valid,
            "output": validation_output,
        },
        "promotion_requirements": {
            "min_active_tests": 8,
            "min_successful_uses": 2,
            "min_success_rate": 0.75,
        },
        "missing_on_disk": False,
        "init_output": init_output,
    }
    upsert_skill_record(
        skill_name=skill_name,
        skill_dir_name=skill_dir.name,
        skill_path=str(skill_dir),
        source="generated",
        status="candidate" if is_valid else "blocked",
        description=signal.get("description"),
        metadata=metadata,
    )
    sync_skill_catalog()

    return {
        "created": True,
        "skill_name": skill_name,
        "skill_path": str(skill_dir),
        "resources": resources,
        "validated": is_valid,
        "validation_output": validation_output,
        "init_output": init_output,
        "status": "candidate" if is_valid else "blocked",
    }


def _float_or_default(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_in_days(value: Any) -> int | None:
    parsed = _parse_datetime(value)
    if not parsed:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return max(0, delta.days)


def _bounded(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _weighted_outcome(successes: int, partials: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((successes + (partials * 0.5)) / total, 3)


def _empty_performance_bucket(skill_name: str) -> dict[str, Any]:
    return {
        "skill_name": skill_name,
        "active_tests": 0,
        "latest_ckl": None,
        "latest_version_status": None,
        "latest_version": None,
        "final_uses": 0,
        "successes": 0,
        "partials": 0,
        "failures": 0,
        "confidence_total": 0.0,
        "confidence_count": 0,
        "selected_total": 0,
        "corrected_away": 0,
        "retrieval_total": 0,
        "retrieval_failures": 0,
        "project_buckets": {},
    }


def _project_bucket(metrics: dict[str, Any], project_key: str) -> dict[str, Any]:
    return metrics["project_buckets"].setdefault(
        project_key,
        {
            "final_uses": 0,
            "successes": 0,
            "partials": 0,
            "failures": 0,
            "confidence_total": 0.0,
            "confidence_count": 0,
        },
    )


def _project_score(bucket: dict[str, Any]) -> float:
    total = int(bucket.get("final_uses") or 0)
    weighted = _weighted_outcome(
        int(bucket.get("successes") or 0),
        int(bucket.get("partials") or 0),
        total,
    )
    avg_confidence = (
        float(bucket["confidence_total"]) / int(bucket["confidence_count"])
        if int(bucket.get("confidence_count") or 0) > 0
        else 0.5
    )
    return round(_bounded((weighted * 0.7) + (_bounded(avg_confidence) * 0.3)), 3)


def _performance_summary(
    metrics: dict[str, Any],
    *,
    min_active_tests: int,
    min_uses: int,
    project_id: str | None = None,
) -> dict[str, Any]:
    final_uses = int(metrics.get("final_uses") or 0)
    successes = int(metrics.get("successes") or 0)
    partials = int(metrics.get("partials") or 0)
    failures = int(metrics.get("failures") or 0)
    confidence_count = int(metrics.get("confidence_count") or 0)
    avg_confidence = (
        float(metrics["confidence_total"]) / confidence_count
        if confidence_count > 0
        else None
    )
    success_rate = round(successes / final_uses, 3) if final_uses else 0.0
    weighted_outcome_rate = _weighted_outcome(successes, partials, final_uses)
    selected_total = int(metrics.get("selected_total") or 0)
    corrected_away = int(metrics.get("corrected_away") or 0)
    correction_rate = round(corrected_away / selected_total, 3) if selected_total else 0.0
    correction_resilience = round(1.0 - correction_rate, 3) if selected_total else 0.5
    retrieval_total = int(metrics.get("retrieval_total") or 0)
    retrieval_failures = int(metrics.get("retrieval_failures") or 0)
    retrieval_reliability = round(1.0 - (retrieval_failures / retrieval_total), 3) if retrieval_total else 0.5
    usage_score = round(_bounded(final_uses / max(1, min_uses)), 3)
    test_score = round(_bounded(int(metrics.get("active_tests") or 0) / max(1, min_active_tests)), 3)
    ckl_score = _bounded(_float_or_default(metrics.get("latest_ckl"), 0.5) or 0.5)

    project_scores = {
        project_key: _project_score(bucket)
        for project_key, bucket in dict(metrics.get("project_buckets") or {}).items()
        if bucket.get("final_uses")
    }
    project_key = str(project_id) if project_id else None
    project_score = _float_or_default(project_scores.get(project_key), None) if project_key else None

    base_score = (
        weighted_outcome_rate * 0.30
        + _bounded(avg_confidence if avg_confidence is not None else 0.5) * 0.15
        + ckl_score * 0.15
        + retrieval_reliability * 0.10
        + correction_resilience * 0.10
        + usage_score * 0.10
        + test_score * 0.10
    )
    lifecycle_score = round((base_score * 0.85) + (project_score * 0.15), 3) if project_score is not None else round(base_score, 3)

    return {
        "active_tests": int(metrics.get("active_tests") or 0),
        "latest_ckl": _float_or_default(metrics.get("latest_ckl"), None),
        "latest_version": metrics.get("latest_version"),
        "latest_version_status": metrics.get("latest_version_status"),
        "final_uses": final_uses,
        "successes": successes,
        "partials": partials,
        "failures": failures,
        "success_rate": success_rate,
        "weighted_outcome_rate": weighted_outcome_rate,
        "avg_confidence": round(avg_confidence, 3) if avg_confidence is not None else None,
        "selected_total": selected_total,
        "corrected_away": corrected_away,
        "correction_rate": correction_rate,
        "correction_resilience": correction_resilience,
        "retrieval_total": retrieval_total,
        "retrieval_failures": retrieval_failures,
        "retrieval_reliability": retrieval_reliability,
        "usage_score": usage_score,
        "test_score": test_score,
        "project_scores": project_scores,
        "project_score": project_score,
        "lifecycle_score": lifecycle_score,
    }


def collect_skill_performance_metrics(
    *,
    skill_names: list[str] | None = None,
    lookback_days: int = 30,
    project_id: str | None = None,
    since: Any | None = None,
    min_active_tests: int = 8,
    min_uses: int = 2,
) -> dict[str, dict[str, Any]]:
    requested = {str(skill_name) for skill_name in list(skill_names or []) if skill_name}
    metrics_by_skill: dict[str, dict[str, Any]] = {
        skill_name: _empty_performance_bucket(skill_name)
        for skill_name in requested
    }

    def ensure_skill(skill_name: str | None) -> dict[str, Any] | None:
        normalized = str(skill_name or "").strip()
        if not normalized:
            return None
        if normalized not in metrics_by_skill:
            metrics_by_skill[normalized] = _empty_performance_bucket(normalized)
        return metrics_by_skill[normalized]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            test_filters = []
            test_params: list[Any] = []
            if requested:
                test_filters.append("skill_name = ANY(%s)")
                test_params.append(list(requested))
            cur.execute(
                f"""
                SELECT skill_name, COUNT(*) FILTER (WHERE is_active = TRUE) AS active_tests
                FROM core_brain.test_suite
                {"WHERE " + " AND ".join(test_filters) if test_filters else ""}
                GROUP BY skill_name
                """,
                test_params,
            )
            for row in cur.fetchall():
                bucket = ensure_skill(row.get("skill_name"))
                if bucket is None:
                    continue
                bucket["active_tests"] = int(row.get("active_tests") or 0)

            version_filters = []
            version_params: list[Any] = []
            if requested:
                version_filters.append("skill_name = ANY(%s)")
                version_params.append(list(requested))
            cur.execute(
                f"""
                SELECT DISTINCT ON (skill_name)
                    skill_name, version_semver, status, ckl_score, created_at
                FROM core_brain.skill_versions
                {"WHERE " + " AND ".join(version_filters) if version_filters else ""}
                ORDER BY skill_name, created_at DESC
                """,
                version_params,
            )
            for row in cur.fetchall():
                bucket = ensure_skill(row.get("skill_name"))
                if bucket is None:
                    continue
                bucket["latest_ckl"] = _float_or_default(row.get("ckl_score"), None)
                bucket["latest_version_status"] = row.get("status")
                bucket["latest_version"] = row.get("version_semver")

            task_filters = [
                "log_type = 'task'",
                "created_at >= NOW() - (%s || ' days')::interval",
            ]
            task_params: list[Any] = [str(int(lookback_days))]
            if since is not None:
                task_filters.append("created_at >= %s")
                task_params.append(since)
            if project_id:
                task_filters.append("project_id = %s")
                task_params.append(project_id)
            if requested:
                task_filters.append("(skill_used = ANY(%s) OR COALESCE(log_metadata->>'selected_skill', '') = ANY(%s))")
                task_params.extend([list(requested), list(requested)])

            cur.execute(
                f"""
                SELECT
                    project_id,
                    skill_used,
                    outcome,
                    confidence_score,
                    COALESCE(log_metadata->>'selected_skill', '') AS selected_skill
                FROM project_registry.project_logs
                WHERE {' AND '.join(task_filters)}
                ORDER BY created_at DESC
                """,
                task_params,
            )
            for row in cur.fetchall():
                final_skill = str(row.get("skill_used") or "").strip()
                selected_skill = str(row.get("selected_skill") or "").strip()
                outcome = str(row.get("outcome") or "").strip()
                confidence = _float_or_default(row.get("confidence_score"), None)
                project_key = str(row.get("project_id")) if row.get("project_id") else "unknown"

                final_bucket = ensure_skill(final_skill)
                if final_bucket is not None:
                    final_bucket["final_uses"] += 1
                    if outcome == "success":
                        final_bucket["successes"] += 1
                    elif outcome == "partial":
                        final_bucket["partials"] += 1
                    elif outcome == "failure":
                        final_bucket["failures"] += 1
                    project_bucket = _project_bucket(final_bucket, project_key)
                    project_bucket["final_uses"] += 1
                    if outcome == "success":
                        project_bucket["successes"] += 1
                    elif outcome == "partial":
                        project_bucket["partials"] += 1
                    elif outcome == "failure":
                        project_bucket["failures"] += 1
                    if confidence is not None:
                        final_bucket["confidence_total"] += confidence
                        final_bucket["confidence_count"] += 1
                        project_bucket["confidence_total"] += confidence
                        project_bucket["confidence_count"] += 1

                if selected_skill:
                    selected_bucket = ensure_skill(selected_skill)
                    if selected_bucket is not None:
                        selected_bucket["selected_total"] += 1
                        if final_skill and final_skill != selected_skill:
                            selected_bucket["corrected_away"] += 1

            retrieval_filters = [
                "created_at >= NOW() - (%s || ' days')::interval",
            ]
            retrieval_params: list[Any] = [str(int(lookback_days))]
            if since is not None:
                retrieval_filters.append("created_at >= %s")
                retrieval_params.append(since)
            if project_id:
                retrieval_filters.append("project_id = %s")
                retrieval_params.append(project_id)
            if requested:
                retrieval_filters.append("skill_name = ANY(%s)")
                retrieval_params.append(list(requested))

            cur.execute(
                f"""
                SELECT skill_name, outcome_status
                FROM core_brain.retrieval_runs
                WHERE {' AND '.join(retrieval_filters)}
                ORDER BY created_at DESC
                """,
                retrieval_params,
            )
            for row in cur.fetchall():
                bucket = ensure_skill(row.get("skill_name"))
                if bucket is None:
                    continue
                bucket["retrieval_total"] += 1
                if str(row.get("outcome_status") or "") == "failure":
                    bucket["retrieval_failures"] += 1

    return {
        skill_name: _performance_summary(
            metrics,
            min_active_tests=min_active_tests,
            min_uses=min_uses,
            project_id=project_id,
        )
        for skill_name, metrics in metrics_by_skill.items()
    }


def _update_skill_version_status_record(
    version_id: str,
    *,
    status: str,
    note: str | None = None,
) -> bool:
    if not version_id:
        return False

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.skill_versions
                SET status = %s,
                    changes_description = CASE
                        WHEN %s::text IS NULL OR %s::text = '' THEN changes_description
                        ELSE changes_description || E'\n' || %s::text
                    END
                WHERE id = %s
                """,
                (status, note, note, note, version_id),
            )
            return cur.rowcount > 0


def _build_performance_metadata(
    performance: dict[str, Any],
    *,
    review_status: str,
    review_reason: str,
    source_version: str | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    metadata = dict(performance)
    metadata["review_status"] = review_status
    metadata["review_reason"] = review_reason
    if source_version:
        metadata["source_version"] = source_version
    if lookback_days is not None:
        metadata["lookback_days"] = int(lookback_days)
    return {"performance": metadata}


def _build_retirement_metadata(
    performance: dict[str, Any],
    *,
    review_reason: str,
    retirement_reason: str,
    last_used_at: Any = None,
    last_improved_at: Any = None,
    active_family_matches: list[str] | None = None,
    retire_after_days: int | None = None,
) -> dict[str, Any]:
    metadata = _build_performance_metadata(
        performance,
        review_status="retired",
        review_reason=review_reason,
    )
    retirement = {
        "reason": retirement_reason,
        "retired_at": utc_now_iso(),
        "last_used_age_days": _age_in_days(last_used_at),
        "last_improved_age_days": _age_in_days(last_improved_at),
        "active_family_matches": list(active_family_matches or []),
    }
    if retire_after_days is not None:
        retirement["retire_after_days"] = int(retire_after_days)
    metadata["retirement"] = retirement
    return metadata


def _candidate_retirement_reason(
    record: dict[str, Any],
    performance: dict[str, Any],
    *,
    min_active_tests: int,
    min_successful_uses: int,
    min_lifecycle_score: float,
    active_family_matches: list[str] | None = None,
    retire_after_days: int = DEFAULT_CANDIDATE_RETIREMENT_IDLE_DAYS,
    retire_lifecycle_score: float = DEFAULT_CANDIDATE_RETIREMENT_SCORE,
) -> str | None:
    metadata = dict(record.get("metadata") or {})
    if metadata.get("missing_on_disk"):
        return "missing_on_disk"

    active_tests = int(performance.get("active_tests") or 0)
    successful_uses = int(performance.get("successes") or 0)
    total_uses = int(performance.get("final_uses") or 0)
    experience_count = int(record.get("experience_count") or 0)
    project_count = int(record.get("project_count") or 0)
    avg_confidence = _float_or_default(record.get("avg_confidence"), None)
    lifecycle_score = float(performance.get("lifecycle_score") or 0.0)
    last_used_age = _age_in_days(record.get("last_used_at"))
    last_improved_age = _age_in_days(record.get("last_improved_at"))
    discovered_age = _age_in_days(record.get("discovered_at"))
    synced_age = _age_in_days(record.get("last_synced_at"))
    stale_age = min(
        [age for age in (last_used_age, last_improved_age, discovered_age, synced_age) if age is not None],
        default=0,
    )

    no_evidence = (
        active_tests == 0
        and successful_uses == 0
        and total_uses == 0
        and experience_count == 0
        and project_count == 0
    )
    weak_signal = lifecycle_score < retire_lifecycle_score or (avg_confidence is not None and avg_confidence < 0.55)
    gated_out = (
        active_tests < min_active_tests
        and successful_uses < min_successful_uses
        and lifecycle_score < min_lifecycle_score
    )

    if stale_age >= retire_after_days and no_evidence and weak_signal:
        return "retired_stale_without_evidence"
    if active_family_matches and stale_age >= retire_after_days and gated_out and no_evidence:
        return "pruned_by_active_family_coverage"
    return None


def evaluate_candidate_promotion(
    skill_name: str,
    *,
    min_active_tests: int = 8,
    min_successful_uses: int = 2,
    min_success_rate: float = 0.75,
    min_lifecycle_score: float = DEFAULT_MIN_LIFECYCLE_SCORE,
    active_family_matches: list[str] | None = None,
    retire_after_days: int = DEFAULT_CANDIDATE_RETIREMENT_IDLE_DAYS,
    retire_lifecycle_score: float = DEFAULT_CANDIDATE_RETIREMENT_SCORE,
) -> dict[str, Any]:
    record = get_skill_record(skill_name)
    if not record:
        return {"skill_name": skill_name, "promoted": False, "reason": "missing_catalog_record"}

    resolved_skill_path = resolve_skill_path(record)
    if not resolved_skill_path:
        return {"skill_name": skill_name, "promoted": False, "reason": "missing_skill_path"}

    skill_dir = Path(resolved_skill_path)
    is_valid, validation_output = _run_skill_creator_validate(skill_dir)
    performance = collect_skill_performance_metrics(
        skill_names=[skill_name],
        min_active_tests=min_active_tests,
        min_uses=min_successful_uses,
    ).get(skill_name, {})

    active_tests = int(performance.get("active_tests") or 0)
    successful_uses = int(performance.get("successes") or 0)
    total_uses = int(performance.get("final_uses") or 0)
    success_rate = float(performance.get("success_rate") or 0.0)
    lifecycle_score = float(performance.get("lifecycle_score") or 0.0)

    metadata_update = {
        "validation": {
            "passed": is_valid,
            "output": validation_output,
        },
        "promotion_snapshot": {
            "active_tests": active_tests,
            "successful_uses": successful_uses,
            "total_uses": total_uses,
            "success_rate": success_rate,
            "lifecycle_score": lifecycle_score,
        },
        **_build_performance_metadata(
            performance,
            review_status="candidate_review",
            review_reason="candidate_promotion_gate",
        ),
    }

    if not is_valid:
        update_skill_status(skill_name, "blocked", metadata_update)
        return {
            "skill_name": skill_name,
            "promoted": False,
            "status": "blocked",
            "reason": "validation_failed",
            "active_tests": active_tests,
            "successful_uses": successful_uses,
            "success_rate": success_rate,
            "lifecycle_score": lifecycle_score,
        }

    retirement_reason = _candidate_retirement_reason(
        record,
        performance,
        min_active_tests=min_active_tests,
        min_successful_uses=min_successful_uses,
        min_lifecycle_score=min_lifecycle_score,
        active_family_matches=active_family_matches,
        retire_after_days=retire_after_days,
        retire_lifecycle_score=retire_lifecycle_score,
    )
    if retirement_reason:
        retire_skill_record(
            skill_name,
            reason=retirement_reason,
            metadata_update=_build_retirement_metadata(
                performance,
                review_reason="candidate_retirement_gate",
                retirement_reason=retirement_reason,
                last_used_at=record.get("last_used_at"),
                last_improved_at=record.get("last_improved_at"),
                active_family_matches=active_family_matches,
                retire_after_days=retire_after_days,
            ),
        )
        return {
            "skill_name": skill_name,
            "promoted": False,
            "retired": True,
            "status": "deprecated",
            "reason": retirement_reason,
            "active_tests": active_tests,
            "successful_uses": successful_uses,
            "success_rate": success_rate,
            "lifecycle_score": lifecycle_score,
        }

    if (
        active_tests >= min_active_tests
        and successful_uses >= min_successful_uses
        and success_rate >= min_success_rate
        and lifecycle_score >= min_lifecycle_score
    ):
        update_skill_status(skill_name, "active", metadata_update)
        return {
            "skill_name": skill_name,
            "promoted": True,
            "status": "active",
            "active_tests": active_tests,
            "successful_uses": successful_uses,
            "success_rate": success_rate,
            "lifecycle_score": lifecycle_score,
        }

    merge_skill_metadata(skill_name, metadata_update)
    return {
        "skill_name": skill_name,
        "promoted": False,
        "status": "candidate",
        "reason": "promotion_gate_not_met",
        "active_tests": active_tests,
        "successful_uses": successful_uses,
        "success_rate": success_rate,
        "lifecycle_score": lifecycle_score,
    }


def review_testing_skill_versions(
    *,
    project_id: str | None = None,
    lookback_days: int = 30,
    min_active_tests: int = 8,
    min_uses: int = DEFAULT_MIN_TESTING_USES,
    activation_score: float = DEFAULT_ACTIVATION_SCORE,
    rollback_score: float = DEFAULT_ROLLBACK_SCORE,
) -> dict[str, list[dict[str, Any]]]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT v.id, v.skill_name, v.version_semver, v.ckl_score, v.created_at
                FROM core_brain.skill_versions v
                JOIN (
                    SELECT skill_name, MAX(created_at) AS latest_created_at
                    FROM core_brain.skill_versions
                    GROUP BY skill_name
                ) latest
                    ON latest.skill_name = v.skill_name
                   AND latest.latest_created_at = v.created_at
                WHERE v.status = 'testing'
                ORDER BY v.skill_name
                """
            )
            versions = [dict(row) for row in cur.fetchall()]

    if not versions:
        return {
            "activated": [],
            "rolled_back": [],
            "monitoring": [],
        }

    skill_names = [str(row["skill_name"]) for row in versions if row.get("skill_name")]
    performance_map = collect_skill_performance_metrics(
        skill_names=skill_names,
        lookback_days=lookback_days,
        project_id=project_id,
        min_active_tests=min_active_tests,
        min_uses=min_uses,
    )

    activated: list[dict[str, Any]] = []
    rolled_back: list[dict[str, Any]] = []
    monitoring: list[dict[str, Any]] = []

    for version in versions:
        skill_name = str(version.get("skill_name") or "")
        if not skill_name:
            continue

        performance = dict(performance_map.get(skill_name) or {})
        lifecycle_score = float(performance.get("lifecycle_score") or 0.0)
        active_tests = int(performance.get("active_tests") or 0)
        final_uses = int(performance.get("final_uses") or 0)
        failures = int(performance.get("failures") or 0)
        successes = int(performance.get("successes") or 0)
        record = get_skill_record(skill_name)

        entry = {
            "skill_name": skill_name,
            "version": version.get("version_semver"),
            "version_id": str(version.get("id") or ""),
            "lifecycle_score": lifecycle_score,
            "active_tests": active_tests,
            "final_uses": final_uses,
            "success_rate": performance.get("success_rate"),
            "project_scores": performance.get("project_scores", {}),
        }

        if not record:
            entry["status"] = "monitoring"
            entry["reason"] = "missing_skill_record"
            monitoring.append(entry)
            continue

        if active_tests < min_active_tests or final_uses < min_uses:
            waiting_reasons: list[str] = []
            if active_tests < min_active_tests:
                waiting_reasons.append(f"tests<{min_active_tests}")
            if final_uses < min_uses:
                waiting_reasons.append(f"uses<{min_uses}")
            waiting_reason = ",".join(waiting_reasons) or "waiting_for_real_usage"
            merge_skill_metadata(
                skill_name,
                _build_performance_metadata(
                    performance,
                    review_status="testing",
                    review_reason=waiting_reason,
                    source_version=str(version.get("version_semver") or ""),
                    lookback_days=lookback_days,
                ),
            )
            entry["status"] = "testing"
            entry["reason"] = waiting_reason
            monitoring.append(entry)
            continue

        if lifecycle_score >= activation_score:
            _update_skill_version_status_record(
                str(version.get("id") or ""),
                status="active",
                note=(
                    f"[MCUM Promotion] lifecycle_score={lifecycle_score:.3f}; "
                    f"uses={final_uses}; active_tests={active_tests}"
                ),
            )
            update_skill_status(
                skill_name,
                "active",
                _build_performance_metadata(
                    performance,
                    review_status="active",
                    review_reason="testing_version_promoted",
                    source_version=str(version.get("version_semver") or ""),
                    lookback_days=lookback_days,
                ),
            )
            entry["status"] = "active"
            entry["promoted"] = True
            activated.append(entry)
            continue

        should_roll_back = lifecycle_score <= rollback_score or (failures > successes and final_uses >= min_uses)
        if should_roll_back:
            resolved_skill_path = resolve_skill_path(record)
            if not resolved_skill_path:
                rollback_applied = False
                skill_md_path = Path(skill_name) / "SKILL.md"
            else:
                skill_md_path = Path(resolved_skill_path) / "SKILL.md"
                backup_path = skill_md_path.with_suffix(skill_md_path.suffix + ".bak")
                rollback_applied = rollback_sisl_writeback(str(skill_md_path), str(backup_path))
            _update_skill_version_status_record(
                str(version.get("id") or ""),
                status="deprecated",
                note=(
                    f"[MCUM Rollback] lifecycle_score={lifecycle_score:.3f}; "
                    f"uses={final_uses}; rollback_applied={rollback_applied}"
                ),
            )
            update_skill_status(
                skill_name,
                "degraded",
                _build_performance_metadata(
                    performance,
                    review_status="rolled_back",
                    review_reason="testing_version_rolled_back",
                    source_version=str(version.get("version_semver") or ""),
                    lookback_days=lookback_days,
                ),
            )
            entry["status"] = "degraded"
            entry["rolled_back"] = rollback_applied
            rolled_back.append(entry)
            continue

        merge_skill_metadata(
            skill_name,
            _build_performance_metadata(
                performance,
                review_status="testing",
                review_reason="testing_version_under_observation",
                source_version=str(version.get("version_semver") or ""),
                lookback_days=lookback_days,
            ),
        )
        entry["status"] = "testing"
        entry["reason"] = "under_observation"
        monitoring.append(entry)

    return {
        "activated": activated,
        "rolled_back": rolled_back,
        "monitoring": monitoring,
    }


def get_dispatchable_skill_catalog() -> dict[str, dict[str, Any]]:
    try:
        return {
            record["skill_name"]: record
            for record in list_skill_catalog()
        }
    except Exception as exc:
        LOGGER.warning("Unable to load skill catalog for dispatch gating: %s", exc)
        return {}


def filter_dispatchable_skills(
    registry: list[dict],
    *,
    include_candidates: bool = False,
    project_context: dict | None = None,
) -> list[dict]:
    catalog_map = get_dispatchable_skill_catalog()
    if not catalog_map:
        return registry

    project_id = str(project_context.get("id")) if project_context and project_context.get("id") else None
    filtered: list[dict] = []
    for skill in registry:
        record = catalog_map.get(skill["name"], {})
        status = str(record.get("status") or "unknown")
        if status in {"blocked", "deprecated"}:
            continue
        if status == "candidate" and not include_candidates:
            continue

        item = dict(skill)
        metadata = dict(record.get("metadata") or {})
        routing_override = dict(metadata.get("routing_override") or {})
        if routing_override.get("enabled") is False:
            continue
        if routing_override.get("profile"):
            item["profile"] = str(routing_override.get("profile") or "").strip() or item.get("profile")
        if routing_override.get("priority") is not None:
            try:
                item["priority"] = int(routing_override.get("priority"))
            except (TypeError, ValueError):
                pass
        dispatch_hints = apply_dispatch_hint_freshness(metadata.get("dispatch_hints") or {})
        metadata["dispatch_hints"] = dispatch_hints
        item["triggers"], item["anti"] = _sanitize_trigger_anti_lists(
            list(item.get("triggers", []))
            + list(routing_override.get("triggers") or [])
            + list(dispatch_hints.get("triggers") or []),
            list(item.get("anti", []))
            + list(routing_override.get("anti") or [])
            + list(dispatch_hints.get("anti") or []),
        )
        profile_fragments = list(dispatch_hints.get("profile_fragments") or [])
        samples = list(dispatch_hints.get("samples") or [])
        if profile_fragments or samples:
            profile_addendum = " ".join(_dedupe_strings(profile_fragments + samples[:2]))
            if profile_addendum:
                item["profile"] = f"{item['profile']} {profile_addendum}".strip()
        item["status"] = status
        item["routing_source"] = str(routing_override.get("source") or item.get("routing_source") or "local")
        item["metadata"] = metadata
        item["priority"] = int(item.get("priority", 5)) + int(dispatch_hints.get("priority_delta", 0) or 0)
        performance = dict(metadata.get("performance") or {})
        lifecycle_score = _float_or_default(performance.get("lifecycle_score"), None)
        if lifecycle_score is not None:
            if lifecycle_score >= 0.85:
                item["priority"] += 1
            elif lifecycle_score <= 0.55:
                item["priority"] -= 1
        if project_id:
            project_scores = dict(performance.get("project_scores") or {})
            project_score = _float_or_default(project_scores.get(project_id), None)
            if project_score is not None:
                if project_score >= 0.85:
                    item["priority"] += 2
                elif project_score <= 0.55:
                    item["priority"] -= 2
        if status == "degraded":
            item["priority"] = max(0, int(item.get("priority", 5)) - 1)
        else:
            item["priority"] = max(0, int(item.get("priority", 5)))
        filtered.append(item)
    return filtered


def _truncate_items(
    items: list[dict[str, Any]],
    *,
    limit: int | None,
) -> tuple[list[dict[str, Any]], int]:
    if limit is None:
        return list(items), 0
    max_items = max(0, int(limit))
    if len(items) <= max_items:
        return list(items), 0
    return list(items[:max_items]), len(items) - max_items


def _build_cycle_journal() -> dict[str, Any]:
    return {
        "events": [],
        "applied": [],
        "planned": [],
        "reversible": [],
        "non_reversible": [],
        "touched_skills": [],
        "counts": {
            "applied": 0,
            "planned": 0,
            "promotions": 0,
            "retirements": 0,
            "consolidations": 0,
            "bootstrap_creations": 0,
            "testing_activations": 0,
            "testing_rollbacks": 0,
            "metadata_updates": 0,
        },
    }


def _append_cycle_journal_event(
    journal: dict[str, Any],
    *,
    action: str,
    skill_name: str | None = None,
    applied: bool,
    reversible: bool,
    status_before: str | None = None,
    status_after: str | None = None,
    reason: str | None = None,
    planned: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "action": action,
        "skill_name": skill_name,
        "applied": bool(applied),
        "planned": bool(planned),
        "reversible": bool(reversible),
        "status_before": status_before,
        "status_after": status_after,
        "reason": reason,
        "details": dict(details or {}),
    }
    journal["events"].append(entry)
    if applied:
        journal["applied"].append(entry)
        journal["counts"]["applied"] += 1
    if planned:
        journal["planned"].append(entry)
        journal["counts"]["planned"] += 1
    if reversible:
        journal["reversible"].append(entry)
    else:
        journal["non_reversible"].append(entry)
    if skill_name and skill_name not in journal["touched_skills"]:
        journal["touched_skills"].append(skill_name)

    if action == "candidate_promotion" and applied:
        journal["counts"]["promotions"] += 1
    elif action == "candidate_retirement" and applied:
        journal["counts"]["retirements"] += 1
    elif action == "candidate_consolidation" and applied:
        journal["counts"]["consolidations"] += 1
    elif action == "bootstrap_candidate" and applied:
        journal["counts"]["bootstrap_creations"] += 1
    elif action == "testing_activation" and applied:
        journal["counts"]["testing_activations"] += 1
    elif action == "testing_rollback" and applied:
        journal["counts"]["testing_rollbacks"] += 1
    elif applied:
        journal["counts"]["metadata_updates"] += 1
    return entry


def run_skill_factory_cycle(
    *,
    project_id: str | None = None,
    auto_bootstrap: bool = True,
    lookback_days: int = 30,
    min_occurrences: int = 2,
    low_confidence_threshold: float = 0.72,
    max_candidates: int = 1,
    min_active_tests: int = 8,
    min_successful_uses: int = 2,
    min_success_rate: float = 0.75,
    min_lifecycle_score: float = DEFAULT_MIN_LIFECYCLE_SCORE,
    min_testing_uses: int = DEFAULT_MIN_TESTING_USES,
    activation_score: float = DEFAULT_ACTIVATION_SCORE,
    rollback_score: float = DEFAULT_ROLLBACK_SCORE,
    max_candidate_ratio_for_bootstrap: float | None = None,
    max_pending_results: int | None = None,
    max_monitoring_results: int | None = None,
    consolidate_candidate_duplicates: bool = False,
) -> dict[str, Any]:
    sync_skill_catalog()
    applied_hints = apply_dispatch_hints(
        lookback_days=lookback_days,
        min_occurrences=min_occurrences,
    )
    initial_candidate_records = [
        record
        for record in list_skill_catalog(status="candidate")
        if not record.get("metadata", {}).get("missing_on_disk")
    ]
    consolidation_summary = {
        "families_seen": 0,
        "families_with_duplicates": 0,
        "consolidated": [],
        "skipped": [],
    }
    if consolidate_candidate_duplicates:
        consolidation_summary = consolidate_candidate_families(candidate_records=initial_candidate_records)
    candidate_records = [
        record
        for record in list_skill_catalog(status="candidate")
        if not record.get("metadata", {}).get("missing_on_disk")
    ]
    candidates = [record["skill_name"] for record in candidate_records]
    active_records = [
        record
        for record in list_skill_catalog(status="active")
        if not record.get("metadata", {}).get("missing_on_disk")
    ]
    active_skill_names = {
        str(record.get("skill_name") or "").strip()
        for record in active_records
        if str(record.get("skill_name") or "").strip()
    }
    initial_candidate_count = len(candidate_records)
    initial_active_count = len(active_records)
    initial_candidate_active_ratio = round(initial_candidate_count / max(1, initial_active_count), 4)
    bootstrap_allowed = bool(auto_bootstrap)
    bootstrap_block_reason = None
    if (
        bootstrap_allowed
        and max_candidate_ratio_for_bootstrap is not None
        and initial_candidate_active_ratio > float(max_candidate_ratio_for_bootstrap)
    ):
        bootstrap_allowed = False
        bootstrap_block_reason = (
            f"candidate_active_ratio={initial_candidate_active_ratio:.2f}"
            f">{float(max_candidate_ratio_for_bootstrap):.2f}"
        )

    promoted: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for skill_name in candidates:
        result = evaluate_candidate_promotion(
            skill_name,
            min_active_tests=min_active_tests,
            min_successful_uses=min_successful_uses,
            min_success_rate=min_success_rate,
            min_lifecycle_score=min_lifecycle_score,
            active_family_matches=_family_matches(active_skill_names, _family_key(skill_name)),
        )
        if result.get("promoted"):
            promoted.append(result)
        elif result.get("retired"):
            retired.append(result)
        else:
            pending.append(result)

    testing_reviews = review_testing_skill_versions(
        project_id=project_id,
        lookback_days=lookback_days,
        min_active_tests=min_active_tests,
        min_uses=min_testing_uses,
        activation_score=activation_score,
        rollback_score=rollback_score,
    )

    signals = collect_skill_gap_signals(
        project_id=project_id,
        lookback_days=lookback_days,
        min_occurrences=min_occurrences,
        low_confidence_threshold=low_confidence_threshold,
    )
    created: list[dict[str, Any]] = []
    if bootstrap_allowed:
        for signal in signals[:max(0, int(max_candidates))]:
            created.append(bootstrap_candidate_skill(signal))
    planned_bootstraps = signals[max(0, int(max_candidates)):] if bootstrap_allowed else signals[:max(0, int(max_candidates))]

    actionable_signals = [
        signal
        for signal in signals
        if str(signal.get("actionability", {}).get("recommended_action") or "") != "reuse_existing_skill"
    ]
    actionable_output, actionable_truncated = _truncate_items(actionable_signals, limit=3)
    pending_output, pending_truncated = _truncate_items(pending, limit=max_pending_results)
    monitoring = list(testing_reviews.get("monitoring", []))
    monitoring_output, monitoring_truncated = _truncate_items(
        monitoring,
        limit=max_monitoring_results,
    )
    final_candidate_records = [
        record
        for record in list_skill_catalog(status="candidate")
        if not record.get("metadata", {}).get("missing_on_disk")
    ]
    final_active_records = [
        record
        for record in list_skill_catalog(status="active")
        if not record.get("metadata", {}).get("missing_on_disk")
    ]
    testing_reviews_output = {
        **testing_reviews,
        "monitoring": monitoring_output,
        "monitoring_total": len(monitoring),
        "monitoring_truncated": monitoring_truncated,
    }
    journal = _build_cycle_journal()
    for item in applied_hints:
        _append_cycle_journal_event(
            journal,
            action="dispatch_hint_update",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=True,
            status_before=str(item.get("status_before") or item.get("status") or "unknown"),
            status_after=str(item.get("status_after") or item.get("status") or "unknown"),
            reason="dispatch_hints_applied",
            details={
                "priority_delta": item.get("priority_delta"),
                "triggers": item.get("triggers"),
                "anti": item.get("anti"),
                "source": item.get("source"),
            },
        )
    for item in consolidation_summary["consolidated"]:
        _append_cycle_journal_event(
            journal,
            action="candidate_consolidation",
            skill_name=item.get("canonical_skill"),
            applied=True,
            reversible=False,
            status_before="candidate",
            status_after="deprecated",
            reason="candidate_family_consolidation",
            details={
                "duplicate_skill": item.get("duplicate_skill"),
                "family_key": item.get("family_key"),
            },
        )
    for item in promoted:
        _append_cycle_journal_event(
            journal,
            action="candidate_promotion",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=True,
            status_before="candidate",
            status_after="active",
            reason=str(item.get("reason") or "promotion_gate_met"),
            details={
                "active_tests": item.get("active_tests"),
                "successful_uses": item.get("successful_uses"),
                "success_rate": item.get("success_rate"),
                "lifecycle_score": item.get("lifecycle_score"),
            },
        )
    for item in retired:
        _append_cycle_journal_event(
            journal,
            action="candidate_retirement",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=True,
            status_before="candidate",
            status_after="deprecated",
            reason=str(item.get("reason") or "retired"),
            details={
                "active_tests": item.get("active_tests"),
                "successful_uses": item.get("successful_uses"),
                "success_rate": item.get("success_rate"),
                "lifecycle_score": item.get("lifecycle_score"),
            },
        )
    for item in pending:
        _append_cycle_journal_event(
            journal,
            action="candidate_review_pending",
            skill_name=item.get("skill_name"),
            applied=False,
            reversible=True,
            status_before="candidate",
            status_after="candidate",
            reason=str(item.get("reason") or "pending_review"),
            planned=True,
            details={
                "active_tests": item.get("active_tests"),
                "successful_uses": item.get("successful_uses"),
                "success_rate": item.get("success_rate"),
                "lifecycle_score": item.get("lifecycle_score"),
            },
        )
    for item in created:
        _append_cycle_journal_event(
            journal,
            action="bootstrap_candidate",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=False,
            status_before="absent",
            status_after=str(item.get("status") or "candidate"),
            reason="bootstrap_candidate_created",
            details={
                "validated": item.get("validated"),
                "resources": item.get("resources"),
            },
        )
    for signal in planned_bootstraps:
        _append_cycle_journal_event(
            journal,
            action="bootstrap_candidate_planned",
            skill_name=signal.get("suggested_skill_name"),
            applied=False,
            reversible=False,
            planned=True,
            reason=(
                "auto_bootstrap_disabled"
                if not bootstrap_allowed
                else "bootstrap_budget_exhausted"
            ),
            details={
                "suggested_skill_name": signal.get("suggested_skill_name"),
                "actionability": signal.get("actionability"),
                "coverage": signal.get("coverage"),
            },
        )
    for item in testing_reviews.get("activated", []):
        _append_cycle_journal_event(
            journal,
            action="testing_activation",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=True,
            status_before="testing",
            status_after="active",
            reason="testing_version_promoted",
            details={
                "version": item.get("version"),
                "lifecycle_score": item.get("lifecycle_score"),
            },
        )
    for item in testing_reviews.get("rolled_back", []):
        _append_cycle_journal_event(
            journal,
            action="testing_rollback",
            skill_name=item.get("skill_name"),
            applied=True,
            reversible=False,
            status_before="testing",
            status_after="degraded",
            reason="testing_version_rolled_back",
            details={
                "version": item.get("version"),
                "lifecycle_score": item.get("lifecycle_score"),
                "rolled_back": item.get("rolled_back"),
            },
        )

    summary = {
        "signals": signals,
        "actionable_signals": actionable_output,
        "actionable_signals_total": len(actionable_signals),
        "actionable_signals_truncated": actionable_truncated,
        "created": created,
        "promoted": promoted,
        "retired": retired,
        "pending": pending_output,
        "pending_total": len(pending),
        "pending_truncated": pending_truncated,
        "applied_hints": applied_hints,
        "testing_reviews": testing_reviews_output,
        "catalog_pressure": {
            "candidate_count_before": initial_candidate_count,
            "active_count_before": initial_active_count,
            "candidate_active_ratio_before": initial_candidate_active_ratio,
            "candidate_count": len(final_candidate_records),
            "active_count": len(final_active_records),
            "candidate_active_ratio": round(len(final_candidate_records) / max(1, len(final_active_records)), 4),
            "auto_bootstrap_requested": bool(auto_bootstrap),
            "auto_bootstrap_applied": bootstrap_allowed,
            "bootstrap_block_reason": bootstrap_block_reason,
        },
        "candidate_lifecycle": {
            "promoted": len(promoted),
            "retired": len(retired),
            "pending": len(pending),
        },
        "journal": journal,
        "candidate_consolidation": {
            "enabled": bool(consolidate_candidate_duplicates),
            **consolidation_summary,
        },
    }

    if project_id and (
        created
        or promoted
        or applied_hints
        or testing_reviews["activated"]
        or testing_reviews["rolled_back"]
        or consolidation_summary["consolidated"]
    ):
        description_bits = []
        if created:
            description_bits.append(f"created={len(created)}")
        if promoted:
            description_bits.append(f"promoted={len(promoted)}")
        if retired:
            description_bits.append(f"retired={len(retired)}")
        if applied_hints:
            description_bits.append(f"hints={len(applied_hints)}")
        if testing_reviews["activated"]:
            description_bits.append(f"versions_activated={len(testing_reviews['activated'])}")
        if testing_reviews["rolled_back"]:
            description_bits.append(f"versions_rolled_back={len(testing_reviews['rolled_back'])}")
        if consolidation_summary["consolidated"]:
            description_bits.append(f"candidate_duplicates_merged={len(consolidation_summary['consolidated'])}")
        if bootstrap_block_reason:
            description_bits.append("bootstrap_blocked_by_candidate_pressure")
        log_entry(
            project_id=project_id,
            log_type="improvement",
            title="Skill factory cycle",
            description="; ".join(description_bits),
            skill_used="mcum-orchestrator",
            skills_orchestrated=[
                *[item["skill_name"] for item in applied_hints if item.get("skill_name")],
                *[item["skill_name"] for item in created if item.get("skill_name")],
                *[item["skill_name"] for item in promoted if item.get("skill_name")],
                *[item["skill_name"] for item in retired if item.get("skill_name")],
                *[item["skill_name"] for item in testing_reviews["activated"] if item.get("skill_name")],
                *[item["skill_name"] for item in testing_reviews["rolled_back"] if item.get("skill_name")],
            ],
            outcome="success",
            confidence_score=0.9,
            log_metadata=json.loads(json.dumps(summary, default=str)),
        )

    return summary
