"""
Adversarial test generation from real failures and degraded retrieval outcomes.
"""

from __future__ import annotations

import json
import uuid

from ..db.connection import get_db, get_cursor


def _make_failure_pattern_test(skill_name: str, row: dict, difficulty: int = 4) -> dict:
    content = row.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)

    conclusion = content.get("conclusion", row.get("title", "Advert a known risk"))
    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "negative_case",
        "partition": "adversarial",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": str(row["id"]) if row.get("id") else None,
        "input_query": (
            f"El usuario quiere ejecutar: {row.get('task_description') or row.get('title', '')}. "
            f"Que riesgo debes advertir antes de proceder?"
        ),
        "expected_result": f"Advertir el riesgo: {conclusion}",
        "expected_source": f"failure_pattern:{row.get('id', '')}",
        "expected_steps": {
            "step1": "Detectar el failure pattern relevante",
            "step2": f"Advertir antes de actuar: {conclusion[:160]}",
            "step3": "Proponer una alternativa o validacion segura",
        },
        "pass_condition": f"La respuesta incluye una advertencia concreta sobre: {conclusion[:120]}",
    }


def _make_failed_run_test(skill_name: str, row: dict, difficulty: int = 5) -> dict:
    failure_reason = (row.get("failure_reason") or row.get("outcome_description") or "No repetir el fallo").strip()
    input_context = (row.get("input_context") or row.get("decision_taken") or skill_name).strip()
    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "conflict_resolution",
        "partition": "adversarial",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": None,
        "input_query": input_context[:280],
        "expected_result": f"No repetir el fallo: {failure_reason}",
        "expected_source": f"retrieval_run:{row.get('id', '')}",
        "expected_steps": {
            "step1": "Reconocer que hubo una ejecucion fallida similar",
            "step2": f"Advertir el fallo previo: {failure_reason[:160]}",
            "step3": "Cambiar estrategia antes de continuar",
        },
        "pass_condition": f"La respuesta evita repetir el error: {failure_reason[:120]}",
    }


def _make_degraded_confidence_test(skill_name: str, row: dict, difficulty: int = 4) -> dict:
    content = row.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)
    conclusion = content.get("conclusion", row.get("title", "Validar antes de reutilizar"))
    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "negative_case",
        "partition": "adversarial",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": str(row["id"]) if row.get("id") else None,
        "input_query": (
            f"Existe un antecedente degradado para: {row.get('task_description') or row.get('title', '')}. "
            f"Como deberias manejarlo para no sobreconfiar?"
        ),
        "expected_result": f"Validar o limitar la reutilizacion de: {conclusion}",
        "expected_source": f"degraded_experience:{row.get('id', '')}",
        "expected_steps": {
            "step1": "Reconocer que la confianza del antecedente cayo",
            "step2": "Advertir la degradacion o necesidad de revalidacion",
            "step3": "No reutilizarlo ciegamente",
        },
        "pass_condition": f"La respuesta exige revalidar o limitar el uso de: {conclusion[:120]}",
    }


def generate_adversarial_tests(
    skill_name: str,
    *,
    limit: int = 5,
    min_confidence: float = 0.50,
) -> list[dict]:
    if limit <= 0:
        return []

    tests: list[dict] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, title, task_description, content, current_confidence
                FROM core_brain.experiences
                WHERE skill_name = %s
                  AND category = 'failure_pattern'
                  AND current_confidence >= %s
                  AND superseded_by IS NULL
                ORDER BY current_confidence DESC, created_at DESC
                LIMIT %s
                """,
                (skill_name, min_confidence, limit),
            )
            for row in cur.fetchall():
                tests.append(_make_failure_pattern_test(skill_name, dict(row)))
                if len(tests) >= limit:
                    return tests[:limit]

            cur.execute(
                """
                SELECT id, input_context, failure_reason, outcome_description, decision_taken
                FROM core_brain.retrieval_runs
                WHERE skill_name = %s
                  AND outcome_status = 'failure'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (skill_name, limit),
            )
            for row in cur.fetchall():
                tests.append(_make_failed_run_test(skill_name, dict(row)))
                if len(tests) >= limit:
                    return tests[:limit]

            cur.execute(
                """
                SELECT id, title, task_description, content, initial_score, current_confidence
                FROM core_brain.experiences
                WHERE skill_name = %s
                  AND current_confidence < initial_score
                  AND superseded_by IS NULL
                ORDER BY (initial_score - current_confidence) DESC, created_at DESC
                LIMIT %s
                """,
                (skill_name, limit),
            )
            for row in cur.fetchall():
                tests.append(_make_degraded_confidence_test(skill_name, dict(row)))
                if len(tests) >= limit:
                    break

    return tests[:limit]
