"""
Preflight planning for the governed knowledge library.

The live MCUM runtime imports this module only to decide whether the
knowledge_library may participate in a task and which retrieval depth is safe.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FLAGS_FILE = ROOT / "directives" / "knowledge_library_flags.json"
POLICY_FILE = ROOT / "directives" / "knowledge_library_policy.json"


DEFAULT_FLAGS = {
    "enabled": False,
    "read_path_enabled": False,
    "write_path_enabled": False,
    "integration_mode": "off",
    "full_read_enabled": False,
}

DEFAULT_POLICY = {
    "citation_required": True,
    "summary_first": True,
    "max_summary_hits": 3,
    "max_chunk_hits": 4,
    "full_read_requires_explicit_request": True,
    "allowed_execution_modes": ["analizar", "proponer", "validar", "ejecutar"],
}


@dataclass
class KnowledgeLibraryPlan:
    enabled: bool
    allow_read_path: bool
    allow_write_path: bool
    integration_mode: str
    retrieval_depth: str
    citation_required: bool
    max_summary_hits: int
    max_chunk_hits: int
    full_read_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(defaults)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(defaults)
    merged = dict(defaults)
    merged.update(raw)
    return merged


def _normalize_flags(raw: dict[str, Any]) -> dict[str, Any]:
    if "flags" not in raw:
        return dict(raw)

    flags = dict(raw.get("flags") or {})
    enabled = bool(flags.get("knowledge_library_enabled", False))
    shadow_enabled = bool(flags.get("shadow_mode_enabled", False))
    cable_enabled = bool(flags.get("mcum_orchestrator_cable_enabled", False))

    integration_mode = "off"
    if cable_enabled:
        integration_mode = "active"
    elif shadow_enabled:
        integration_mode = "shadow"

    return {
        "enabled": enabled,
        "read_path_enabled": enabled and bool(flags.get("summary_first_retrieval_enabled", False)),
        "write_path_enabled": enabled and bool(flags.get("document_ingestion_enabled", False)),
        "integration_mode": integration_mode,
        "full_read_enabled": enabled and bool(flags.get("full_document_read_enabled", False)),
    }


def _normalize_policy(raw: dict[str, Any]) -> dict[str, Any]:
    if "retrieval" not in raw and "citation" not in raw:
        return dict(raw)

    retrieval = dict(raw.get("retrieval") or {})
    citation = dict(raw.get("citation") or {})
    anti_hallucination = dict(raw.get("anti_hallucination") or {})

    return {
        "citation_required": bool(citation.get("required", True)),
        "summary_first": str(retrieval.get("default_mode", "summary_first")) == "summary_first",
        "max_summary_hits": int(retrieval.get("max_sections_per_document", 3)),
        "max_chunk_hits": int(retrieval.get("max_chunks_per_section", 4)),
        "full_read_requires_explicit_request": bool(
            anti_hallucination.get("full_read_only_on_demand", True)
        ),
        "allowed_execution_modes": list(
            raw.get("allowed_execution_modes")
            or DEFAULT_POLICY["allowed_execution_modes"]
        ),
    }


def build_library_preflight(
    *,
    task_description: str,
    task_brief: dict[str, Any] | None = None,
    flags: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> KnowledgeLibraryPlan:
    resolved_flags = dict(DEFAULT_FLAGS)
    resolved_flags.update(_normalize_flags(flags or _load_json(FLAGS_FILE, DEFAULT_FLAGS)))

    resolved_policy = dict(DEFAULT_POLICY)
    resolved_policy.update(_normalize_policy(policy or _load_json(POLICY_FILE, DEFAULT_POLICY)))

    task_brief = dict(task_brief or {})
    execution_mode = str(task_brief.get("execution_mode") or "analizar")
    user_requested_full_read = bool(task_brief.get("knowledge_library_full_read", False))

    if execution_mode not in list(resolved_policy.get("allowed_execution_modes") or []):
        return KnowledgeLibraryPlan(
            enabled=False,
            allow_read_path=False,
            allow_write_path=False,
            integration_mode="off",
            retrieval_depth="none",
            citation_required=True,
            max_summary_hits=0,
            max_chunk_hits=0,
            full_read_allowed=False,
            reason=f"execution_mode_not_allowed:{execution_mode}",
        )

    if not bool(resolved_flags.get("enabled", False)):
        return KnowledgeLibraryPlan(
            enabled=False,
            allow_read_path=False,
            allow_write_path=False,
            integration_mode="off",
            retrieval_depth="none",
            citation_required=bool(resolved_policy.get("citation_required", True)),
            max_summary_hits=int(resolved_policy.get("max_summary_hits", 0)),
            max_chunk_hits=int(resolved_policy.get("max_chunk_hits", 0)),
            full_read_allowed=False,
            reason="library_disabled_by_flag",
        )

    allow_read_path = bool(resolved_flags.get("read_path_enabled", False))
    allow_write_path = bool(resolved_flags.get("write_path_enabled", False))
    integration_mode = str(resolved_flags.get("integration_mode", "off"))
    full_read_enabled = bool(resolved_flags.get("full_read_enabled", False))

    retrieval_depth = "summaries"
    if allow_read_path:
        retrieval_depth = "summaries_and_chunks"
    if allow_read_path and full_read_enabled and user_requested_full_read:
        retrieval_depth = "full_read"

    full_read_allowed = (
        allow_read_path
        and full_read_enabled
        and (
            not bool(resolved_policy.get("full_read_requires_explicit_request", True))
            or user_requested_full_read
        )
    )

    if not allow_read_path:
        reason = "library_isolated_until_manual_enable"
    elif retrieval_depth == "full_read":
        reason = "full_read_explicitly_requested"
    else:
        reason = "summary_first_read_path"

    return KnowledgeLibraryPlan(
        enabled=True,
        allow_read_path=allow_read_path,
        allow_write_path=allow_write_path,
        integration_mode=integration_mode,
        retrieval_depth=retrieval_depth,
        citation_required=bool(resolved_policy.get("citation_required", True)),
        max_summary_hits=int(resolved_policy.get("max_summary_hits", 3)),
        max_chunk_hits=int(resolved_policy.get("max_chunk_hits", 4)),
        full_read_allowed=full_read_allowed,
        reason=reason,
    )
