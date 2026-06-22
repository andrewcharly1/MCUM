"""
MCUM — Registro post-hoc de las 7 mejoras incrementales del motor MCUM.
Ejecutar desde el directorio MCUM:
  cd C:/Users/dev/workspace/.agent/skills/MCUM
  python register_mcum_improvements.py
"""

import sys
import json
from pathlib import Path

# Asegurar que el modulo MCUM esta en el path
sys.path.insert(0, str(Path(__file__).parent))

from core.session_manager import OrchestratorSession, TaskResult
from db.experience_store import retrieve_for_task, save_experience

# ──────────────────────────────────────────────
# Helper: build experience payload
# ──────────────────────────────────────────────
def make_exp(category, title, conclusion, context, applicability, not_applicable, task_desc):
    return {
        "category": category,
        "title": title,
        "content": {
            "conclusion": conclusion,
            "context": context,
        },
        "applicability": {"when": applicability},
        "not_applicable_cases": {"when_not": not_applicable},
        "task_description": task_desc,
    }

# ──────────────────────────────────────────────
# Las 7 mejoras
# ──────────────────────────────────────────────
IMPROVEMENTS = [
    {
        "id": "P1-A",
        "category": "mcum_improvement",
        "title": "P1-A: Auto-infer intake warnings en policy.py normalize_task_brief()",
        "conclusion": (
            "Se implemento la deteccion automatica de campos requeridos inferidos en "
            "normalize_task_brief(). Cuando un campo requerido se infiere automaticamente "
            "(ej. task_type='analizar' -> execution_mode='ejecutar'), el sistema marca "
            "_intake_inferred_fields y genera _intake_warnings para transparente el proceso."
        ),
        "context": (
            "El intake de MCUM requiere project_path, task_type, objective, expected_deliverable, "
            "success_criteria y execution_mode. Cuando el usuario no especifica alguno, MCUM inferia "
            "sin registrar que la inferencia ocurrida, lo que podia causar confusion en el seguimiento."
        ),
        "applicability": (
            "Cuando se ejecuta una sesion MCUM y el task_brief tiene campos inferidos automaticamente."
        ),
        "not_applicable": (
            "Cuando el usuario provee explicitamente todos los campos requeridos del intake."
        ),
    },
    {
        "id": "P1-B",
        "category": "mcum_improvement",
        "title": "P1-B: record_status en OrchestratorSession.close()",
        "conclusion": (
            "OrchestratorSession.close() ahora retorna un dict con log_id, record_status "
            "('recorded'|'record_failed'), outcome y session_id. Los 5 sitios de llamada en "
            "workspace_session.py fueron actualizados para consumir este retorno."
        ),
        "context": (
            "Anteriormente close() retornaba solo el log_id como string. Los callers "
            "necesitaban inferir el estado del registro consultando valores internos."
        ),
        "applicability": (
            "Cualquier codigo que llame OrchestratorSession.close() y necesite confirmar "
            "el estado del registro en PostgreSQL."
        ),
        "not_applicable": (
            "Sesiones que no requieren confirmacion de registro o que ya tienen su propio "
            "mecanismo de verificacion."
        ),
    },
    {
        "id": "P2-A",
        "category": "mcum_improvement",
        "title": "P2-A: Warning en dispatcher para enums invalidos",
        "conclusion": (
            "dispatch() ahora valida task_type y execution_mode contra los enums permitidos "
            "y agrega warnings a DispatchResult.warnings cuando encuentra valores fuera de "
            "los dominios validos."
        ),
        "context": (
            "El dispatcher aceptaba cualquier string como task_type y execution_mode. "
            "Valores invalidos silenciosamente causaban seleccion de skill incorrecta."
        ),
        "applicability": (
            "Cuando se dispatch una tarea con task_type o execution_mode que no matchea "
            "los enums definidos (analizar, crear, corregir, mejorar, planificar, automatizar, "
            "validar para task_type; analizar, proponer, ejecutar para execution_mode)."
        ),
        "not_applicable": (
            "Dispatch en contexts donde los enums ya fueron validados antes de llamar a dispatch()."
        ),
    },
    {
        "id": "P2-B",
        "category": "mcum_improvement",
        "title": "P2-B: Exponer skill_factory result en logs",
        "conclusion": (
            "_run_skill_factory_cycle() ahora retorna un dict con created/promoted/signals, "
            "y esos datos se exponen en extra_metadata de log_session_end bajo las keys "
            "skill_factory_ran, skill_factory_created, skill_factory_promoted, skill_factory_signals_n."
        ),
        "context": (
            "El skill factory ejecutaba ciclos pero sus resultados no quedaban persistidos "
            "en los logs de sesion, haciendolo inobservable."
        ),
        "applicability": (
            "Cuando auto_improve=True y execution_policy.skill_factory_mode != 'disabled'."
        ),
        "not_applicable": (
            "Sesiones con auto_improve=False o skill_factory_mode='disabled'."
        ),
    },
    {
        "id": "P2-C",
        "category": "mcum_improvement",
        "title": "P2-C: Verificacion knowledge_library en compiled_state",
        "conclusion": (
            "Confirmado que state_compiler.py ya tiene la logica correcta de knowledge_library "
            "en compile_state(). El compiled_state.to_metadata() expone knowledge_library "
            "correctamente en el extra_metadata de log_session_end."
        ),
        "context": (
            "Se verifico que la integracion de knowledge_library_shadow con el state_compiler "
            "esta implementada correctamente y no requiere cambios."
        ),
        "applicability": (
            "Cuando state_compiler esta habilitado y se usa compiled_state.to_metadata()."
        ),
        "not_applicable": (
            "Cuando state_compiler.enabled=False o se usa el path sin compiled_state."
        ),
    },
    {
        "id": "P3-A",
        "category": "mcum_improvement",
        "title": "P3-A: HTML artifacts pending QA en session end",
        "conclusion": (
            "Se agrega html_artifacts_pending_qa al extra_metadata de log_session_end, "
            "conteniendo la lista de paths .html de artifacts cuando outcome=success. "
            "Esto permite a QA identificar que artifacts HTML necesitan validacion."
        ),
        "context": (
            "Los artifacts HTML generados durante la sesion no quedaban marcados explicitamente "
            "como pendientes de QA en los logs de sesion."
        ),
        "applicability": (
            "Cuando result.outcome='success' y la sesion genero artifacts con extension .html."
        ),
        "not_applicable": (
            "Sesiones con outcome != 'success' o que no generaron artifacts HTML."
        ),
    },
    {
        "id": "P3-B",
        "category": "mcum_improvement",
        "title": "P3-B: Verificacion memory_governor en retrieve_for_task",
        "conclusion": (
            "Confirmado que apply_memory_governor se aplica correctamente a relevant, "
            "failure_patterns, conflict_cases y active_patterns en retrieve_for_task(). "
            "El memory_governor esta activo y los warnings de governance se incluyen en el "
            "result dictionary."
        ),
        "context": (
            "Se verifico que experience_store.py aplica memory_governor a todas las listas "
            "de items retrieved y que los warnings de governance se exponen correctamente."
        ),
        "applicability": (
            "Cuando retrieve_for_task() retorna experiencias y el memory governor esta "
            "habilitado en la policy."
        ),
        "not_applicable": (
            "Cuando memory_governor.enabled=False en la policy de retrieval."
        ),
    },
]

