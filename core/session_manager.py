"""
Session lifecycle for the MCUM orchestrator.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .. import __version__
from ..anti_loop import (
    analyze_problem_loop,
    enrich_loop_state_with_strategy,
    finalize_loop_state,
    sanitize_loop_state,
)
from ..memory_freshness import build_project_structure_snapshots, build_source_snapshots
from ..logging_utils import get_logger
from ..db.experience_store import (
    adjust_confidence,
    finalize_retrieval_run,
    record_retrieval_run,
    retrieve_for_task,
    save_experience,
)
from ..db.knowledge_library_shadow import retrieve_knowledge_library_shadow
from ..db.code_graph_store import (
    infer_code_graph_filters,
    link_experience_to_code_graph,
    retrieve_code_graph_context,
)
from ..db.project_registry import (
    estimate_tokens,
    get_context_effectiveness_profile,
    get_dispatch_performance_profile,
    get_or_create_project,
    get_retrieval_scope_profile,
    log_entry,
    log_session_end,
    log_session_start,
)
from ..db.session_playbooks import retrieve_session_playbooks, save_session_playbook
from ..db.skill_catalog import get_skill_record, mark_skill_used, sync_skill_catalog
from ..db.pattern_store import record_pattern_usage_events
from ..db.unified_graph_store import sync_unified_project_graph
from .dispatcher import DispatchResult, dispatch
from .code_graph_sync import sync_project_code_graph
from .multi_agent import resolve_orchestration_context
from .project_context_orchestrator import (
    build_project_context_envelope,
    render_project_context_envelope,
)
from .skill_factory import run_skill_factory_cycle
from .state_compiler import CompiledState, compile_state
from ..policy import (
    apply_execution_profile,
    load_execution_policy,
    load_intake_policy,
    load_pattern_policy,
    normalize_task_brief,
    task_brief_metrics,
    validate_task_brief,
)
from ..sisl.autonomous_loop import run_autonomous_improvement

LOGGER = get_logger("core.session_manager")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+./:-]*", re.IGNORECASE)


def _tokenize_text(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return {token.lower() for token in _TOKEN_RE.findall(text) if len(token) >= 3}


def _normalize_pathish(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def _basename(path: Any) -> str:
    normalized = _normalize_pathish(path)
    if not normalized:
        return ""
    return Path(normalized).name.lower()


def _path_tokens(path: Any) -> set[str]:
    normalized = _normalize_pathish(path)
    if not normalized:
        return set()
    token_source = normalized.replace("/", " ").replace(".", " ").replace("-", " ").replace("_", " ")
    return _tokenize_text(token_source)


def _pattern_ids_from_items(items: list[dict[str, Any]] | None) -> list[str]:
    pattern_ids: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            candidate = item.get("id") or item.get("pattern_id")
        else:
            candidate = item
        cleaned = str(candidate or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            pattern_ids.append(cleaned)
    return pattern_ids


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            cleaned = str(item or "").strip()
            if cleaned:
                items.append(cleaned)
        return items
    return []


def _build_dispatch_hints_from_anti_loop(
    task_brief: dict[str, Any],
    anti_loop_state: dict[str, Any] | None,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    state = sanitize_loop_state(anti_loop_state)
    policy = dict(policy or {})
    if not state.get("enabled") or not bool(policy.get("rerank_dispatch_on_high_risk", True)):
        return {}

    loop_risk = float(state.get("loop_risk") or 0.0)
    recommendation = str(state.get("recommendation") or "").strip()
    warning_risk_threshold = float(policy.get("warning_risk_threshold", 0.35) or 0.35)
    preferred_skills: list[str] = []
    for item in [
        task_brief.get("preferred_dispatch_skill_hint"),
        task_brief.get("preferred_write_skill_hint"),
        *list(state.get("success_escape_skills") or []),
        *list(state.get("alternate_success_skills") or []),
    ]:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in preferred_skills:
            preferred_skills.append(cleaned)

    active = bool(preferred_skills) and (
        loop_risk >= warning_risk_threshold
        or recommendation in {
            "reuse_prior_success",
            "consider_alternate_successful_skill",
            "switch_strategy_before_retry",
            "increase_validation_and_diverge",
        }
    )
    if not active:
        return {}

    return {
        "enabled": True,
        "preferred_skills": preferred_skills[:3],
        "loop_risk": loop_risk,
        "recommendation": recommendation,
        "warning_risk_threshold": warning_risk_threshold,
        "preferred_score_boost": float(policy.get("dispatch_preference_score_boost", 0.08) or 0.08),
        "preferred_priority_boost": float(policy.get("dispatch_preference_priority_boost", 0.5) or 0.5),
    }


@dataclass
class TaskContext:
    session_id: str
    project_id: str
    project_name: str
    task_description: str
    skill_selected: str
    dispatch_result: DispatchResult
    auto_dispatch_result: DispatchResult | None = None
    retrieved_experiences: list[dict] = field(default_factory=list)
    failure_patterns: list[dict] = field(default_factory=list)
    conflict_cases: list[dict] = field(default_factory=list)
    active_patterns: list[dict] = field(default_factory=list)
    code_graph_hits: list[dict] = field(default_factory=list)
    code_graph_metadata: dict[str, Any] = field(default_factory=dict)
    knowledge_library_hits: list[dict] = field(default_factory=list)
    knowledge_library_mode: str = "disabled"
    knowledge_library_metadata: dict[str, Any] = field(default_factory=dict)
    feedback_signals: dict[str, Any] = field(default_factory=dict)
    retrieval_mode: str = "keywords_fallback"
    session_start_ts: float = field(default_factory=time.time)
    log_id: str | None = None
    retrieval_run_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    task_brief: dict = field(default_factory=dict)
    project_scope: str = "unknown"
    playbooks: list[dict] = field(default_factory=list)
    playbook_scope: str = "none"
    memory_governance: dict[str, Any] = field(default_factory=dict)
    playbook_memory_governance: dict[str, Any] = field(default_factory=dict)
    anti_loop: dict[str, Any] = field(default_factory=dict)
    retrieval_latency_ms: int = 0
    skill_status: str = "unknown"
    compiled_state: CompiledState | None = None
    retrieval_scope_learning: dict | None = None
    dispatch_learning_profile: dict | None = None
    dispatch_hints: dict | None = None
    orchestration: dict[str, Any] | None = None
    project_context_envelope: dict[str, Any] = field(default_factory=dict)
    graph_intelligence: dict[str, Any] = field(default_factory=dict)

    def to_context_block(self) -> str:
        if self.compiled_state is not None:
            compiled = self.compiled_state.to_context_block()
            envelope = render_project_context_envelope(self.project_context_envelope)
            if envelope:
                return compiled + "\n\n## MCUM project graph envelope\n" + envelope
            return compiled

        lines = [
            f"# MCUM context - session {self.session_id[:8]}",
            f"Project: {self.project_name}",
            f"Execution mode: {self.task_brief.get('execution_mode', 'ejecutar')}",
            f"Brief completeness: {self.task_brief.get('metrics', {}).get('completeness_score', 0):.2f}",
            f"Selected skill: {self.skill_selected} (confidence: {self.dispatch_result.confidence:.2f})",
            f"Skill status: {self.skill_status}",
            f"Selection method: {self.dispatch_result.match_method}",
            f"Retrieval mode: {self.retrieval_mode} [{self.project_scope}] in {self.retrieval_latency_ms}ms",
            "",
        ]
        if self.knowledge_library_mode != "disabled" or self.knowledge_library_hits:
            lines.insert(
                7,
                "Knowledge library: "
                f"{self.knowledge_library_mode} | hits={len(self.knowledge_library_hits)} "
                f"| tokens~{int(self.knowledge_library_metadata.get('tokens_used_estimate', 0) or 0)}",
            )

        if self.auto_dispatch_result and self.auto_dispatch_result.skill_name != self.skill_selected:
            lines.insert(
                6,
                "Auto-dispatch shadow: "
                f"{self.auto_dispatch_result.skill_name} "
                f"({self.auto_dispatch_result.match_method}, {self.auto_dispatch_result.confidence:.2f})",
            )

        if self.retrieved_experiences:
            lines.append(f"## Retrieved experiences ({len(self.retrieved_experiences)}):")
            for exp in self.retrieved_experiences:
                lines.extend(_render_experience(exp))

        if self.code_graph_hits:
            lines.append(f"\n## Code graph ({len(self.code_graph_hits)}):")
            for item in self.code_graph_hits:
                lines.extend(_render_experience(item, prefix="CODE"))

        if self.knowledge_library_hits:
            lines.append(f"\n## Knowledge library ({len(self.knowledge_library_hits)}):")
            for item in self.knowledge_library_hits:
                lines.extend(_render_experience(item, prefix="KNOW"))

        if self.failure_patterns:
            lines.append(f"\n## Failure patterns ({len(self.failure_patterns)}):")
            for exp in self.failure_patterns:
                lines.extend(_render_experience(exp, prefix="RISK"))

        if self.conflict_cases:
            lines.append(f"\n## Conflicts ({len(self.conflict_cases)}):")
            for exp in self.conflict_cases:
                lines.extend(_render_experience(exp, prefix="CONFLICT"))

        if self.active_patterns:
            lines.append(f"\n## Active patterns ({len(self.active_patterns)}):")
            for pattern in self.active_patterns:
                lines.extend(_render_pattern(pattern))

        if self.feedback_signals.get("signals"):
            summary = self.feedback_signals.get("summary") or {}
            lines.append(
                "\n## Human feedback "
                f"({summary.get('signals_n', len(self.feedback_signals.get('signals') or []))}):"
            )
            for signal in self.feedback_signals.get("signals", [])[:3]:
                lines.extend(_render_feedback_signal(signal))

        if self.playbooks:
            lines.append(f"\n## Session playbooks ({len(self.playbooks)}) [{self.playbook_scope}]:")
            for playbook in self.playbooks:
                lines.extend(_render_playbook(playbook))

        if self.warnings:
            lines.append("\n## Warnings:")
            for warning in self.warnings:
                lines.append(f"- {warning}")

        return "\n".join(lines)


def _render_experience(exp: dict, prefix: str | None = None) -> list[str]:
    heading = exp.get("title", "untitled")
    label = exp.get("category", "unknown")
    similarity = exp.get("_similarity")
    if prefix:
        line = f"- {prefix} [{label}] {heading}"
    else:
        line = f"- [{label}] {heading}"
    if similarity is not None:
        line += f" (sim={similarity:.2f})"

    lines = [line]
    content = exp.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if isinstance(content, dict):
        if content.get("conclusion"):
            lines.append(f"  conclusion: {content['conclusion']}")
        if content.get("context"):
            lines.append(f"  context: {content['context']}")
    applicability = exp.get("applicability")
    if isinstance(applicability, dict) and applicability.get("when"):
        lines.append(f"  use when: {applicability['when']}")
    not_applicable = exp.get("not_applicable_cases")
    if isinstance(not_applicable, dict) and not_applicable.get("when_not"):
        lines.append(f"  do not use when: {not_applicable['when_not']}")
    return lines


def _render_playbook(playbook: dict) -> list[str]:
    lines = [f"- [playbook] {playbook.get('title', 'untitled')} (sim={playbook.get('_similarity', 0):.2f})"]
    if playbook.get("objective"):
        lines.append(f"  objective: {playbook['objective']}")
    if playbook.get("output_summary"):
        lines.append(f"  summary: {playbook['output_summary']}")
    commands = playbook.get("commands") or []
    if commands:
        lines.append(f"  commands: {' | '.join(commands)}")
    files_touched = playbook.get("files_touched") or []
    if files_touched:
        lines.append(f"  files: {' | '.join(files_touched[:4])}")
    if playbook.get("reusable_when"):
        lines.append(f"  reuse when: {playbook['reusable_when']}")
    return lines


def _render_pattern(pattern: dict) -> list[str]:
    lines = [f"- [pattern] {pattern.get('name', 'untitled')} (sim={pattern.get('_combined_score', 0):.2f})"]
    if pattern.get("description"):
        lines.append(f"  description: {pattern['description']}")
    if pattern.get("evidence_count") is not None:
        lines.append(f"  evidence_count: {pattern['evidence_count']}")
    if pattern.get("_utility_reasons"):
        lines.append(f"  reasons: {' | '.join(str(reason) for reason in pattern['_utility_reasons'])}")
    return lines


def _render_feedback_signal(signal: dict) -> list[str]:
    lines = [
        f"- [feedback] {signal.get('id', 'unknown')} "
        f"(feedback={signal.get('user_feedback', 0):+d}, outcome={signal.get('outcome_status', 'unknown')})"
    ]
    if signal.get("decision_taken"):
        lines.append(f"  decision: {signal['decision_taken']}")
    if signal.get("failure_reason"):
        lines.append(f"  failure: {signal['failure_reason']}")
    return lines


@dataclass
class TaskResult:
    task_description: str
    skill_used: str
    outcome: str
    confidence_score: float
    output_summary: str | None = None
    artifacts: list[dict] = field(default_factory=list)
    error_description: str | None = None
    experience_data: dict | None = None
    validation_summary: str | None = None
    context_tokens_out: int | None = None
    playbook_data: dict | None = None
    skills_orchestrated: list[str] = field(default_factory=list)
    correction_source: str | None = None
    user_feedback: int | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


class OrchestratorSession:
    def __init__(
        self,
        project_path: str,
        task_description: str,
        project_name: str | None = None,
        tech_stack: dict | None = None,
        force_skill: str | None = None,
        verbose: bool = True,
        auto_improve: bool = True,
        task_brief: dict | None = None,
    ) -> None:
        self.project_path = project_path
        self.task_description = task_description
        self.project_name = project_name
        self.tech_stack = tech_stack or {}
        self.force_skill = force_skill
        self.verbose = verbose
        self.auto_improve = auto_improve
        self.intake_policy = load_intake_policy()
        self.task_brief = normalize_task_brief(project_path, task_description, task_brief=task_brief)
        self.execution_policy = apply_execution_profile(load_execution_policy(), self.task_brief)
        self.task_brief["execution_profile"] = self.execution_policy.get("_execution_profile", "full")
        controls = dict(self.execution_policy.get("_execution_profile_controls") or {})
        if bool(controls.get("no_auto_improve", False)):
            self.auto_improve = False
        if bool(controls.get("suppress_autonomy_hooks", False)):
            self.task_brief["suppress_autonomy_hooks"] = True
        self.task_brief["metrics"] = task_brief_metrics(self.task_brief, self.intake_policy)
        self.orchestration_context = resolve_orchestration_context(
            self.task_brief,
            self.execution_policy,
        )
        self.session_id = str(uuid.uuid4())
        self._start_ts: float | None = None
        self._ctx: TaskContext | None = None
        self._project: dict | None = None
        self._closed = False

    def _log(self, message: str) -> None:
        if self.verbose:
            LOGGER.info("[MCUM %s] %s", self.session_id[:6], message)

    def _dispatch_result_payload(self, result: DispatchResult | None) -> dict | None:
        if result is None:
            return None
        return {
            "skill_name": result.skill_name,
            "confidence": result.confidence,
            "match_method": result.match_method,
            "triggered_by": result.triggered_by,
            "semantic_score": result.semantic_score,
            "alternatives": result.alternatives[:3],
            "warnings": result.warnings[:3],
        }

    def _collect_context_items(self) -> list[dict[str, Any]]:
        if self._ctx is None:
            return []

        selected_lookup: dict[tuple[str, str], dict[str, Any]] = {}
        if self._ctx.compiled_state is not None:
            selected_items = getattr(self._ctx.compiled_state, "selected_items", {}) or {}
            for section, items in selected_items.items():
                for item in items:
                    identifier = str(item.get("id") or "").strip()
                    if identifier:
                        selected_lookup[(section, identifier)] = item

        items_by_section = {
            "experiences": getattr(self._ctx, "retrieved_experiences", []),
            "code_graph": getattr(self._ctx, "code_graph_hits", []),
            "knowledge_library": getattr(self._ctx, "knowledge_library_hits", []),
            "failure_patterns": getattr(self._ctx, "failure_patterns", []),
            "conflict_cases": getattr(self._ctx, "conflict_cases", []),
            "active_patterns": getattr(self._ctx, "active_patterns", []),
            "playbooks": getattr(self._ctx, "playbooks", []),
        }
        collected: list[dict[str, Any]] = []
        for section, items in items_by_section.items():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("id") or "").strip()
                selected_item = selected_lookup.get((section, identifier))
                merged = dict(item)
                if selected_item:
                    merged.update(selected_item)
                collected.append(
                    {
                        "section": section,
                        "selected": bool(selected_item) or self._ctx.compiled_state is None,
                        "item": merged,
                    }
                )
        return collected

    def _context_item_paths(self, section: str, item: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        if section == "playbooks":
            for path in item.get("files_touched") or []:
                normalized = _normalize_pathish(path)
                if normalized:
                    paths.append(normalized)
            return paths

        for artifact in item.get("source_artifacts") or []:
            if isinstance(artifact, dict):
                normalized = _normalize_pathish(artifact.get("path"))
                if normalized:
                    paths.append(normalized)

        for ref in item.get("evidence_refs") or []:
            if isinstance(ref, dict):
                normalized = _normalize_pathish(ref.get("path") or ref.get("file"))
            else:
                normalized = _normalize_pathish(ref)
            if normalized:
                paths.append(normalized)
        return paths

    def _context_item_text(self, section: str, item: dict[str, Any]) -> str:
        if section == "playbooks":
            payload = {
                "title": item.get("title"),
                "objective": item.get("objective"),
                "output_summary": item.get("output_summary"),
                "reusable_when": item.get("reusable_when"),
                "commands": item.get("commands"),
                "files_touched": item.get("files_touched"),
            }
            return json.dumps(payload, ensure_ascii=False, default=str)

        if section == "active_patterns":
            payload = {
                "name": item.get("name"),
                "description": item.get("description"),
                "category": item.get("category"),
                "evidence_ids": item.get("evidence_ids"),
                "evidence_projects": item.get("evidence_projects"),
                "evidence_skills": item.get("evidence_skills"),
            }
            return json.dumps(payload, ensure_ascii=False, default=str)

        content = item.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {"raw": content}
        payload = {
            "title": item.get("title"),
            "category": item.get("category"),
            "content": content,
            "applicability": item.get("applicability"),
            "not_applicable_cases": item.get("not_applicable_cases"),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _result_paths(self, result: TaskResult) -> list[str]:
        paths: list[str] = []
        files_touched = list((result.playbook_data or {}).get("files_touched") or [])
        for path in files_touched:
            normalized = _normalize_pathish(path)
            if normalized:
                paths.append(normalized)
        for artifact in result.artifacts:
            normalized = _normalize_pathish(artifact.get("path"))
            if normalized:
                paths.append(normalized)
        return paths

    def _evaluate_context_effectiveness(self, result: TaskResult) -> dict[str, Any]:
        if self._ctx is None:
            return {"items": [], "summary": {}}

        result_paths = self._result_paths(result)
        result_basenames = {_basename(path) for path in result_paths if _basename(path)}
        result_path_tokens = set().union(*(_path_tokens(path) for path in result_paths)) if result_paths else set()
        result_tokens = (
            _tokenize_text(result.task_description)
            | _tokenize_text(result.output_summary)
            | _tokenize_text(result.validation_summary)
            | _tokenize_text(result.error_description)
            | result_path_tokens
        )
        error_tokens = _tokenize_text(result.error_description)
        items: list[dict[str, Any]] = []

        for entry in self._collect_context_items():
            section = str(entry["section"])
            selected = bool(entry["selected"])
            item = dict(entry["item"])
            item_paths = self._context_item_paths(section, item)
            item_basenames = {_basename(path) for path in item_paths if _basename(path)}
            item_path_tokens = set().union(*(_path_tokens(path) for path in item_paths)) if item_paths else set()
            item_tokens = _tokenize_text(self._context_item_text(section, item)) | item_path_tokens

            basename_overlap = (
                len(result_basenames & item_basenames) / max(1, len(result_basenames))
                if result_basenames and item_basenames
                else 0.0
            )
            path_token_overlap = (
                len(result_path_tokens & item_path_tokens) / max(1, len(result_path_tokens))
                if result_path_tokens and item_path_tokens
                else 0.0
            )
            artifact_match = max(basename_overlap, path_token_overlap)
            token_overlap = (
                len(result_tokens & item_tokens) / max(1, min(len(result_tokens), len(item_tokens)))
                if result_tokens and item_tokens
                else 0.0
            )
            error_overlap = (
                len(error_tokens & item_tokens) / max(1, min(len(error_tokens), len(item_tokens)))
                if error_tokens and item_tokens
                else 0.0
            )
            skill_match = 1.0 if str(item.get("skill_name") or "") == str(result.skill_used or "") else 0.0
            utility_score = float(item.get("_utility_score") or 0.0)
            utility_norm = min(1.0, utility_score)

            support_score = round(
                (artifact_match * 0.45)
                + (token_overlap * 0.28)
                + (skill_match * 0.10)
                + (utility_norm * 0.12)
                + (
                    (error_overlap * 0.05)
                    if section in {"failure_patterns", "conflict_cases"} and result.outcome == "failure"
                    else 0.0
                ),
                4,
            )

            if selected:
                if support_score >= 0.72:
                    effectiveness = "high"
                elif support_score >= 0.50:
                    effectiveness = "medium"
                elif support_score >= 0.32:
                    effectiveness = "low"
                else:
                    effectiveness = "miss"
            else:
                if support_score >= 0.55:
                    effectiveness = "missed_opportunity"
                elif support_score >= 0.32:
                    effectiveness = "near_miss"
                else:
                    effectiveness = "irrelevant"

            delta = 0.0
            if section != "playbooks":
                if result.outcome == "success":
                    if effectiveness == "high":
                        delta = 0.04
                    elif effectiveness == "medium":
                        delta = 0.025
                    elif effectiveness == "low":
                        delta = 0.01
                    elif selected:
                        delta = -0.01
                elif result.outcome == "partial":
                    if effectiveness == "high":
                        delta = 0.02
                    elif effectiveness == "medium":
                        delta = 0.01
                    elif selected and effectiveness == "miss":
                        delta = -0.01
                elif result.outcome == "failure":
                    if section in {"failure_patterns", "conflict_cases"} and effectiveness in {"high", "medium"}:
                        delta = 0.01
                    elif selected and effectiveness == "miss":
                        delta = -0.05
                    elif selected:
                        delta = -0.03
                else:
                    if selected and effectiveness == "miss":
                        delta = -0.02

            items.append(
                {
                    "id": str(item.get("id")) if item.get("id") is not None else None,
                    "title": item.get("title") or item.get("objective") or item.get("output_summary"),
                    "section": section,
                    "selected": selected,
                    "effectiveness": effectiveness,
                    "support_score": support_score,
                    "artifact_match": round(artifact_match, 4),
                    "token_overlap": round(token_overlap, 4),
                    "error_overlap": round(error_overlap, 4),
                    "skill_match": bool(skill_match),
                    "utility_score": round(utility_score, 4),
                    "utility_reasons": list(item.get("_utility_reasons") or [])[:4],
                    "freshness_state": item.get("_freshness_state"),
                    "delta": round(delta, 4),
                }
            )

        items.sort(
            key=lambda item: (
                bool(item.get("selected")),
                float(item.get("support_score") or 0.0),
                float(item.get("delta") or 0.0),
            ),
            reverse=True,
        )
        summary = {
            "items_evaluated": len(items),
            "selected_items": sum(1 for item in items if item.get("selected")),
            "items_adjusted": sum(1 for item in items if abs(float(item.get("delta") or 0.0)) > 0),
            "high_value_selected": sum(
                1 for item in items if item.get("selected") and item.get("effectiveness") in {"high", "medium"}
            ),
            "missed_opportunities": sum(1 for item in items if item.get("effectiveness") == "missed_opportunity"),
            "top_helpful": [
                {
                    "id": str(item.get("id")) if item.get("id") is not None else None,
                    "title": item.get("title"),
                    "section": item.get("section"),
                    "effectiveness": item.get("effectiveness"),
                    "support_score": item.get("support_score"),
                }
                for item in items
                if item.get("selected") and item.get("effectiveness") in {"high", "medium"}
            ][:3],
            "top_missed": [
                {
                    "id": str(item.get("id")) if item.get("id") is not None else None,
                    "title": item.get("title"),
                    "section": item.get("section"),
                    "effectiveness": item.get("effectiveness"),
                    "support_score": item.get("support_score"),
                }
                for item in items
                if item.get("effectiveness") == "missed_opportunity"
            ][:3],
        }
        return {"items": items, "summary": summary}

    def _reinforce_retrieval_confidence(
        self,
        outcome: str,
        context_effectiveness: dict[str, Any] | None = None,
    ) -> None:
        if self._ctx is None:
            return

        feedback_items = list((context_effectiveness or {}).get("items") or [])
        selected_feedback_ids: set[str] = set()
        if feedback_items:
            adjusted = 0
            for item in feedback_items:
                experience_id = str(item.get("id") or "").strip()
                section = str(item.get("section") or "")
                delta = float(item.get("delta") or 0.0)
                if (
                    not experience_id
                    or section in {"playbooks", "active_patterns", "knowledge_library"}
                    or experience_id.startswith("kl-")
                    or delta == 0.0
                ):
                    continue
                adjust_confidence(
                    experience_id=experience_id,
                    delta=delta,
                    revalidated=True,
                    new_context=delta > 0,
                )
                adjusted += 1
                selected_feedback_ids.add(experience_id)

        pattern_linked_ids: set[str] = {
            str(evidence_id).strip()
            for pattern in self._ctx.active_patterns
            for evidence_id in (pattern.get("evidence_ids") or [])
            if str(evidence_id).strip()
        }
        pattern_linked_ids.difference_update(selected_feedback_ids)
        if pattern_linked_ids:
            pattern_delta = 0.0
            if outcome == "success":
                pattern_delta = 0.02
            elif outcome == "partial":
                pattern_delta = 0.01
            elif outcome == "failure":
                pattern_delta = -0.03
            else:
                pattern_delta = -0.01
            if pattern_delta != 0.0:
                for experience_id in pattern_linked_ids:
                    adjust_confidence(
                        experience_id=experience_id,
                        delta=pattern_delta,
                        revalidated=True,
                        new_context=pattern_delta > 0,
                    )
                selected_feedback_ids.update(pattern_linked_ids)
                if feedback_items:
                    adjusted = len(selected_feedback_ids)
                    summary = (context_effectiveness or {}).get("summary") or {}
                    self._log(
                        "Selective confidence feedback applied: "
                        f"adjusted={adjusted} high_value={summary.get('high_value_selected', 0)} "
                        f"missed={summary.get('missed_opportunities', 0)}"
                    )
                else:
                    self._log(f"Pattern-linked confidence adjusted for {len(pattern_linked_ids)} item(s).")
                return

        if feedback_items:
            summary = (context_effectiveness or {}).get("summary") or {}
            self._log(
                "Selective confidence feedback applied: "
                f"adjusted={adjusted} high_value={summary.get('high_value_selected', 0)} "
                f"missed={summary.get('missed_opportunities', 0)}"
            )
            return

        if outcome == "success":
            delta = 0.03
            new_context = True
        elif outcome == "partial":
            delta = 0.01
            new_context = True
        elif outcome == "failure":
            delta = -0.05
            new_context = False
        else:
            delta = -0.02
            new_context = False

        ids = {
            str(item.get("id"))
            for item in (
                self._ctx.retrieved_experiences
                + self._ctx.failure_patterns
                + self._ctx.conflict_cases
            )
            if item.get("id")
        }

        for experience_id in ids:
            adjust_confidence(
                experience_id=experience_id,
                delta=delta,
                revalidated=True,
                new_context=new_context,
            )

        if ids:
            self._log(f"Confidence adjusted for {len(ids)} retrieved item(s) with delta={delta:+.2f}")

    def _run_autonomous_improvement(self, skill_name: str) -> None:
        if not self.auto_improve or self._ctx is None:
            return

        try:
            result = run_autonomous_improvement(
                skill_name=skill_name,
                project_id=self._ctx.project_id,
                trigger="session_close",
                verbose=False,
            )
            if result.get("skipped"):
                self._log(f"Autonomous SISL skipped: {result.get('reason')}")
            else:
                cycle = result.get("cycle", {})
                self._log(
                    "Autonomous SISL completed: "
                    f"ckl={cycle.get('ckl_score', 0):.3f} "
                    f"applied={len(cycle.get('applied', []))}"
                )
        except Exception as exc:
            self._log(f"Autonomous SISL warning: {exc}")

    def _run_skill_factory_cycle(self) -> dict | None:
        """Ejecuta el skill factory cycle y retorna el resultado para logging.

        Returns:
            dict con keys: created, promoted, signals, skipped, reason
            None si esta deshabilitado o si auto_improve=False
        """
        if not self.auto_improve or self._ctx is None:
            return None

        mode = str(self.execution_policy.get("skill_factory_mode", "disabled") or "disabled")
        if mode == "disabled":
            return None

        try:
            result = run_skill_factory_cycle(
                project_id=self._ctx.project_id,
                auto_bootstrap=mode == "candidate_bootstrap",
                min_occurrences=int(self.execution_policy.get("skill_factory_min_gap_occurrences", 2) or 2),
                low_confidence_threshold=float(
                    self.execution_policy.get("skill_factory_low_confidence_threshold", 0.72) or 0.72
                ),
                max_candidates=int(
                    self.execution_policy.get("skill_factory_max_candidates_per_cycle", 1) or 1
                ),
                min_active_tests=int(self.execution_policy.get("skill_factory_min_active_tests", 8) or 8),
                min_successful_uses=int(
                    self.execution_policy.get("skill_factory_min_successful_uses", 2) or 2
                ),
                min_success_rate=float(
                    self.execution_policy.get("skill_factory_min_success_rate", 0.75) or 0.75
                ),
            )
            created = len(result.get("created", []))
            promoted = len(result.get("promoted", []))
            if created or promoted:
                self._log(f"Skill factory completed: created={created} promoted={promoted}")
            elif result.get("signals"):
                self._log(f"Skill factory found {len(result['signals'])} signal(s) but no skill was promoted.")
            return result
        except Exception as exc:
            self._log(f"Skill factory warning: {exc}")
            return None

    def _validate_task_brief(self) -> None:
        issues = []

        if self.execution_policy.get("require_task_brief", False):
            issues.extend(validate_task_brief(self.task_brief, self.intake_policy))

        if (
            self.intake_policy.get("require_user_confirmation", False)
            and self.execution_policy.get("block_on_unconfirmed_brief", False)
            and not self.task_brief.get("confirmed", False)
        ):
            issues.append("brief_unconfirmed")

        if issues and (
            self.execution_policy.get("strict_mode", False)
            or self.intake_policy.get("block_if_missing_required_fields", False)
        ):
            raise ValueError(
                "MCUM intake blocked. Task brief incompleto o invalido: "
                + ", ".join(str(issue) for issue in issues)
            )

    def _sync_skill_catalog(self) -> None:
        try:
            summary = sync_skill_catalog()
            self._log(f"Skill catalog synced: {summary.get('skills_synced', 0)} skill(s)")
        except Exception as exc:
            self._log(f"Skill catalog sync warning: {exc}")

    def _resolve_skill_status(self, skill_name: str) -> str:
        try:
            record = get_skill_record(skill_name)
        except Exception as exc:
            self._log(f"Skill catalog lookup warning: {exc}")
            return "unknown"
        if not record:
            return "unknown"
        return str(record.get("status") or "unknown")

    def _enforce_skill_status_policy(self, skill_name: str) -> str:
        status = self._resolve_skill_status(skill_name)
        if self.force_skill or status not in {"blocked", "deprecated"}:
            return status

        message = f"MCUM blocked skill '{skill_name}' because its lifecycle status is '{status}'."
        if self.execution_policy.get("block_on_policy_violation", False):
            raise RuntimeError(message)
        self._log(message)
        return status

    def _save_session_playbook(self, result: TaskResult, task_log_id: str) -> str | None:
        if self._ctx is None:
            return None
        if result.outcome not in {"success", "partial"}:
            return None

        payload = dict(result.playbook_data or {})
        commands = payload.get("commands") or []
        if isinstance(commands, str):
            commands = [commands]
        output_summary = payload.get("output_summary") or result.output_summary
        validation_summary = payload.get("validation_summary") or result.validation_summary
        files_touched = payload.get("files_touched") or [
            artifact["path"]
            for artifact in result.artifacts
            if artifact.get("exists")
        ]
        issues_avoided = payload.get("issues_avoided") or []
        if isinstance(issues_avoided, str):
            issues_avoided = [issues_avoided]
        active_pattern_ids = _pattern_ids_from_items(
            getattr(self._ctx, "active_patterns", None)
        )

        try:
            playbook_artifacts = list(result.artifacts or [])
            playbook_artifacts.extend(
                snapshot
                for snapshot in build_project_structure_snapshots(
                    self.project_path,
                    extra_paths=files_touched,
                )
                if snapshot not in playbook_artifacts
            )
            playbook_id = save_session_playbook(
                project_id=self._ctx.project_id,
                skill_name=result.skill_used,
                title=payload.get("title", result.task_description[:120]),
                task_description=result.task_description,
                objective=payload.get("objective") or self.task_brief.get("objective"),
                output_summary=output_summary,
                validation_summary=validation_summary,
                commands=commands,
                files_touched=files_touched,
                artifacts=playbook_artifacts,
                issues_avoided=issues_avoided,
                reusable_when=payload.get("reusable_when") or self.task_brief.get("success_criteria"),
                outcome=result.outcome,
                confidence_score=result.confidence_score,
                source_session_id=self.session_id,
                source_task_log_id=task_log_id,
                project_path=self.project_path,
                pattern_ids=active_pattern_ids,
            )
            self._log(f"Session playbook saved: {playbook_id}")
            return playbook_id
        except Exception as exc:
            self._log(f"Session playbook warning: {exc}")
            return None

    def _enforce_dispatch_policy(self, dispatch_result: DispatchResult) -> None:
        if self.force_skill:
            return

        min_confidence = float(self.execution_policy.get("min_dispatch_confidence", 0.0) or 0.0)
        if dispatch_result.confidence >= min_confidence:
            return

        message = (
            "MCUM strict mode blocked execution because dispatch confidence "
            f"{dispatch_result.confidence:.2f} is below {min_confidence:.2f}."
        )
        if self.execution_policy.get("block_on_policy_violation", False):
            raise RuntimeError(message)
        self._log(message)

    def _apply_anti_loop_controls(self, anti_loop_state: dict[str, Any]) -> dict[str, Any]:
        state = sanitize_loop_state(anti_loop_state)
        if not state.get("enabled"):
            return state

        policy = dict(self.execution_policy.get("anti_loop") or {})
        recommendation = str(state.get("recommendation") or "").strip()
        loop_risk = float(state.get("loop_risk") or 0.0)
        medium_threshold = float(policy.get("warning_risk_threshold", 0.35) or 0.35)

        actions_applied: list[str] = []
        warnings = list(state.get("warnings") or [])
        validation_escalated = False
        divergence_angles: list[str] = []

        if recommendation in {"switch_strategy_before_retry", "increase_validation_and_diverge"} and loop_risk >= medium_threshold:
            validation_note = (
                "Anti-loop: include explicit validation evidence and explain what changed versus prior failed attempts."
            )
            validation_required = str(self.task_brief.get("validation_required") or "").strip()
            if validation_note not in validation_required:
                self.task_brief["validation_required"] = (
                    f"{validation_required} | {validation_note}" if validation_required else validation_note
                )
            validation_escalated = True
            actions_applied.append("validation_escalated")

            constraints = _coerce_list(self.task_brief.get("constraints"))
            for constraint in [
                "Anti-loop: do not retry the same strategy without a material change.",
                "Anti-loop: document the changed path before marking success.",
            ]:
                if constraint not in constraints:
                    constraints.append(constraint)
            if constraints:
                self.task_brief["constraints"] = constraints
            actions_applied.append("constraints_injected")

            divergence_angles = [
                str(item).strip()
                for item in (policy.get("divergence_angles") or ["conservative", "critical", "alternate_skill"])
                if str(item).strip()
            ]
            if divergence_angles:
                self.task_brief["anti_loop_angles"] = divergence_angles
                actions_applied.append("divergence_requested")
                warnings.append(
                    "Anti-loop control active: rotate perspective before retrying "
                    f"({', '.join(divergence_angles[:3])})."
                )

        alternate_skills = [
            str(item).strip()
            for item in (state.get("alternate_success_skills") or state.get("success_escape_skills") or [])
            if str(item).strip()
        ]
        if alternate_skills:
            sources = _coerce_list(self.task_brief.get("sources_to_review"))
            hint = f"Anti-loop hint: inspect successful alternate skills first -> {', '.join(alternate_skills[:3])}"
            if hint not in sources:
                sources.append(hint)
                self.task_brief["sources_to_review"] = sources
            actions_applied.append("alternate_skill_hint")

        state["warnings"] = list(dict.fromkeys(warnings))
        state["validation_escalated"] = validation_escalated
        state["divergence_angles"] = divergence_angles
        state["actions_applied"] = actions_applied
        return sanitize_loop_state(state)

    def _apply_result_policy(self, result: TaskResult) -> TaskResult:
        if not self.execution_policy.get("strict_mode", False):
            return result

        outcome = result.outcome
        output_summary = result.output_summary or ""
        error_description = result.error_description
        execution_mode = self.task_brief.get("execution_mode", "ejecutar")
        anti_loop_state = sanitize_loop_state(getattr(self._ctx, "anti_loop", {})) if self._ctx else {}
        anti_loop_requires_explicit_validation = bool(anti_loop_state.get("validation_escalated"))
        validation_present = bool(
            result.validation_summary
            if anti_loop_requires_explicit_validation
            else result.validation_summary
            or (execution_mode in {"analizar", "proponer"} and output_summary)
        )
        artifacts_required = (
            self.execution_policy.get("require_artifacts_for_success", False)
            and execution_mode == "ejecutar"
        )

        violations: list[str] = []
        if outcome == "success":
            if self.execution_policy.get("require_validation_before_success", False) and not validation_present:
                violations.append(
                    "anti_loop_validation_required"
                    if anti_loop_requires_explicit_validation
                    else "missing_validation"
                )
            if artifacts_required and not result.artifacts:
                violations.append("missing_artifacts")

        if violations:
            note = f"Policy adjusted outcome due to: {', '.join(violations)}."
            output_summary = f"{output_summary} {note}".strip()
            if self.execution_policy.get("block_on_policy_violation", False):
                outcome = "partial"
            error_description = error_description or note

        return replace(
            result,
            outcome=outcome,
            output_summary=output_summary,
            error_description=error_description,
        )

    def _build_orchestrated_skills(self, result: TaskResult) -> list[str]:
        names: list[str] = []
        for skill_name in [self._ctx.skill_selected if self._ctx else None, result.skill_used, *result.skills_orchestrated]:
            cleaned = str(skill_name or "").strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
        return names

    def _build_skill_correction_metadata(
        self,
        result: TaskResult,
        orchestrated_skills: list[str],
    ) -> dict:
        selected_skill = self._ctx.skill_selected if self._ctx else ""
        final_skill = str(result.skill_used or "").strip()
        delegated_skills = [
            skill_name
            for skill_name in orchestrated_skills
            if skill_name not in {selected_skill, final_skill}
        ]
        implicit = bool(
            selected_skill
            and final_skill
            and selected_skill != final_skill
            and not self.force_skill
        )
        changed = bool(selected_skill and final_skill and selected_skill != final_skill)
        source = result.correction_source
        if not source:
            if implicit:
                source = "final_skill_override"
            elif delegated_skills:
                source = "delegated_execution"

        return {
            "selected_skill": selected_skill,
            "final_skill": final_skill,
            "delegated_skills": delegated_skills,
            "changed": changed,
            "implicit": implicit,
            "source": source,
        }

    def _get_orchestration_context(self) -> dict[str, Any]:
        context = getattr(self, "orchestration_context", None)
        if isinstance(context, dict):
            return context
        ctx = getattr(self, "_ctx", None)
        inherited = getattr(ctx, "orchestration", None)
        if isinstance(inherited, dict):
            return inherited
        return {
            "role": "coordinator",
            "allow_learning_writes": True,
            "suppress_autonomy_hooks": False,
        }

    def _allow_learning_writes(self) -> bool:
        return bool(self._get_orchestration_context().get("allow_learning_writes", True))

    def _should_skip_autonomy_hooks(self) -> bool:
        return bool(self._get_orchestration_context().get("suppress_autonomy_hooks", False))

    def _sync_code_graph(self, *, trigger: str) -> dict[str, Any]:
        configured_policy = self.execution_policy.get("code_graph")
        if not isinstance(configured_policy, dict) or not configured_policy:
            return {"status": "policy_unconfigured", "trigger": trigger}
        policy = dict(configured_policy)
        if not bool(policy.get("enabled", True)) or not bool(policy.get("auto_sync", False)):
            return {"status": "disabled", "trigger": trigger}
        role = str(self._get_orchestration_context().get("role") or "coordinator")
        if role != "coordinator" and bool(policy.get("coordinator_only_sync", True)):
            return {"status": "deferred_to_coordinator", "trigger": trigger}
        if not getattr(self, "_project", None):
            return {"status": "project_unavailable", "trigger": trigger}
        result = sync_project_code_graph(
            project_id=str(self._project["id"]),
            project_path=self.project_path,
            project_name=self.project_name,
            policy=policy,
            trigger=trigger,
        )
        if result.get("status") == "failure":
            self._log(f"Code graph sync warning ({trigger}): {result.get('error')}")
        return result

    def _sync_graph_intelligence(
        self,
        *,
        trigger: str,
        code_graph_sync: dict[str, Any] | None = None,
        selected_skill: str | None = None,
    ) -> dict[str, Any]:
        policy = dict(self.execution_policy.get("graph_intelligence") or {})
        if not bool(policy.get("enabled", True)):
            return {"status": "disabled", "trigger": trigger}
        role = str(self._get_orchestration_context().get("role") or "coordinator")
        if role != "coordinator" and bool(policy.get("coordinator_only_sync", True)):
            return {"status": "deferred_to_coordinator", "trigger": trigger}
        if not getattr(self, "_project", None):
            return {"status": "project_unavailable", "trigger": trigger}
        result = sync_unified_project_graph(
            project_id=str(self._project["id"]),
            trigger=trigger,
            selected_skill=selected_skill,
            code_graph_sync=code_graph_sync,
            metadata={
                "session_id": self.session_id,
                "project_path": self.project_path,
                "orchestration_role": role,
            },
        )
        if result.get("status") == "failure":
            self._log(f"Graph intelligence sync warning ({trigger}): {result.get('error')}")
        return result

    def _finalize_task_graph(self, *, selected_skill: str | None = None) -> dict[str, Any]:
        close_sync = dict(getattr(self, "_close_code_graph_sync", {}) or {})
        code_graph = (
            {**close_sync, "reused_for_trigger": "task_end"}
            if close_sync
            else self._sync_code_graph(trigger="task_end")
        )
        projection_source = code_graph
        intelligence = self._sync_graph_intelligence(
            trigger="task_end",
            code_graph_sync=projection_source,
            selected_skill=selected_skill,
        )
        return {
            "code_graph": code_graph,
            "projection_source": projection_source,
            "graph_intelligence": intelligence,
        }

    def begin(self) -> TaskContext:
        self._validate_task_brief()
        self._start_ts = time.time()
        self._sync_skill_catalog()
        self._project = get_or_create_project(
            project_path=self.project_path,
            project_name=self.project_name,
            tech_stack=self.tech_stack,
        )
        project_id = self._project["id"]
        project_name = self._project["project_name"]
        code_graph_sync_begin = self._sync_code_graph(trigger="session_begin")
        anti_loop_problem = analyze_problem_loop(
            project_id=project_id,
            task_description=self.task_description,
            task_brief=self.task_brief,
            policy=self.execution_policy.get("anti_loop") or {},
        )
        dispatch_hints = _build_dispatch_hints_from_anti_loop(
            self.task_brief,
            anti_loop_problem,
            self.execution_policy.get("anti_loop") or {},
        )

        dispatch_learning_profile: dict[str, Any] | None = None
        try:
            dispatch_learning_profile = get_dispatch_performance_profile(
                project_id=project_id,
                task_type=str(self.task_brief.get("task_type") or ""),
                execution_mode=str(self.task_brief.get("execution_mode") or ""),
                limit=80,
                min_samples=3,
                allow_cross_project=bool(self.execution_policy.get("allow_cross_project_fallback", True)),
            )
        except Exception as exc:
            self._log(f"Dispatch learning warning: {exc}")

        auto_dispatch_result = None
        if self.force_skill:
            auto_dispatch_result = dispatch(
                task_description=self.task_description,
                project_context=self._project,
                force_skill=None,
                dispatch_learning_profile=dispatch_learning_profile,
                dispatch_hints=dispatch_hints,
            )

        dispatch_result = dispatch(
            task_description=self.task_description,
            project_context=self._project,
            force_skill=self.force_skill,
            dispatch_learning_profile=dispatch_learning_profile,
            dispatch_hints=dispatch_hints,
        )
        self._enforce_dispatch_policy(dispatch_result)
        skill_status = self._enforce_skill_status_policy(dispatch_result.skill_name)
        retrieval_scope_profile: dict[str, Any] | None = None
        try:
            retrieval_scope_profile = get_retrieval_scope_profile(
                project_id=project_id,
                skill_name=dispatch_result.skill_name,
                task_type=str(self.task_brief.get("task_type") or ""),
                execution_mode=str(self.task_brief.get("execution_mode") or ""),
                limit=40,
                min_samples=3,
                allow_cross_project=bool(self.execution_policy.get("allow_cross_project_fallback", True)),
            )
        except Exception as exc:
            self._log(f"Retrieval scope learning warning: {exc}")
        retrieval_start = time.perf_counter()
        retrieval_result = retrieve_for_task(
            self.task_description,
            skill_context=dispatch_result.skill_name,
            project_id=project_id,
            policy=self.execution_policy,
            scope_learning_profile=retrieval_scope_profile,
        )
        active_pattern_ids = _pattern_ids_from_items(retrieval_result.get("active_patterns", []))
        playbook_result = retrieve_session_playbooks(
            self.task_description,
            skill_name=dispatch_result.skill_name,
            project_id=project_id,
            limit=int(self.execution_policy.get("max_playbooks", 3) or 3),
            min_similarity=float(self.execution_policy.get("min_playbook_similarity", 0.28) or 0.28),
            allow_cross_project=bool(self.execution_policy.get("allow_cross_project_fallback", True)),
            policy=self.execution_policy,
            active_pattern_ids=active_pattern_ids,
        )
        try:
            knowledge_library_result = retrieve_knowledge_library_shadow(
                self.task_description,
                task_brief=self.task_brief,
            )
        except Exception as exc:
            self._log(f"Knowledge library shadow warning: {exc}")
            knowledge_library_result = {
                "enabled": False,
                "shadow_mode": False,
                "applied_mode": "disabled",
                "hits": [],
                "warnings": ["knowledge_library:shadow_error"],
                "tokens_used_estimate": 0,
                "metadata": {"error": str(exc)},
            }
        retrieval_latency_ms = int((time.perf_counter() - retrieval_start) * 1000)
        knowledge_library_hits = list(knowledge_library_result.get("hits") or [])
        knowledge_library_payload = {
            "enabled": bool(knowledge_library_result.get("enabled")),
            "shadow_mode": bool(knowledge_library_result.get("shadow_mode")),
            "mode": str(knowledge_library_result.get("applied_mode") or "disabled"),
            "hits_retrieved": len(knowledge_library_hits),
            "tokens_used_estimate": int(knowledge_library_result.get("tokens_used_estimate") or 0),
            "warnings": list(knowledge_library_result.get("warnings") or []),
            "metadata": dict(knowledge_library_result.get("metadata") or {}),
            "route_plan": dict((knowledge_library_result.get("metadata") or {}).get("route_plan") or {}),
            "integration_mode": str((knowledge_library_result.get("metadata") or {}).get("integration_mode") or ""),
        }
        code_graph_policy = dict((self.execution_policy.get("code_graph") or {}))
        code_graph_hits: list[dict[str, Any]] = []
        code_graph_payload: dict[str, Any] = {
            "enabled": bool(code_graph_policy.get("enabled", True)),
            "hits_retrieved": 0,
            "tokens_used_estimate": 0,
            "metadata": {"sync": code_graph_sync_begin},
            "warnings": [],
        }
        if bool(code_graph_policy.get("enabled", True)):
            code_graph_query = " ".join(
                str(value or "")
                for value in (
                    self.task_description,
                    self.task_brief.get("objective"),
                    self.task_brief.get("expected_deliverable"),
                    " ".join(str(item) for item in self.task_brief.get("sources_to_review") or []),
                )
            ).strip()
            try:
                explicit_filters = dict(self.task_brief.get("code_graph_filters") or {})
                inferred_filters = (
                    infer_code_graph_filters(code_graph_query)
                    if bool(code_graph_policy.get("auto_filters", True)) and not explicit_filters
                    else {}
                )
                graph_filters = explicit_filters or inferred_filters
                code_graph_result = retrieve_code_graph_context(
                    project_id=project_id,
                    query=code_graph_query,
                    limit=int(code_graph_policy.get("max_hits") or 8),
                    depth=int(code_graph_policy.get("depth") or 1),
                    languages=list(graph_filters.get("languages") or []),
                    exclude_languages=list(graph_filters.get("exclude_languages") or []),
                    path_prefix=graph_filters.get("path_prefix"),
                    node_kinds=list(graph_filters.get("node_kinds") or []),
                )
                code_graph_hits = list(code_graph_result.get("hits") or [])
                linked_experiences = list(code_graph_result.get("linked_experiences") or [])
                existing_experience_ids = {
                    str(item.get("id"))
                    for item in retrieval_result.get("experiences", [])
                    if item.get("id")
                }
                for experience in linked_experiences:
                    if str(experience.get("id") or "") in existing_experience_ids:
                        continue
                    experience["_retrieval_source"] = "code_graph_link"
                    retrieval_result.setdefault("experiences", []).append(experience)
                    existing_experience_ids.add(str(experience.get("id") or ""))
                retrieval_result["total_retrieved"] = sum(
                    len(retrieval_result.get(key, []) or [])
                    for key in ("experiences", "failure_patterns", "conflict_cases", "active_patterns")
                )
                code_graph_payload = {
                    "enabled": bool(code_graph_result.get("enabled", True)),
                    "hits_retrieved": len(code_graph_hits),
                    "linked_experiences_retrieved": len(linked_experiences),
                    "tokens_used_estimate": int(code_graph_result.get("tokens_used_estimate") or 0),
                    "metadata": {
                        **dict(code_graph_result.get("metadata") or {}),
                        "sync": code_graph_sync_begin,
                    },
                    "warnings": list(code_graph_result.get("warnings") or []),
                }
            except Exception as exc:
                self._log(f"Code graph warning: {exc}")
                code_graph_payload = {
                    "enabled": True,
                    "hits_retrieved": 0,
                    "tokens_used_estimate": 0,
                    "metadata": {"error": str(exc), "sync": code_graph_sync_begin},
                    "warnings": ["code_graph:retrieval_error"],
                }
        graph_intelligence_begin = self._sync_graph_intelligence(
            trigger="session_begin_context",
            code_graph_sync=code_graph_sync_begin,
            selected_skill=dispatch_result.skill_name,
        )
        warnings = list(dispatch_result.warnings)
        warnings.extend(retrieval_result.get("warnings", []))
        warnings.extend(playbook_result.get("warnings", []))
        warnings.extend(code_graph_payload.get("warnings", []))
        if retrieval_result["total_retrieved"] == 0:
            warnings.insert(0, "Cold start: no previous experience was retrieved.")
        anti_loop_state = enrich_loop_state_with_strategy(
            loop_state=anti_loop_problem,
            skill_name=dispatch_result.skill_name,
            dispatch_method=dispatch_result.match_method,
            retrieval_mode=retrieval_result.get("retrieval_mode", "unknown"),
            execution_mode=self.task_brief.get("execution_mode"),
            playbook_scope=playbook_result.get("search_scope", "none"),
            orchestration=self._get_orchestration_context(),
            policy=self.execution_policy.get("anti_loop") or {},
        )
        anti_loop_state = self._apply_anti_loop_controls(anti_loop_state)
        warnings.extend(anti_loop_state.get("warnings", []))
        anti_loop_metadata = sanitize_loop_state(anti_loop_state)

        context_learning_profile: dict[str, Any] | None = None
        try:
            state_compiler_policy = self.execution_policy.get("state_compiler") or {}
            context_learning_profile = get_context_effectiveness_profile(
                project_id=project_id,
                skill_name=dispatch_result.skill_name,
                task_type=str(self.task_brief.get("task_type") or ""),
                execution_mode=str(self.task_brief.get("execution_mode") or ""),
                limit=int(state_compiler_policy.get("effectiveness_history_limit", 60) or 60),
                min_samples=int(state_compiler_policy.get("min_effectiveness_samples", 3) or 3),
                allow_cross_project=bool(self.execution_policy.get("allow_cross_project_fallback", True)),
            )
        except Exception as exc:
            self._log(f"Context learning profile warning: {exc}")

        auto_dispatch_payload = self._dispatch_result_payload(auto_dispatch_result)
        state_compiler_enabled = bool(
            (self.execution_policy.get("state_compiler") or {}).get("enabled", True)
        )
        compiled_state = (
            compile_state(
                session_id=self.session_id,
                project_name=project_name,
                project_id=project_id,
                project_scope=retrieval_result.get("project_scope", "unknown"),
                task_description=self.task_description,
                task_brief=self.task_brief,
                skill_selected=dispatch_result.skill_name,
                skill_status=skill_status,
                dispatch_confidence=dispatch_result.confidence,
                dispatch_method=dispatch_result.match_method,
                auto_dispatch_result=auto_dispatch_payload,
                retrieval_mode=retrieval_result.get("retrieval_mode", "unknown"),
                retrieval_latency_ms=retrieval_latency_ms,
                experiences=retrieval_result.get("experiences", []),
                code_graph_hits=code_graph_hits,
                knowledge_library_hits=knowledge_library_hits,
                knowledge_library_mode=knowledge_library_payload["mode"],
                knowledge_library_metadata=knowledge_library_payload,
                active_patterns=retrieval_result.get("active_patterns", []),
                failure_patterns=retrieval_result.get("failure_patterns", []),
                conflict_cases=retrieval_result.get("conflict_cases", []),
                playbooks=playbook_result.get("playbooks", []),
                warnings=warnings,
                execution_policy=self.execution_policy,
                effectiveness_profile=context_learning_profile,
            )
            if state_compiler_enabled
            else None
        )
        project_context_envelope = build_project_context_envelope(
            session_id=self.session_id,
            project_id=str(project_id),
            project_name=str(project_name),
            project_path=self.project_path,
            task_description=self.task_description,
            task_brief=self.task_brief,
            selected_skill=dispatch_result.skill_name,
            skill_status=skill_status,
            code_graph_hits=code_graph_hits,
            experiences=retrieval_result.get("experiences", []),
            active_patterns=retrieval_result.get("active_patterns", []),
            failure_patterns=retrieval_result.get("failure_patterns", []),
            conflict_cases=retrieval_result.get("conflict_cases", []),
            knowledge_library_hits=knowledge_library_hits,
            graph_intelligence=graph_intelligence_begin,
            execution_policy=self.execution_policy,
        )

        session_info = log_session_start(
            project_path=self.project_path,
            skill_used=dispatch_result.skill_name,
            task_description=self.task_description,
            extra_metadata={
                "session_id": self.session_id,
                "dispatch_method": dispatch_result.match_method,
                "triggered_by": dispatch_result.triggered_by,
                "skill_version": __version__,
                "task_brief": self.task_brief,
                "skill_status": skill_status,
                "auto_dispatch": auto_dispatch_payload,
                "dispatch_learning": dispatch_learning_profile,
                "dispatch_hints": dispatch_hints,
                "retrieval_scope_learning": retrieval_result.get("scope_learning_profile"),
                "active_patterns_retrieved": len(retrieval_result.get("active_patterns", [])),
                "pattern_ids_used": active_pattern_ids,
                "feedback_signals": retrieval_result.get("feedback_signals", {}),
                "memory_governance": retrieval_result.get("memory_governance", {}),
                "playbook_memory_governance": playbook_result.get("memory_governance", {}),
                "code_graph": code_graph_payload,
                "graph_intelligence": graph_intelligence_begin,
                "project_context_envelope": {
                    "envelope_hash": project_context_envelope.get("envelope_hash"),
                    "context_pack_id": project_context_envelope.get("context_pack_id"),
                    "token_estimate": project_context_envelope.get("token_estimate"),
                },
                "knowledge_library": knowledge_library_payload,
                "anti_loop": anti_loop_metadata,
                "orchestration": self._get_orchestration_context(),
                "compiled_context": compiled_state.to_metadata() if compiled_state else None,
            },
        )
        log_id = session_info["log_id"]

        retrieval_run_id = record_retrieval_run(
            session_id=self.session_id,
            project_id=project_id,
            skill_name=dispatch_result.skill_name,
            input_context=self.task_description,
            retrieval_result=retrieval_result,
            decision_taken=f"selected_skill={dispatch_result.skill_name}",
            final_confidence=dispatch_result.confidence,
        )

        log_entry(
            project_id=project_id,
            log_type="decision",
            title=f"Skill selected: {dispatch_result.skill_name}",
            description=f"Method={dispatch_result.match_method}; trigger={dispatch_result.triggered_by}",
            skill_used="mcum-orchestrator",
            skills_orchestrated=[dispatch_result.skill_name],
            confidence_score=dispatch_result.confidence,
            pattern_ids_used=active_pattern_ids,
            retrieval_run_id=retrieval_run_id,
            log_metadata={
                "session_id": self.session_id,
                "alternatives": dispatch_result.alternatives[:3],
                "skill_status": skill_status,
                "playbooks_retrieved": len(playbook_result.get("playbooks", [])),
                "playbook_scope": playbook_result.get("search_scope", "none"),
                "auto_dispatch": auto_dispatch_payload,
                "dispatch_learning": dispatch_learning_profile,
                "dispatch_hints": dispatch_hints,
                "retrieval_scope_learning": retrieval_result.get("scope_learning_profile"),
                "pattern_ids_used": active_pattern_ids,
                "memory_governance": retrieval_result.get("memory_governance", {}),
                "playbook_memory_governance": playbook_result.get("memory_governance", {}),
                "code_graph": code_graph_payload,
                "graph_intelligence": graph_intelligence_begin,
                "project_context_envelope": {
                    "envelope_hash": project_context_envelope.get("envelope_hash"),
                    "context_pack_id": project_context_envelope.get("context_pack_id"),
                    "token_estimate": project_context_envelope.get("token_estimate"),
                },
                "knowledge_library": knowledge_library_payload,
                "anti_loop": anti_loop_metadata,
                "orchestration": self._get_orchestration_context(),
                "compiled_context": compiled_state.to_metadata() if compiled_state else None,
            },
            retrieval_latency_ms=retrieval_latency_ms,
        )

        self._ctx = TaskContext(
            session_id=self.session_id,
            project_id=project_id,
            project_name=project_name,
            task_description=self.task_description,
            skill_selected=dispatch_result.skill_name,
            dispatch_result=dispatch_result,
            auto_dispatch_result=auto_dispatch_result,
            retrieved_experiences=retrieval_result.get("experiences", []),
            failure_patterns=retrieval_result.get("failure_patterns", []),
            conflict_cases=retrieval_result.get("conflict_cases", []),
            active_patterns=retrieval_result.get("active_patterns", []),
            code_graph_hits=code_graph_hits,
            code_graph_metadata=code_graph_payload,
            knowledge_library_hits=knowledge_library_hits,
            knowledge_library_mode=knowledge_library_payload["mode"],
            knowledge_library_metadata=knowledge_library_payload,
            feedback_signals=retrieval_result.get("feedback_signals", {}),
            retrieval_mode=retrieval_result.get("retrieval_mode", "unknown"),
            session_start_ts=self._start_ts,
            log_id=log_id,
            retrieval_run_id=retrieval_run_id,
            warnings=warnings,
            task_brief=self.task_brief,
            project_scope=retrieval_result.get("project_scope", "unknown"),
            playbooks=playbook_result.get("playbooks", []),
            playbook_scope=playbook_result.get("search_scope", "none"),
            memory_governance=retrieval_result.get("memory_governance", {}),
            playbook_memory_governance=playbook_result.get("memory_governance", {}),
            anti_loop=anti_loop_metadata,
            retrieval_latency_ms=retrieval_latency_ms,
            skill_status=skill_status,
            compiled_state=compiled_state,
            retrieval_scope_learning=retrieval_result.get("scope_learning_profile"),
            dispatch_learning_profile=dispatch_learning_profile,
            dispatch_hints=dispatch_hints,
            orchestration=self._get_orchestration_context(),
            project_context_envelope=project_context_envelope,
            graph_intelligence=graph_intelligence_begin,
        )

        self._log(f"Project: {project_name}")
        self._log(
            f"Skill: {dispatch_result.skill_name} via {dispatch_result.match_method} "
            f"| retrieved={retrieval_result['total_retrieved']}"
        )
        if compiled_state is not None:
            self._log(
                "State compiler: "
                f"tokens={compiled_state.estimated_tokens}/{compiled_state.token_budget} "
                f"| selected={compiled_state.selected_counts}"
            )
        return self._ctx

    def close(self, result: TaskResult) -> dict[str, Any]:
        finalization: dict[str, Any] = {}
        try:
            payload = self._close_impl(result)
        finally:
            selected_skill = result.skill_used or (
                self._ctx.skill_selected if self._ctx is not None else None
            )
            try:
                finalization = self._finalize_task_graph(selected_skill=selected_skill)
            except Exception as exc:
                finalization = {"status": "failure", "error": str(exc)}
                self._log(f"Final task graph sync warning: {exc}")
        payload["graph_finalization"] = finalization
        return payload

    def _close_impl(self, result: TaskResult) -> dict[str, Any]:
        """
        Cierra la sesion y retorna un dict con:
          - log_id: str — ID del log en PostgreSQL
          - record_status: "recorded" | "record_failed" — si el registro MCUM succeedio
          - outcome: str — outcome de la tarea
          - session_id: str — ID de la sesion
        """
        if self._ctx is None or self._start_ts is None:
            raise RuntimeError("Call begin() before close().")
        if self._closed:
            raise RuntimeError("Session is already closed.")

        result = self._apply_result_policy(result)
        duration_sec = int(time.time() - self._start_ts)
        wall_clock_ms = int((time.time() - self._start_ts) * 1000)
        experience_id: str | None = None
        learning_writeback_deferred = False
        orchestrated_skills = self._build_orchestrated_skills(result)
        skill_correction = self._build_skill_correction_metadata(result, orchestrated_skills)
        allow_learning_writes = self._allow_learning_writes()
        code_graph_sync_close = self._sync_code_graph(trigger="session_close")
        self._close_code_graph_sync = code_graph_sync_close
        experience_code_links: dict[str, Any] = {"status": "not_applicable", "linked": 0}

        if result.experience_data and result.outcome in {"success", "partial"} and allow_learning_writes:
            data = result.experience_data
            experience_source_paths = list((result.playbook_data or {}).get("files_touched") or [])
            if not experience_source_paths:
                experience_source_paths = [
                    artifact["path"]
                    for artifact in result.artifacts
                    if artifact.get("path")
                ]
            source_artifacts = build_source_snapshots(
                experience_source_paths,
                project_path=self.project_path,
            )
            source_artifacts.extend(
                snapshot
                for snapshot in build_project_structure_snapshots(
                    self.project_path,
                    extra_paths=experience_source_paths,
                )
                if snapshot not in source_artifacts
            )
            experience_id = save_experience(
                category=data.get("category", "implementation_recipe"),
                title=data.get("title", result.task_description[:120]),
                content=data.get(
                    "content",
                    {"conclusion": result.output_summary or "Task completed"},
                ),
                skill_name=result.skill_used,
                project_id=self._ctx.project_id,
                task_description=result.task_description,
                applicability=data.get("applicability"),
                not_applicable_cases=data.get("not_applicable_cases"),
                conditions=data.get("conditions"),
                evidence_refs=data.get("evidence_refs"),
                source_artifacts=source_artifacts,
                review_notes=data.get("review_notes"),
                initial_score=result.confidence_score,
                skill_version=__version__,
                is_synthetic=bool(data.get("is_synthetic", False)),
            )
            self._log(f"Experience saved: {experience_id}")
            result_paths = self._result_paths(result)
            if code_graph_sync_close.get("status") in {"success", "no_changes"}:
                experience_code_links = link_experience_to_code_graph(
                    experience_id=experience_id,
                    project_id=self._ctx.project_id,
                    paths=result_paths,
                    evidence_refs=list(data.get("evidence_refs") or []),
                    link_kind="modified" if result_paths else "validated",
                    confidence=result.confidence_score,
                    ensure_schema=False,
                )
            else:
                experience_code_links = {
                    "status": "skipped",
                    "reason": f"code_graph_sync:{code_graph_sync_close.get('status') or 'unknown'}",
                    "linked": 0,
                }
            self._log(f"Experience code links: {experience_code_links.get('linked', 0)}")
        elif result.experience_data and result.outcome in {"success", "partial"}:
            learning_writeback_deferred = True
            self._log("Worker session deferred experience writeback to coordinator.")

        context_tokens_in = (
            self._ctx.compiled_state.estimated_tokens
            if self._ctx.compiled_state is not None
            else estimate_tokens(self._ctx.to_context_block())
        )
        context_tokens_out = result.context_tokens_out
        if context_tokens_out is None:
            context_tokens_out = estimate_tokens(
                {
                    "output_summary": result.output_summary,
                    "validation_summary": result.validation_summary,
                    "error_description": result.error_description,
                }
        )
        context_effectiveness = self._evaluate_context_effectiveness(result)
        active_pattern_ids = _pattern_ids_from_items(self._ctx.active_patterns)
        anti_loop_metadata = finalize_loop_state(
            project_id=self._ctx.project_id,
            loop_state=getattr(self._ctx, "anti_loop", {}),
            result_outcome=result.outcome,
            result_error_description=result.error_description,
            result_validation_summary=result.validation_summary,
            result_metadata=dict(result.extra_metadata or {}),
            policy=self.execution_policy.get("anti_loop") or {},
        )

        log_id = log_entry(
            project_id=self._ctx.project_id,
            log_type="task",
            title=result.task_description[:200],
            description=result.output_summary,
            skill_used=result.skill_used,
            skills_orchestrated=orchestrated_skills,
            outcome=result.outcome,
            outcome_details=result.error_description,
            artifacts_generated=result.artifacts,
            experience_ids=[experience_id] if experience_id else [],
            pattern_ids_used=active_pattern_ids,
            retrieval_run_id=self._ctx.retrieval_run_id,
            session_duration_sec=duration_sec,
            confidence_score=result.confidence_score,
            context_tokens_in=context_tokens_in,
            context_tokens_out=context_tokens_out,
            task_wall_clock_ms=wall_clock_ms,
            retrieval_latency_ms=self._ctx.retrieval_latency_ms,
            log_metadata={
                "session_id": self.session_id,
                "task_description": result.task_description,
                "dispatch_method": self._ctx.dispatch_result.match_method,
                "retrieval_mode": self._ctx.retrieval_mode,
                "skill_version": __version__,
                "task_brief": self.task_brief,
                "project_scope": self._ctx.project_scope,
                "playbooks_retrieved": len(self._ctx.playbooks),
                "playbook_scope": self._ctx.playbook_scope,
                "skill_status": self._ctx.skill_status,
                "selected_skill": self._ctx.skill_selected,
                "dispatch_learning": self._ctx.dispatch_learning_profile,
                "dispatch_hints": getattr(self._ctx, "dispatch_hints", None),
                "retrieval_scope_learning": self._ctx.retrieval_scope_learning,
                "active_patterns_retrieved": len(self._ctx.active_patterns),
                "pattern_ids_used": active_pattern_ids,
                "feedback_signals": self._ctx.feedback_signals,
                "memory_governance": getattr(self._ctx, "memory_governance", {}),
                "playbook_memory_governance": getattr(self._ctx, "playbook_memory_governance", {}),
                "code_graph": getattr(self._ctx, "code_graph_metadata", {}),
                "code_graph_sync_close": code_graph_sync_close,
                "experience_code_links": experience_code_links,
                "knowledge_library": getattr(self._ctx, "knowledge_library_metadata", {}),
                "anti_loop": anti_loop_metadata,
                "final_skill": result.skill_used,
                "delegated_skills": skill_correction["delegated_skills"],
                "skill_correction": skill_correction,
                "orchestration": self._get_orchestration_context(),
                "learning_writeback_deferred": learning_writeback_deferred,
                "task_result_metadata": dict(result.extra_metadata or {}),
                "auto_dispatch": self._dispatch_result_payload(self._ctx.auto_dispatch_result),
                "validation_summary": result.validation_summary,
                "context_effectiveness": context_effectiveness,
                "compiled_context": (
                    self._ctx.compiled_state.to_metadata()
                    if self._ctx.compiled_state is not None
                    else None
                ),
            },
        )
        pattern_usage = {"status": "not_applicable", "events_recorded": 0}
        if active_pattern_ids and allow_learning_writes:
            try:
                pattern_lifecycle = dict((load_pattern_policy().get("lifecycle") or {}))
                pattern_usage = record_pattern_usage_events(
                    pattern_ids=active_pattern_ids,
                    project_id=self._ctx.project_id,
                    session_id=self.session_id,
                    log_id=log_id,
                    outcome=result.outcome,
                    user_feedback=result.user_feedback,
                    metadata={"task_description": result.task_description},
                    min_usage_before_health_decision=int(
                        pattern_lifecycle.get("min_usage_before_health_decision", 5) or 5
                    ),
                    degraded_success_rate=float(
                        pattern_lifecycle.get("degraded_success_rate", 0.50) or 0.50
                    ),
                )
            except Exception as exc:
                pattern_usage = {"status": "failure", "error": str(exc), "events_recorded": 0}
                self._log(f"Pattern usage tracking warning: {exc}")
        playbook_id = self._save_session_playbook(result, log_id) if allow_learning_writes else None
        if result.playbook_data and not allow_learning_writes:
            learning_writeback_deferred = True
            self._log("Worker session deferred playbook writeback to coordinator.")

        if self._ctx.retrieval_run_id:
            finalize_retrieval_run(
                retrieval_run_id=self._ctx.retrieval_run_id,
                outcome_status=result.outcome,
                outcome_description=result.output_summary or result.error_description,
                final_confidence=result.confidence_score,
                failure_reason=result.error_description if result.outcome == "failure" else None,
                user_feedback=result.user_feedback,
            )

        if allow_learning_writes:
            self._reinforce_retrieval_confidence(
                result.outcome,
                context_effectiveness=context_effectiveness,
            )

        if self._should_skip_autonomy_hooks():
            self._log("Skipping autonomy hooks for worker-supervised session.")
            skill_factory_result = None
        else:
            self._run_autonomous_improvement(result.skill_used)
            skill_factory_result = self._run_skill_factory_cycle()

        log_session_end(
            project_id=self._ctx.project_id,
            session_duration_sec=duration_sec,
            tasks_completed=1,
            skill_used="mcum-orchestrator",
            outcome=result.outcome,
            context_tokens_in=context_tokens_in,
            context_tokens_out=context_tokens_out,
            task_wall_clock_ms=wall_clock_ms,
            retrieval_latency_ms=self._ctx.retrieval_latency_ms,
            pattern_ids_used=active_pattern_ids,
            extra_metadata={
                "session_id": self.session_id,
                "retrieval_run_id": self._ctx.retrieval_run_id,
                "task_log_id": log_id,
                "playbook_id": playbook_id,
                "selected_skill": self._ctx.skill_selected,
                "final_skill": result.skill_used,
                "skills_orchestrated": orchestrated_skills,
                "skill_correction": skill_correction,
                "dispatch_learning": self._ctx.dispatch_learning_profile,
                "dispatch_hints": getattr(self._ctx, "dispatch_hints", None),
                "retrieval_scope_learning": self._ctx.retrieval_scope_learning,
                "active_patterns_retrieved": len(self._ctx.active_patterns),
                "pattern_ids_used": active_pattern_ids,
                "pattern_usage": pattern_usage,
                "feedback_signals": self._ctx.feedback_signals,
                "memory_governance": getattr(self._ctx, "memory_governance", {}),
                "playbook_memory_governance": getattr(self._ctx, "playbook_memory_governance", {}),
                "code_graph": getattr(self._ctx, "code_graph_metadata", {}),
                "code_graph_sync_close": code_graph_sync_close,
                "experience_code_links": experience_code_links,
                "knowledge_library": getattr(self._ctx, "knowledge_library_metadata", {}),
                "anti_loop": anti_loop_metadata,
                "context_effectiveness": context_effectiveness,
                "orchestration": self._get_orchestration_context(),
                "learning_writeback_deferred": learning_writeback_deferred,
                "task_result_metadata": dict(result.extra_metadata or {}),
                "compiled_context": (
                    self._ctx.compiled_state.to_metadata()
                    if self._ctx.compiled_state is not None
                    else None
                ),
                "skill_factory_ran": skill_factory_result is not None,
                "skill_factory_created": len(skill_factory_result.get("created", [])) if skill_factory_result else 0,
                "skill_factory_promoted": len(skill_factory_result.get("promoted", [])) if skill_factory_result else 0,
                "skill_factory_signals_n": len(skill_factory_result.get("signals", [])) if skill_factory_result else 0,
                "html_artifacts_pending_qa": [
                    a.get("path") for a in result.artifacts
                    if isinstance(a, dict) and str(a.get("path") or "").lower().endswith(".html")
                ] if result.outcome == "success" else [],
            },
        )

        for skill_name in orchestrated_skills:
            try:
                mark_skill_used(skill_name)
            except Exception as exc:
                self._log(f"Skill usage mark warning for {skill_name}: {exc}")

        self._closed = True

        self._log(f"Session closed with outcome={result.outcome}")
        record_status = "recorded" if log_id else "record_failed"
        return {
            "log_id": log_id,
            "record_status": record_status,
            "outcome": result.outcome,
            "session_id": self.session_id,
        }

    def abort(
        self,
        error_description: str,
        output_summary: str | None = None,
        validation_summary: str | None = None,
        confidence_score: float = 0.0,
    ) -> dict[str, Any]:
        if self._ctx is None:
            raise RuntimeError("Call begin() before abort().")

        summary = output_summary or "Session aborted before successful completion."
        return self.close(
            TaskResult(
                task_description=self.task_description,
                skill_used=self._ctx.skill_selected,
                outcome="failure",
                confidence_score=confidence_score,
                output_summary=summary,
                error_description=error_description,
                validation_summary=validation_summary,
            )
        )

    @property
    def context(self) -> TaskContext | None:
        return self._ctx


def quick_dispatch(task_description: str, project_path: str | None = None) -> dict:
    project_id = None
    project = None
    dispatch_learning_profile = None
    dispatch_hints = None
    if project_path:
        try:
            sync_skill_catalog()
        except Exception:
            pass
        project = get_or_create_project(project_path=project_path)
        project_id = project.get("id")
        try:
            dispatch_learning_profile = get_dispatch_performance_profile(project_id=project_id)
        except Exception:
            dispatch_learning_profile = None
        try:
            anti_loop_problem = analyze_problem_loop(
                project_id=project_id,
                task_description=task_description,
                task_brief={"project_path": project_path},
                policy=load_execution_policy().get("anti_loop") or {},
            )
            dispatch_hints = _build_dispatch_hints_from_anti_loop(
                {"project_path": project_path},
                anti_loop_problem,
                load_execution_policy().get("anti_loop") or {},
            )
        except Exception:
            dispatch_hints = None
    dispatch_result = dispatch(
        task_description,
        project_context=project,
        dispatch_learning_profile=dispatch_learning_profile,
        dispatch_hints=dispatch_hints,
    )
    retrieval_result = retrieve_for_task(
        task_description,
        skill_context=dispatch_result.skill_name,
        project_id=project_id,
    )
    return {
        "skill": dispatch_result.skill_name,
        "confidence": dispatch_result.confidence,
        "method": dispatch_result.match_method,
        "triggered_by": dispatch_result.triggered_by,
        "alternatives": dispatch_result.alternatives[:2],
        "experiences_n": len(retrieval_result.get("experiences", [])),
        "failure_patterns_n": len(retrieval_result.get("failure_patterns", [])),
        "conflicts_n": len(retrieval_result.get("conflict_cases", [])),
        "warnings": dispatch_result.warnings,
        "retrieval_mode": retrieval_result.get("retrieval_mode", "unknown"),
        "tokens_used_estimate": retrieval_result.get("tokens_used_estimate", 0),
        "dispatch_learning_active": bool(
            isinstance(dispatch_learning_profile, dict) and dispatch_learning_profile.get("active")
        ),
        "dispatch_hints_active": bool(dispatch_hints),
    }


if __name__ == "__main__":
    session = OrchestratorSession(
        project_path="C:/Users/dev/workspace",
        task_description="Configure a robust PostgreSQL connection in Python on Windows",
        project_name="the workspace",
        task_brief={"confirmed": True},
    )
    ctx = session.begin()
    print(ctx.to_context_block())
    result = TaskResult(
        task_description=session.task_description,
        skill_used=ctx.skill_selected,
        outcome="success",
        confidence_score=0.9,
        output_summary="Session manager smoke test completed.",
        validation_summary="Smoke test output printed successfully.",
        experience_data={
            "category": "implementation_recipe",
            "title": "Session manager smoke test",
            "content": {
                "conclusion": "Use begin() and close() to keep retrieval, logging, and experience persistence aligned.",
                "context": "MCUM session lifecycle",
            },
        },
    )
    print(session.close(result))
