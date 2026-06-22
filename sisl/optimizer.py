"""
Optimizer for the MCUM SISL loop.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from ..db.connection import get_db, get_cursor
from ..db.experience_store import get_failure_patterns
from ..policy import load_execution_policy
from .skill_writer import apply_sisl_proposals, rollback_sisl_writeback
from .test_runner import SkillEvalResult


IMPROVEMENT_TYPES = {
    "add_case": "Agregar caso de uso faltante al SKILL.md",
    "add_not_applicable": "Agregar caso NOT APPLICABLE que no estaba documentado",
    "add_failure_warning": "Agregar advertencia de failure_pattern al SKILL.md",
    "clarify_trigger": "Clarificar trigger/activacion del skill",
    "add_example": "Agregar ejemplo concreto de aplicacion",
    "fix_pass_condition": "Corregir pass_condition demasiado estricta o laxa",
    "cold_start_coverage": "Agregar mas experiences base (cold start detectado)",
}


@dataclass
class ImprovementProposal:
    improvement_type: str
    section_to_edit: str
    current_text: str
    proposed_text: str
    confidence: float
    evidence: str
    test_ids_failing: list[str] = field(default_factory=list)


@dataclass
class OptimizationReport:
    skill_name: str
    skill_version: str
    previous_ckl: float
    target_ckl: float
    proposals: list[ImprovementProposal]
    failure_patterns: list[dict]
    cold_start_flag: bool
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def has_improvements(self) -> bool:
        return len(self.proposals) > 0

    def high_confidence_proposals(self, threshold: float = 0.70) -> list[ImprovementProposal]:
        return [proposal for proposal in self.proposals if proposal.confidence >= threshold]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "previous_ckl": self.previous_ckl,
            "target_ckl": self.target_ckl,
            "cold_start_flag": self.cold_start_flag,
            "n_proposals": len(self.proposals),
            "n_high_confidence": len(self.high_confidence_proposals()),
            "proposals": [
                {
                    "type": proposal.improvement_type,
                    "section": proposal.section_to_edit,
                    "evidence": proposal.evidence[:200],
                    "confidence": proposal.confidence,
                    "proposed": proposal.proposed_text[:300],
                }
                for proposal in self.proposals
            ],
        }


def _analyze_failures(eval_result: SkillEvalResult) -> dict:
    analysis = {
        "total_failures": 0,
        "cold_start_count": 0,
        "not_app_missing": 0,
        "factual_failures": [],
        "negative_failures": [],
        "adv_failures": [],
    }

    for test in eval_result.test_results:
        if test.passed:
            continue
        analysis["total_failures"] += 1

        lowered_reason = test.reason.lower()
        if "cold start" in lowered_reason or "sin experience" in lowered_reason:
            analysis["cold_start_count"] += 1
        if "not_applicable" in lowered_reason or "signals" in lowered_reason:
            analysis["not_app_missing"] += 1

        entry = {
            "test_id": test.test_id,
            "score": test.score,
            "reason": test.reason,
            "preview": test.response_preview,
        }

        if test.partition == "adversarial":
            analysis["adv_failures"].append(entry)
        elif test.test_type == "factual_retrieval":
            analysis["factual_failures"].append(entry)
        else:
            analysis["negative_failures"].append(entry)

    return analysis


def _propose_cold_start_fix(skill_name: str, count: int) -> ImprovementProposal:
    return ImprovementProposal(
        improvement_type="cold_start_coverage",
        section_to_edit="OUTPUT CONTRACT / Casos de Uso",
        current_text="",
        proposed_text=(
            f"ACCION REQUERIDA: El skill '{skill_name}' tiene {count} failures por falta de experiencias. "
            f"Completa tareas reales con este skill, guarda experiences al cerrar la sesion y reevalua."
        ),
        confidence=0.95,
        evidence=f"{count} tests fallaron por cold start o falta de experiencias recuperadas.",
    )


def _propose_not_applicable_addition(skill_name: str, count: int) -> ImprovementProposal:
    return ImprovementProposal(
        improvement_type="add_not_applicable",
        section_to_edit="INPUT CONTRACT / NEED_INFO",
        current_text="",
        proposed_text=(
            f"Se detectaron {count} casos donde el skill '{skill_name}' carece de guardrails NOT APPLICABLE. "
            "Documenta explicitamente cuando no aplica y que alternativa debe proponerse."
        ),
        confidence=0.80,
        evidence=f"{count} tests indicaron falta de not_applicable_cases o guardrails negativos.",
    )


def _propose_failure_warning(skill_name: str, failure_exp: dict) -> ImprovementProposal:
    content = failure_exp.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)
    conclusion = content.get("conclusion", "") if isinstance(content, dict) else ""

    return ImprovementProposal(
        improvement_type="add_failure_warning",
        section_to_edit="REGLAS DETERMINISTAS / Paso 2 RETRIEVAL",
        current_text="",
        proposed_text=(
            f"RIESGO DOCUMENTADO: {failure_exp.get('title', '')}. "
            f"Advertir antes de ejecutar: {conclusion}. "
            f"Contexto no aplicable: {failure_exp.get('task_description', 'N/A')}"
        ),
        confidence=0.75,
        evidence=f"failure_pattern con confianza {failure_exp.get('current_confidence', 0):.2f}",
    )


def _propose_context_block_addition(skill_name: str, factual_failures: list[dict]) -> ImprovementProposal:
    examples = "\n".join(f"  - {failure['reason'][:100]}" for failure in factual_failures[:3])
    return ImprovementProposal(
        improvement_type="add_case",
        section_to_edit="PROTOCOLO DE EJECUCION / Paso 4",
        current_text="",
        proposed_text=f"Agregar al SKILL.md el manejo de los siguientes casos sub-documentados:\n{examples}",
        confidence=0.65,
        evidence=f"{len(factual_failures)} tests factuales fallaron por cobertura incompleta.",
        test_ids_failing=[failure["test_id"] for failure in factual_failures[:5]],
    )


def analyze_and_propose(
    skill_name: str,
    eval_result: SkillEvalResult,
    target_ckl: float = 0.85,
) -> OptimizationReport:
    proposals: list[ImprovementProposal] = []
    analysis = _analyze_failures(eval_result)
    failure_patterns = get_failure_patterns(
        query_text=skill_name,
        min_confidence=0.50,
        limit=5,
    )
    failure_patterns = [
        item for item in failure_patterns
        if str(item.get("skill_name") or skill_name) == skill_name
    ]

    cold_start_flag = analysis["cold_start_count"] > (eval_result.val_total * 0.30)

    if analysis["cold_start_count"] > 0:
        proposals.append(_propose_cold_start_fix(skill_name, analysis["cold_start_count"]))
    if analysis["not_app_missing"] > 0:
        proposals.append(_propose_not_applicable_addition(skill_name, analysis["not_app_missing"]))
    for failure_pattern in failure_patterns[:2]:
        proposals.append(_propose_failure_warning(skill_name, failure_pattern))
    if analysis["factual_failures"]:
        proposals.append(_propose_context_block_addition(skill_name, analysis["factual_failures"]))

    return OptimizationReport(
        skill_name=skill_name,
        skill_version=eval_result.skill_version,
        previous_ckl=eval_result.ckl_score,
        target_ckl=target_ckl,
        proposals=proposals,
        failure_patterns=failure_patterns,
        cold_start_flag=cold_start_flag,
    )


def _next_version(skill_version: str) -> str:
    parts = skill_version.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return skill_version + "-optimized"


def save_optimization_report(report: OptimizationReport, skill_version: str) -> dict:
    version_id = str(uuid.uuid4())
    high_conf = report.high_confidence_proposals()
    new_version = _next_version(skill_version)

    diff_patch = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)[:2000]
    description = (
        f"[SISL Optimizer] CKL={report.previous_ckl:.3f} -> target={report.target_ckl:.2f}. "
        f"{len(report.proposals)} mejoras propuestas ({len(high_conf)} de alta confianza)."
    )

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.skill_versions (
                    id, skill_name, version_semver, ckl_score,
                    status, changes_description, diff_patch, improvement_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    version_id,
                    report.skill_name,
                    new_version,
                    report.previous_ckl,
                    "testing",
                    description,
                    diff_patch,
                    "sisl_loop",
                ),
            )
            row = cur.fetchone()
            return {
                "id": str(row["id"]) if row else version_id,
                "version": new_version,
            }


def _update_skill_version_status(
    version_id: str | None,
    *,
    status: str,
    note: str | None = None,
    ckl_score: float | None = None,
) -> None:
    if not version_id:
        return

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE core_brain.skill_versions
                SET status = %s,
                    ckl_score = COALESCE(%s, ckl_score),
                    changes_description = CASE
                        WHEN %s::text IS NULL OR %s::text = '' THEN changes_description
                        ELSE changes_description || E'\n' || %s::text
                    END
                WHERE id = %s
                """,
                (status, ckl_score, note, note, note, version_id),
            )