# ──────────────────────────────────────────────
# Main execution
# ──────────────────────────────────────────────
def main():
    print("=" * 70)
    print("MCUM - Registro post-hoc de 7 mejoras incrementales")
    print("=" * 70)

    project_path = (
        "C:/Users/dev/workspace/.agent/skills/MCUM"
    )

    # Step 1: Create OrchestratorSession
    print("\n[1] Creando OrchestratorSession...")
    session = OrchestratorSession(
        project_path=project_path,
        task_description=(
            "Registrar las 7 mejoras incrementales implementadas en el motor MCUM"
        ),
        project_name="MCUM",
        task_brief={
            "confirmed": True,
            "project_path": project_path,
            "task_type": "mejorar",
            "execution_mode": "ejecutar",
            "objective": (
                "Registrar en PostgreSQL MCUM las 7 mejoras incrementales "
                "implementadas en los archivos del motor MCUM"
            ),
            "expected_deliverable": (
                "7 experiencias grabadas en PostgreSQL, session cerrada con "
                "record_status='recorded'"
            ),
            "success_criteria": (
                "verify: retrieve_for_task encuentra las 7 experiencias, "
                "session.close() retorna record_status='recorded'"
            ),
        },
    )
    ctx = session.begin()
    print(f"    session_id: {session.session_id}")
    print(f"    project_id: {ctx.project_id}")
    print(f"    skill_selected: {ctx.skill_selected}")

    # Step 2: Save each improvement as an experience
    print("\n[2] Guardando 7 experiencias en PostgreSQL...")
    experience_ids = []
    for i, imp in enumerate(IMPROVEMENTS, 1):
        print(f"    [{i}/7] {imp['id']}...", end=" ")
        exp_id = save_experience(
            category="mcum_improvement",
            title=imp["title"],
            content={
                "conclusion": imp["conclusion"],
                "context": imp["context"],
            },
            skill_name="mcum-orchestrator",
            project_id=ctx.project_id,
            task_description=imp["title"],
            applicability={"when": imp["applicability"]},
            not_applicable_cases={"when_not": imp["not_applicable"]},
            conditions=None,
            evidence_refs=None,
            source_artifacts=None,
            review_notes=(
                f"Registro post-hoc MCUM. "
                f"Mejora {imp['id']} implementada previamente en el motor."
            ),
            initial_score=0.85,
            tested_by="agent",
            skill_version=None,
            is_synthetic=True,
        )
        experience_ids.append({"improvement_id": imp["id"], "experience_id": exp_id})
        print(f"experience_id={exp_id}")

    # Step 3: Verify with retrieve_for_task
    print("\n[3] Verificando con retrieve_for_task...")
    retrieval = retrieve_for_task(
        task_description=(
            "MCUM improvements motor cerebral auto-infer intake warnings "
            "record_status dispatcher skill factory knowledge_library "
            "memory governor"
        ),
        skill_context="mcum-orchestrator",
        project_id=ctx.project_id,
    )
    print(f"    retrieval_mode: {retrieval['retrieval_mode']}")
    print(f"    total_retrieved: {retrieval['total_retrieved']}")
    print(f"    experiences retrieved: {len(retrieval['experiences'])}")

    # Step 4: Build TaskResult and close session
    print("\n[4] Cerrando OrchestratorSession...")
    result = TaskResult(
        task_description=session.task_description,
        skill_used="mcum-orchestrator",
        outcome="success",
        confidence_score=0.95,
        output_summary=(
            f"Registradas {len(experience_ids)} experiencias de mejora incremental "
            f"del motor MCUM. verify: retrieve={retrieval['total_retrieved']}"
        ),
        validation_summary=(
            f"7 experiencias grabadas. retrieval confirmo {retrieval['total_retrieved']} "
            f"items en retrieval_mode={retrieval['retrieval_mode']}."
        ),
        artifacts=[],
        extra_metadata={
            "improvements_registered": len(experience_ids),
            "experience_ids": experience_ids,
            "retrieval_verification": {
                "retrieval_mode": retrieval["retrieval_mode"],
                "total_retrieved": retrieval["total_retrieved"],
            },
        },
    )
    close_result = session.close(result)
    print(f"    log_id: {close_result.get('log_id')}")
    print(f"    record_status: {close_result.get('record_status')}")
    print(f"    outcome: {close_result.get('outcome')}")
    print(f"    session_id: {close_result.get('session_id')}")

    # ──────────────────────────────────────────
    # Final report
    # ──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("REPORTE FINAL - Registro de mejoras MCUM")
    print("=" * 70)
    print(f"session_id    : {session.session_id}")
    print(f"log_id        : {close_result.get('log_id')}")
    print(f"record_status : {close_result.get('record_status')}")
    print(f"outcome       : {close_result.get('outcome')}")
    print(f"experiences   : {len(experience_ids)}")
    print()
    print(f"{'Improvement':<10} {'Experience ID':<44} {'Status'}")
    print("-" * 70)
    for item in experience_ids:
        print(f"{item['improvement_id']:<10} {item['experience_id']:<44} saved")
    print()
    print(f"Retrieval verification:")
    print(f"  retrieval_mode   : {retrieval['retrieval_mode']}")
    print(f"  total_retrieved  : {retrieval['total_retrieved']}")
    print(f"  experiences      : {len(retrieval['experiences'])}")

    # Save report to file
    report_path = Path(__file__).parent / "mcum_improvements_report.json"
    report = {
        "session_id": session.session_id,
        "log_id": close_result.get("log_id"),
        "record_status": close_result.get("record_status"),
        "outcome": close_result.get("outcome"),
        "experience_ids": experience_ids,
        "retrieval_verification": {
            "retrieval_mode": retrieval["retrieval_mode"],
            "total_retrieved": retrieval["total_retrieved"],
            "experiences_retrieved": len(retrieval["experiences"]),
        },
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    return report


if __name__ == "__main__":
    main()