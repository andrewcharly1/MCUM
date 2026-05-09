"""
Session lifecycle for the MCUM orchestrator.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace

from .. import __version__
from ..logging_utils import get_logger
from ..db.experience_store import (
    adjust_confidence,
    finalize_retrieval_run,
    record_retrieval_run,
    retrieve_for_task,
    save_experience,
)
from ..db.project_registry import (
    estimate_tokens,
    get_or_create_project,
    log_entry,
    log_session_end,
    log_session_start,
)
from ..db.session_playbooks import retrieve_session_playbooks, save_session_playbook
from ..db.skill_catalog import get_skill_record, mark_skill_used, sync_skill_catalog
from .dispatcher import DispatchResult, dispatch
from .skill_factory import run_skill_factory_cycle
from ..policy import (
    load_execution_policy,
    load_intake_policy,
    normalize_task_brief,
    task_brief_metrics,
    validate_task_brief,
)
from ..sisl.autonomous_loop import run_autonomous_improvement

LOGGER = get_logger("core.session_manager")


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
    retrieval_mode: str = "keywords_fallback"
    session_start_ts: float = field(default_factory=time.time)
    log_id: str | None = None
    retrieval_run_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    task_brief: dict = field(default_factory=dict)
    project_scope: str = "unknown"
    playbooks: list[dict] = field(default_factory=list)
    playbook_scope: str = "none"
    retrieval_latency_ms: int = 0
    skill_status: str = "unknown"

    def to_context_block(self) -> str:
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

        if self.failure_patterns:
            lines.append(f"\n## Failure patterns ({len(self.failure_patterns)}):")
            for exp in self.failure_patterns:
                lines.extend(_render_experience(exp, prefix="RISK"))

        if self.conflict_cases:
            lines.append(f"\n## Conflicts ({len(self.conflict_cases)}):")
            for exp in self.conflict_cases:
                lines.extend(_render_experience(exp, prefix="CONFLICT"))

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
        self.execution_policy = load_execution_policy()
        self.task_brief = normalize_task_brief(project_path, task_description, task_brief=task_brief)
        self.task_brief["metrics"] = task_brief_metrics(self.task_brief, self.intake_policy)
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

    def _reinforce_retrieval_confidence(self, outcome: str) -> None:
        if self._ctx is None:
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

    def _run_skill_factory_cycle(self) -> None:
        if not self.auto_improve or self._ctx is None:
            return

        mode = str(self.execution_policy.get("skill_factory_mode", "disabled") or "disabled")
        if mode == "disabled":
            return

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
        except Exception as exc:
            self._log(f"Skill factory warning: {exc}")

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
        files_touched = payload.get("files_touched") or [
            artifact["path"]
            for artifact in result.artifacts
            if artifact.get("exists")
        ]
        issues_avoided = payload.get("issues_avoided") or []
        if isinstance(issues_avoided, str):
            issues_avoided = [issues_avoided]

        try:
            playbook_id = save_session_playbook(
                project_id=self._ctx.project_id,
                skill_name=result.skill_used,
                title=payload.get("title", result.task_description[:120]),
                task_description=result.task_description,
                objective=payload.get("objective") or self.task_brief.get("objective"),
                output_summary=result.output_summary,
                validation_summary=result.validation_summary,
                commands=commands,
                files_touched=files_touched,
                artifacts=result.artifacts,
                issues_avoided=issues_avoided,
                reusable_when=payload.get("reusable_when") or self.task_brief.get("success_criteria"),
                outcome=result.outcome,
                confidence_score=result.confidence_score,
                source_session_id=self.session_id,
                source_task_log_id=task_log_id,
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

    def _apply_result_policy(self, result: TaskResult) -> TaskResult:
        if not self.execution_policy.get("strict_mode", False):
            return result

        outcome = result.outcome
        output_summary = result.output_summary or ""
        error_description = result.error_description
        execution_mode = self.task_brief.get("execution_mode", "ejecutar")
        validation_present = bool(
            result.validation_summary
            or (execution_mode in {"analizar", "proponer"} and output_summary)
        )
        artifacts_required = (
            self.execution_policy.get("require_artifacts_for_success", False)
            and execution_mode == "ejecutar"
        )

        violations: list[str] = []
        if outcome == "success":
            if self.execution_policy.get("require_validation_before_success", False) and not validation_present:
                violations.append("missing_validation")
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

        auto_dispatch_result = None
        if self.force_skill:
            auto_dispatch_result = dispatch(
                task_description=self.task_description,
                project_context=self._project,
                force_skill=None,
            )

        dispatch_result = dispatch(
            task_description=self.task_description,
            project_context=self._project,
            force_skill=self.force_skill,
        )
        self._enforce_dispatch_policy(dispatch_result)
        skill_status = self._enforce_skill_status_policy(dispatch_result.skill_name)
        retrieval_start = time.perf_counter()
        retrieval_result = retrieve_for_task(
            self.task_description,
            skill_context=dispatch_result.skill_name,
            project_id=project_id,
            policy=self.execution_policy,
        )
        playbook_result = retrieve_session_playbooks(
            self.task_description,
            skill_name=dispatch_result.skill_name,
            project_id=project_id,
            limit=int(self.execution_policy.get("max_playbooks", 3) or 3),
            min_similarity=float(self.execution_policy.get("min_playbook_similarity", 0.28) or 0.28),
            allow_cross_project=bool(self.execution_policy.get("allow_cross_project_fallback", True)),
        )
        retrieval_latency_ms = int((time.perf_counter() - retrieval_start) * 1000)

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
                "auto_dispatch": self._dispatch_result_payload(auto_dispatch_result),
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
            retrieval_run_id=retrieval_run_id,
            log_metadata={
                "session_id": self.session_id,
                "alternatives": dispatch_result.alternatives[:3],
                "skill_status": skill_status,
                "playbooks_retrieved": len(playbook_result.get("playbooks", [])),
                "playbook_scope": playbook_result.get("search_scope", "none"),
                "auto_dispatch": self._dispatch_result_payload(auto_dispatch_result),
            },
            retrieval_latency_ms=retrieval_latency_ms,
        )

        warnings = list(dispatch_result.warnings)
        warnings.extend(retrieval_result.get("warnings", []))
        warnings.extend(playbook_result.get("warnings", []))
        if retrieval_result["total_retrieved"] == 0:
            warnings.insert(0, "Cold start: no previous experience was retrieved.")

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
            retrieval_mode=retrieval_result.get("retrieval_mode", "unknown"),
            session_start_ts=self._start_ts,
            log_id=log_id,
            retrieval_run_id=retrieval_run_id,
            warnings=warnings,
            task_brief=self.task_brief,
            project_scope=retrieval_result.get("project_scope", "unknown"),
            playbooks=playbook_result.get("playbooks", []),
            playbook_scope=playbook_result.get("search_scope", "none"),
            retrieval_latency_ms=retrieval_latency_ms,
            skill_status=skill_status,
        )

        self._log(f"Project: {project_name}")
        self._log(
            f"Skill: {dispatch_result.skill_name} via {dispatch_result.match_method} "
            f"| retrieved={retrieval_result['total_retrieved']}"
        )
        return self._ctx

    def close(self, result: TaskResult) -> str:
        if self._ctx is None or self._start_ts is None:
            raise RuntimeError("Call begin() before close().")
        if self._closed:
            raise RuntimeError("Session is already closed.")

        result = self._apply_result_policy(result)
        duration_sec = int(time.time() - self._start_ts)
        wall_clock_ms = int((time.time() - self._start_ts) * 1000)
        experience_id: str | None = None
        orchestrated_skills = self._build_orchestrated_skills(result)
        skill_correction = self._build_skill_correction_metadata(result, orchestrated_skills)

        if result.experience_data and result.outcome in {"success", "partial"}:
            data = result.experience_data
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
                review_notes=data.get("review_notes"),
                initial_score=result.confidence_score,
                skill_version=__version__,
                is_synthetic=bool(data.get("is_synthetic", False)),
            )
            self._log(f"Experience saved: {experience_id}")

        context_tokens_in = estimate_tokens(self._ctx.to_context_block())
        context_tokens_out = result.context_tokens_out
        if context_tokens_out is None:
            context_tokens_out = estimate_tokens(
                {
                    "output_summary": result.output_summary,
                    "validation_summary": result.validation_summary,
                    "error_description": result.error_description,
                }
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
                "final_skill": result.skill_used,
                "delegated_skills": skill_correction["delegated_skills"],
                "skill_correction": skill_correction,
                "auto_dispatch": self._dispatch_result_payload(self._ctx.auto_dispatch_result),
                "validation_summary": result.validation_summary,
            },
        )
        playbook_id = self._save_session_playbook(result, log_id)

        if self._ctx.retrieval_run_id:
            finalize_retrieval_run(
                retrieval_run_id=self._ctx.retrieval_run_id,
                outcome_status=result.outcome,
                outcome_description=result.output_summary or result.error_description,
                final_confidence=result.confidence_score,
                failure_reason=result.error_description if result.outcome == "failure" else None,
            )

        self._reinforce_retrieval_confidence(result.outcome)

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
            extra_metadata={
                "session_id": self.session_id,
                "retrieval_run_id": self._ctx.retrieval_run_id,
                "task_log_id": log_id,
                "playbook_id": playbook_id,
                "selected_skill": self._ctx.skill_selected,
                "final_skill": result.skill_used,
                "skills_orchestrated": orchestrated_skills,
                "skill_correction": skill_correction,
            },
        )

        for skill_name in orchestrated_skills:
            try:
                mark_skill_used(skill_name)
            except Exception as exc:
                self._log(f"Skill usage mark warning for {skill_name}: {exc}")

        self._run_autonomous_improvement(result.skill_used)
        self._run_skill_factory_cycle()
        self._closed = True

        self._log(f"Session closed with outcome={result.outcome}")
        return log_id

    def abort(
        self,
        error_description: str,
        output_summary: str | None = None,
        validation_summary: str | None = None,
        confidence_score: float = 0.0,
    ) -> str:
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
    if project_path:
        project = get_or_create_project(project_path=project_path)
        project_id = project.get("id")
    dispatch_result = dispatch(task_description, project_context=project)
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
    }


if __name__ == "__main__":
    session = OrchestratorSession(
        project_path="C:/Users/carlo/OneDrive/Escritorio/CERTIFICACION LABORAL",
        task_description="Configure a robust PostgreSQL connection in Python on Windows",
        project_name="CERTIFICACION LABORAL",
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