def apply_high_confidence_improvements(
    report: OptimizationReport,
    skill_md_path: str | None = None,
    dry_run: bool = True,
    writeback_mode: str = "disabled",
) -> list[dict]:
    applied: list[dict] = []
    high_conf = report.high_confidence_proposals(threshold=0.70)

    if not high_conf:
        return []

    execution_policy = load_execution_policy()
    effective_mode = writeback_mode or execution_policy.get("autonomous_writeback", "disabled")
    if not dry_run and effective_mode == "disabled":
        return [
            {
                "type": proposal.improvement_type,
                "applied": False,
                "dry_run": False,
                "note": "Execution policy blocks autonomous writeback to SKILL.md.",
            }
            for proposal in high_conf
        ]

    if dry_run:
        return [
            {
                "type": proposal.improvement_type,
                "applied": False,
                "dry_run": True,
            }
            for proposal in high_conf
        ]

    write_result = apply_sisl_proposals(report.skill_name, high_conf, skill_md_path=skill_md_path)
    if not write_result.get("applied"):
        return [
            {
                "type": proposal.improvement_type,
                "applied": False,
                "dry_run": False,
                "note": write_result.get("note", "No se pudo aplicar el writeback SISL."),
            }
            for proposal in high_conf
        ]

    for proposal in high_conf:
        applied.append(
            {
                "type": proposal.improvement_type,
                "applied": True,
                "dry_run": False,
                "path": write_result["path"],
                "backup_path": write_result.get("backup_path"),
                "mode": write_result["mode"],
                "sections": write_result.get("sections", []),
                "note": "Structured SISL candidate writeback materialized in SKILL.md.",
            }
        )
    return applied


