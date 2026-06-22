"""Build compact, project-first context envelopes and worker slices."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from ..db.project_registry import estimate_tokens
from ..db.unified_graph_store import persist_context_pack, query_unified_graph
from .context_query_planner import build_context_query_plan


def _compact_item(item: dict[str, Any], *, kind: str) -> dict[str, Any]:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    return {
        "id": str(item.get("id") or item.get("node_id") or ""),
        "kind": kind,
        "title": item.get("title") or item.get("name") or item.get("qualified_name"),
        "summary": (
            content.get("conclusion")
            or item.get("summary")
            or item.get("context_summary")
            or item.get("description")
        ),
        "relative_path": item.get("relative_path"),
        "qualified_name": item.get("qualified_name"),
        "line_start": item.get("line_start"),
        "line_end": item.get("line_end"),
        "confidence": item.get("current_confidence") or item.get("confidence") or item.get("score"),
        "metadata": {
            key: value
            for key, value in {
                "category": item.get("category"),
                "skill_name": item.get("skill_name"),
                "status": item.get("status"),
                "health_state": item.get("health_state"),
            }.items()
            if value not in (None, "", [], {})
        },
    }


def _trim_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [_compact_item(dict(item), kind=str(item.get("entity_type") or item.get("category") or "item")) for item in items[:limit]]


def build_project_context_envelope(
    *,
    session_id: str,
    project_id: str,
    project_name: str,
    project_path: str,
    task_description: str,
    task_brief: dict[str, Any],
    selected_skill: str,
    skill_status: str,
    code_graph_hits: list[dict[str, Any]] | None = None,
    experiences: list[dict[str, Any]] | None = None,
    active_patterns: list[dict[str, Any]] | None = None,
    failure_patterns: list[dict[str, Any]] | None = None,
    conflict_cases: list[dict[str, Any]] | None = None,
    knowledge_library_hits: list[dict[str, Any]] | None = None,
    graph_intelligence: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    graph_policy = dict((execution_policy or {}).get("graph_intelligence") or {})
    plan = build_context_query_plan(
        task_description,
        task_brief=task_brief,
        agent_role="coordinator",
        policy=graph_policy,
    )
    unified_result: dict[str, Any] = {"entities": [], "relations": []}
    if bool(graph_policy.get("enabled", True)):
        try:
            unified_result = query_unified_graph(
                project_id=project_id,
                query=plan["query"],
                limit=int(graph_policy.get("max_entities", 12) or 12),
                entity_types=list(plan.get("entity_types") or []),
            )
        except Exception as exc:
            unified_result = {"entities": [], "relations": [], "error": str(exc)}

    design_entities = [
        item for item in unified_result.get("entities", []) if item.get("entity_type") == "design_system"
    ]
    spec_entities = [
        item for item in unified_result.get("entities", []) if item.get("entity_type") == "spec_contract"
    ]
    primary_entities = [
        item
        for item in unified_result.get("entities", [])
        if item.get("entity_type") not in {"design_system", "spec_contract"}
    ]
    envelope = {
        "version": "1.0",
        "project": {"id": project_id, "name": project_name, "path": project_path},
        "snapshot": dict(graph_intelligence or {}),
        "query_plan": plan,
        "task_contract": {
            key: task_brief.get(key)
            for key in (
                "task_id",
                "task_type",
                "objective",
                "expected_deliverable",
                "success_criteria",
                "execution_mode",
                "risk_level",
                "validation_required",
            )
            if task_brief.get(key) not in (None, "", [], {})
        },
        "selected_skill": {"name": selected_skill, "status": skill_status},
        "graph_context": {
            "primary_entities": _trim_items(primary_entities, limit=8),
            "code_locations": _trim_items(list(code_graph_hits or []), limit=5),
            "relations": [
                {
                    "relation_type": item.get("relation_type"),
                    "source_title": item.get("source_title"),
                    "target_title": item.get("target_title"),
                    "confidence": item.get("confidence"),
                }
                for item in list(unified_result.get("relations") or [])[:10]
            ],
        },
        "operational_memory": {
            "experiences": _trim_items(list(experiences or []), limit=4),
            "patterns": _trim_items(list(active_patterns or []), limit=2),
            "failures": _trim_items(list(failure_patterns or []), limit=2),
            "conflicts": _trim_items(list(conflict_cases or []), limit=1),
        },
        "design_context": _trim_items(design_entities, limit=1) if plan.get("include_design_system") else [],
        "spec_test_context": _trim_items(spec_entities, limit=3) if plan.get("include_specs") else [],
        "knowledge_context": _trim_items(list(knowledge_library_hits or []), limit=2),
        "constraints": list(task_brief.get("constraints") or [])[:5],
        "references": list(task_brief.get("sources_to_review") or [])[:6],
        "warnings": [unified_result["error"]] if unified_result.get("error") else [],
        "trace": {"session_id": session_id, "project_first": True, "writeback": "coordinator_only"},
    }
    envelope["token_estimate"] = estimate_tokens(envelope)
    envelope["envelope_hash"] = hashlib.sha256(
        json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if persist and bool(graph_policy.get("persist_context_packs", True)):
        try:
            envelope["context_pack_id"] = persist_context_pack(
                project_id=project_id,
                session_id=session_id,
                agent_role="coordinator",
                task_query=plan["query"],
                envelope=envelope,
                token_budget=int(plan["token_budget"]),
                token_estimate=int(envelope["token_estimate"]),
                snapshot_id=str((graph_intelligence or {}).get("snapshot_id") or ""),
                metadata={"selected_skill": selected_skill, "intent": plan["primary_intent"]},
            )
        except Exception as exc:
            envelope["warnings"].append(f"context_pack_persist_failed:{exc}")
    return envelope


def build_worker_context_slice(
    envelope: dict[str, Any] | None,
    *,
    role: str,
    mode: str,
    max_tokens: int = 900,
) -> dict[str, Any]:
    source = deepcopy(envelope or {})
    if not source:
        return {}
    memory = dict(source.get("operational_memory") or {})
    graph = dict(source.get("graph_context") or {})
    slice_payload = {
        "version": source.get("version"),
        "envelope_hash": source.get("envelope_hash"),
        "context_pack_id": source.get("context_pack_id"),
        "graph_snapshot_id": (source.get("snapshot") or {}).get("snapshot_id"),
        "project": source.get("project"),
        "query_plan": {
            "primary_intent": (source.get("query_plan") or {}).get("primary_intent"),
            "project_first": True,
        },
        "task_contract": source.get("task_contract"),
        "selected_skill": source.get("selected_skill"),
        "code_locations": list(graph.get("code_locations") or [])[:4],
        "primary_entities": list(graph.get("primary_entities") or [])[:4],
        "experiences": list(memory.get("experiences") or [])[:3],
        "failures": list(memory.get("failures") or [])[:2],
        "patterns": list(memory.get("patterns") or [])[:1],
        "design_context": list(source.get("design_context") or [])[:1],
        "spec_test_context": list(source.get("spec_test_context") or [])[:2],
        "constraints": list(source.get("constraints") or [])[:4],
        "references": list(source.get("references") or [])[:4],
        "worker": {"role": role, "mode": mode, "writeback": "coordinator_only"},
    }
    trim_order = (
        "primary_entities",
        "experiences",
        "code_locations",
        "spec_test_context",
        "design_context",
        "patterns",
        "failures",
        "references",
    )
    while estimate_tokens(slice_payload) > max(200, int(max_tokens or 900)):
        changed = False
        for key in trim_order:
            items = slice_payload.get(key)
            if isinstance(items, list) and items:
                items.pop()
                changed = True
                break
        if not changed:
            break
    slice_payload["token_estimate"] = estimate_tokens(slice_payload)
    slice_payload["slice_hash"] = hashlib.sha256(
        json.dumps(slice_payload, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return slice_payload


def render_worker_context_slice(context_slice: dict[str, Any] | None, *, limit: int = 3600) -> str:
    if not context_slice:
        return "No project context slice was attached."
    text = json.dumps(context_slice, ensure_ascii=False, default=str, indent=2)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 48)].rstrip() + "\n...[context slice clipped]"


def render_project_context_envelope(envelope: dict[str, Any] | None, *, limit: int = 4200) -> str:
    if not envelope:
        return ""
    compact = {
        "envelope_hash": envelope.get("envelope_hash"),
        "context_pack_id": envelope.get("context_pack_id"),
        "snapshot": envelope.get("snapshot"),
        "query_plan": envelope.get("query_plan"),
        "selected_skill": envelope.get("selected_skill"),
        "graph_context": envelope.get("graph_context"),
        "design_context": envelope.get("design_context"),
        "spec_test_context": envelope.get("spec_test_context"),
        "warnings": envelope.get("warnings"),
    }
    text = json.dumps(compact, ensure_ascii=False, default=str, indent=2)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 48)].rstrip() + "\n...[project envelope clipped]"
