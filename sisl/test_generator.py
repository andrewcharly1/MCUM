"""
Automatic test generation for the MCUM SISL loop.
"""

from __future__ import annotations

import json
import uuid

from ..db.connection import get_db, get_cursor
from ..db.experience_store import get_failure_patterns, search_by_keywords
from .adversarial_test_generator import generate_adversarial_tests


TEST_BUDGET_RATIOS = {
    "factual": 0.60,
    "negative": 0.25,
    "adversarial": 0.15,
}


def _allocate_test_budgets(max_tests: int) -> dict[str, int]:
    """Allocate an exact split whose total always equals max_tests."""
    if max_tests <= 0:
        return {"factual": 0, "negative": 0, "adversarial": 0}

    factual_budget = int(max_tests * TEST_BUDGET_RATIOS["factual"])
    negative_budget = int(max_tests * TEST_BUDGET_RATIOS["negative"])
    adversarial_budget = max_tests - factual_budget - negative_budget

    if factual_budget == 0:
        factual_budget = 1
        if adversarial_budget > 0:
            adversarial_budget -= 1
        elif negative_budget > 0:
            negative_budget -= 1

    return {
        "factual": factual_budget,
        "negative": negative_budget,
        "adversarial": adversarial_budget,
    }


def _make_factual_test(skill_name: str, experience: dict, difficulty: int = 2) -> dict:
    content = experience.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)

    conclusion = content.get("conclusion", "")
    context = content.get("context", "")

    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "factual_retrieval",
        "partition": "val",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": str(experience["id"]) if experience.get("id") else None,
        "input_query": f"Para la situacion: {experience.get('title', '')} - cual es la conclusion principal?",
        "expected_result": conclusion,
        "expected_source": f"experience:{experience.get('id', '')}",
        "expected_steps": {
            "step1": "Identificar que la tarea es relevante para este contexto",
            "step2": "Recuperar la experience correspondiente",
            "step3": f"Aplicar: {conclusion[:200]}",
        },
        "pass_condition": (
            f"La respuesta menciona: '{conclusion[:80]}' o aplica correctamente el principio"
            f" en contexto de: {context[:100]}"
        ),
    }


def _make_negative_test(skill_name: str, experience: dict, difficulty: int = 3) -> dict | None:
    not_app = experience.get("not_applicable_cases", {})
    if isinstance(not_app, str):
        not_app = json.loads(not_app)

    when_not = not_app.get("when_not") if isinstance(not_app, dict) else None
    if not when_not:
        return None

    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "negative_case",
        "partition": "val",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": str(experience["id"]) if experience.get("id") else None,
        "input_query": (
            f"Situacion: {when_not}. "
            f"Debes aplicar la solucion de '{experience.get('title', '')}' en este contexto?"
        ),
        "expected_result": "NO. Esta solucion no aplica en este contexto.",
        "expected_source": f"experience:{experience.get('id', '')}:not_applicable",
        "expected_steps": {
            "step1": "Verificar si el contexto actual coincide con not_applicable_cases",
            "step2": "Identificar que no aplica",
            "step3": "Explicar alternativa o recomendar buscar otra solucion",
        },
        "pass_condition": (
            f"La respuesta indica claramente que la solucion no aplica cuando: {when_not[:100]}"
        ),
    }


def _make_failure_test(skill_name: str, failure_exp: dict, difficulty: int = 4) -> dict:
    content = failure_exp.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)

    conclusion = content.get("conclusion", "No cometer este error")

    return {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "test_type": "negative_case",
        "partition": "adversarial",
        "difficulty": difficulty,
        "generated_by": "sisl",
        "source_experience_id": str(failure_exp["id"]) if failure_exp.get("id") else None,
        "input_query": (
            f"El usuario quiere hacer: {failure_exp.get('task_description') or failure_exp.get('title', '')}. "
            f"Que riesgos debes advertir antes de proceder?"
        ),
        "expected_result": f"Advertir: {conclusion}",
        "expected_source": f"failure_pattern:{failure_exp.get('id', '')}",
        "expected_steps": {
            "step1": "Detectar el failure pattern relevante",
            "step2": f"Advertir antes de ejecutar: {conclusion[:150]}",
            "step3": "Proponer alternativa segura",
        },
        "pass_condition": f"La respuesta incluye advertencia especifica sobre: '{conclusion[:80]}'",
    }


def generate_tests_for_skill(
    skill_name: str,
    max_tests: int = 20,
    min_confidence: float = 0.70,
) -> list[dict]:
    tests: list[dict] = []
    budgets = _allocate_test_budgets(max_tests)

    positive_exps = search_by_keywords(
        keywords=[],
        skill_name=skill_name,
        min_confidence=min_confidence,
        limit=budgets["factual"],
    )

    for exp in positive_exps:
        tests.append(_make_factual_test(skill_name, exp))

    if not positive_exps:
        general_exps = search_by_keywords(
            keywords=[skill_name.replace("-", " ")],
            min_confidence=min_confidence,
            limit=budgets["factual"],
        )
        for exp in general_exps:
            tests.append(_make_factual_test(skill_name, exp, difficulty=3))

    all_exps_with_not_app = search_by_keywords(
        keywords=[],
        skill_name=skill_name,
        min_confidence=min_confidence,
        limit=50,
    )

    negative_count = 0
    for exp in all_exps_with_not_app:
        if negative_count >= budgets["negative"]:
            break
        neg_test = _make_negative_test(skill_name, exp)
        if neg_test:
            tests.append(neg_test)
            negative_count += 1

    adversarial_tests = generate_adversarial_tests(
        skill_name,
        limit=budgets["adversarial"],
        min_confidence=0.50,
    )
    tests.extend(adversarial_tests)

    if len(adversarial_tests) < budgets["adversarial"]:
        failure_patterns = get_failure_patterns(
            query_text=skill_name,
            min_confidence=0.50,
            limit=budgets["adversarial"] - len(adversarial_tests),
        )
        for fp in failure_patterns:
            tests.append(_make_failure_test(skill_name, fp, difficulty=4))

    return tests[:max_tests]