def _gate_writeback(
    baseline: SkillEvalResult,
    candidate: SkillEvalResult,
) -> tuple[bool, str]:
    improved = (
        candidate.ckl_score > baseline.ckl_score
        or candidate.adv_score > baseline.adv_score
        or candidate.val_score > baseline.val_score
    )
    no_regression = (
        candidate.ckl_score >= baseline.ckl_score
        and candidate.adv_score >= baseline.adv_score
        and candidate.val_score >= baseline.val_score
    )
    if improved and no_regression:
        return True, "Candidate writeback improved or preserved CKL while increasing evaluation quality."
    ceiling_preserved = (
        baseline.ckl_score >= 0.99
        and baseline.adv_score >= 0.99
        and baseline.val_score >= 0.99
        and no_regression
    )
    if ceiling_preserved:
        return True, "Candidate writeback preserved ceiling-level evaluation while codifying guardrails."
    return False, "Candidate writeback did not improve CKL/adversarial score and was rolled back."


def run_sisl_cycle(
    skill_name: str,
    skill_version: str = "1.0.0",
    target_ckl: float = 0.85,
    verbose: bool = True,
    dry_run: bool = True,
    persist_eval: bool = True,
    writeback_mode: str | None = None,
) -> dict:
    from .test_runner import run_evaluation, save_eval_to_db

    if verbose:
        print("\n" + ("=" * 50))
        print(f"SISL CYCLE: {skill_name} v{skill_version}")
        print(f"Target CKL: {target_ckl}")
        print(("=" * 50) + "\n")

    if verbose:
        print("1. Evaluando skill...")
    eval_result = run_evaluation(skill_name, skill_version, verbose=verbose)

    if verbose:
        print("\nResultado:")
        print(eval_result.summary())

    eval_record_id = None
    if persist_eval and eval_result.test_results:
        eval_record_id = save_eval_to_db(eval_result)
        if verbose:
            print(f"   Evaluacion guardada en DB: {eval_record_id[:8]}...")

    if verbose:
        print("\n2. Analizando failures y generando propuestas...")
    report = analyze_and_propose(skill_name, eval_result, target_ckl)

    if verbose:
        print(f"   Propuestas: {len(report.proposals)}")
        print(f"   Alta confianza: {len(report.high_confidence_proposals())}")
        if report.cold_start_flag:
            print("   Cold start detectado: el sistema necesita mas uso real")

    report_record = None
    if eval_result.ckl_score > 0 or report.proposals:
        report_record = save_optimization_report(report, skill_version)
        if verbose:
            print(f"\n3. Reporte guardado en DB: {report_record['id'][:8]}...")

    if verbose:
        print("\n4. Mejoras de alta confianza:")
    resolved_writeback_mode = writeback_mode or load_execution_policy().get("autonomous_writeback", "disabled")
    applied = apply_high_confidence_improvements(
        report,
        dry_run=dry_run,
        writeback_mode=resolved_writeback_mode,
    )

    gate_result = None
    candidate_eval_result = None
    candidate_eval_record_id = None

    if not dry_run and applied and applied[0].get("applied"):
        candidate_eval_result = run_evaluation(
            skill_name,
            report_record["version"] if report_record else skill_version,
            verbose=False,
        )
        if persist_eval and candidate_eval_result.test_results:
            candidate_eval_record_id = save_eval_to_db(candidate_eval_result)

        accepted, gate_note = _gate_writeback(eval_result, candidate_eval_result)
        gate_result = {
            "accepted": accepted,
            "note": gate_note,
            "baseline_ckl": eval_result.ckl_score,
            "candidate_ckl": candidate_eval_result.ckl_score,
            "baseline_adv": eval_result.adv_score,
            "candidate_adv": candidate_eval_result.adv_score,
        }

        if accepted:
            target_status = "active" if resolved_writeback_mode == "enabled" else "testing"
            _update_skill_version_status(
                report_record["id"] if report_record else None,
                status=target_status,
                note=gate_note,
                ckl_score=candidate_eval_result.ckl_score,
            )
            for item in applied:
                item["accepted"] = True
                item["gate_note"] = gate_note
        else:
            rollback_sisl_writeback(applied[0]["path"], applied[0].get("backup_path"))
            _update_skill_version_status(
                report_record["id"] if report_record else None,
                status="deprecated",
                note=gate_note,
                ckl_score=eval_result.ckl_score,
            )
            for item in applied:
                item["accepted"] = False
                item["rolled_back"] = True
                item["gate_note"] = gate_note

    final_ckl = candidate_eval_result.ckl_score if gate_result and gate_result["accepted"] and candidate_eval_result else eval_result.ckl_score
    should_continue = final_ckl < target_ckl

    result = {
        "ckl_score": final_ckl,
        "baseline_ckl_score": eval_result.ckl_score,
        "proposals_n": len(report.proposals),
        "high_conf_n": len(report.high_confidence_proposals()),
        "applied": applied,
        "report": report,
        "eval_result": eval_result,
        "eval_record_id": eval_record_id,
        "candidate_eval_result": candidate_eval_result,
        "candidate_eval_record_id": candidate_eval_record_id,
        "report_id": report_record["id"] if report_record else None,
        "report_version": report_record["version"] if report_record else None,
        "gate_result": gate_result,
        "should_continue": should_continue,
    }

    if verbose:
        print("\n" + ("-" * 50))
        if should_continue:
            gap = target_ckl - final_ckl
            print(f"CKL={final_ckl:.3f} | Gap={gap:.3f} | Continuar iterando")
        else:
            print(f"CKL={final_ckl:.3f} >= {target_ckl} | Target alcanzado")

    return result


if __name__ == "__main__":
    print("MCUM SISL - Optimizer")
    print("-" * 50)

    result = run_sisl_cycle(
        skill_name="mcum-orchestrator",
        skill_version="1.0.0",
        target_ckl=0.85,
        verbose=True,
        dry_run=True,
    )

    print("\nCiclo SISL completado")
    print(f"   CKL: {result['ckl_score']:.3f}")
    print(f"   Propuestas: {result['proposals_n']}")
    print(f"   Alta confianza: {result['high_conf_n']}")
    print(f"   Continuar: {result['should_continue']}")