def save_tests_to_db(tests: list[dict]) -> list[str]:
    if not tests:
        return []

    inserted: list[str] = []
    with get_db() as conn:
        with get_cursor(conn) as cur:
            for test in tests:
                cur.execute(
                    """
                    SELECT id
                    FROM core_brain.test_suite
                    WHERE skill_name = %s
                      AND test_type = %s
                      AND partition = %s
                      AND input_query = %s
                      AND COALESCE(expected_source, '') = COALESCE(%s, '')
                      AND is_active = TRUE
                    LIMIT 1
                    """,
                    (
                        test["skill_name"],
                        test["test_type"],
                        test["partition"],
                        test["input_query"],
                        test.get("expected_source"),
                    ),
                )
                existing = cur.fetchone()
                if existing:
                    inserted.append(str(existing["id"]))
                    continue

                cur.execute(
                    """
                    INSERT INTO core_brain.test_suite (
                        id, skill_name, test_type, partition, difficulty, generated_by,
                        source_experience_id, input_query, expected_result,
                        expected_source, expected_steps, pass_condition, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        test["id"],
                        test["skill_name"],
                        test["test_type"],
                        test["partition"],
                        test["difficulty"],
                        test["generated_by"],
                        test.get("source_experience_id"),
                        test["input_query"],
                        test["expected_result"],
                        test.get("expected_source"),
                        json.dumps(test.get("expected_steps", {})),
                        test["pass_condition"],
                    ),
                )
                row = cur.fetchone()
                if row:
                    inserted.append(str(row["id"]))
    return inserted


def generate_and_save(skill_name: str, max_tests: int = 20) -> dict:
    tests = generate_tests_for_skill(skill_name, max_tests)

    factual = sum(1 for test in tests if test["test_type"] == "factual_retrieval")
    negative = sum(
        1 for test in tests
        if test["partition"] == "val" and test["test_type"] == "negative_case"
    )
    adversarial = sum(1 for test in tests if test["partition"] == "adversarial")

    saved_ids = save_tests_to_db(tests)

    return {
        "skill_name": skill_name,
        "total_generated": len(tests),
        "saved_ids": saved_ids,
        "breakdown": {
            "factual": factual,
            "negative": negative,
            "adversarial": adversarial,
        },
    }


def get_test_suite(skill_name: str, partition: str | None = "val") -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            if partition:
                cur.execute(
                    """
                    SELECT id, skill_name, test_type, partition, difficulty,
                           input_query, expected_result, expected_source, expected_steps, pass_condition,
                           source_experience_id, generated_by, created_at
                    FROM core_brain.test_suite
                    WHERE skill_name = %s AND partition = %s AND is_active = TRUE
                    ORDER BY difficulty ASC, created_at DESC
                    """,
                    (skill_name, partition),
                )
            else:
                cur.execute(
                    """
                    SELECT id, skill_name, test_type, partition, difficulty,
                           input_query, expected_result, expected_source, expected_steps, pass_condition,
                           source_experience_id, generated_by, created_at
                    FROM core_brain.test_suite
                    WHERE skill_name = %s AND is_active = TRUE
                    ORDER BY difficulty ASC, created_at DESC
                    """,
                    (skill_name,),
                )
            return [dict(row) for row in cur.fetchall()]


def list_skill_test_counts() -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT skill_name,
                       COUNT(*) FILTER (WHERE partition='val') as val_count,
                       COUNT(*) FILTER (WHERE partition='adversarial') as adv_count,
                       COUNT(*) as total
                FROM core_brain.test_suite
                WHERE is_active = TRUE
                GROUP BY skill_name
                ORDER BY total DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]


if __name__ == "__main__":
    print("MCUM SISL - Generador de Tests")
    print("-" * 50)

    skill = "mcum-orchestrator"
    print(f"\nGenerando tests para: {skill}...")
    result = generate_and_save(skill, max_tests=15)

    print(f"  Total generados : {result['total_generated']}")
    print(f"  Guardados en DB : {len(result['saved_ids'])}")
    print("  Breakdown:")
    for key, value in result["breakdown"].items():
        print(f"    {key:<15}: {value}")

    print("\nConteo de tests por skill:")
    counts = list_skill_test_counts()
    for row in counts:
        print(f"  {row['skill_name']:<30} val={row['val_count']}  adv={row['adv_count']}  total={row['total']}")

    print("\nTest generator operativo")
