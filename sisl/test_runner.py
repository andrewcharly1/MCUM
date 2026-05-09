"""
Skill evaluation for the MCUM SISL loop.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..db.connection import get_db, get_cursor
from ..db.embedder import cosine_similarity, embed
from ..db.experience_store import retrieve_for_task
from .test_generator import get_test_suite


@dataclass
class TestResult:
    test_id: str
    test_type: str
    partition: str
    difficulty: int
    passed: bool
    score: float
    reason: str
    response_preview: str = ""
    duration_ms: float = 0.0


@dataclass
class SkillEvalResult:
    skill_name: str
    skill_version: str
    eval_run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ckl_score: float = 0.0
    test_results: list[TestResult] = field(default_factory=list)
    val_pass: int = 0
    val_total: int = 0
    adv_pass: int = 0
    adv_total: int = 0
    duration_sec: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def val_score(self) -> float:
        return self.val_pass / self.val_total if self.val_total > 0 else 0.0

    @property
    def adv_score(self) -> float:
        return self.adv_pass / self.adv_total if self.adv_total > 0 else 0.0

    def summary(self) -> str:
        bar_len = int(self.ckl_score * 20)
        bar = "#" * bar_len + "-" * (20 - bar_len)
        return (
            f"  CKL Score: {self.ckl_score:.3f} [{bar}]\n"
            f"  Val:  {self.val_pass}/{self.val_total} ({self.val_score:.1%})\n"
            f"  Adv:  {self.adv_pass}/{self.adv_total} ({self.adv_score:.1%})\n"
            f"  Tiempo: {self.duration_sec:.1f}s"
        )


def _evaluate_factual(test: dict, context: dict) -> TestResult:
    start = time.time()
    input_query = test["input_query"]
    expected_result = test.get("expected_result") or ""
    source_exp_id = test.get("source_experience_id")

    retrieved = retrieve_for_task(input_query, skill_context=test.get("skill_name"))
    experiences = retrieved.get("experiences", [])

    source_found = any(str(exp.get("id", "")) == str(source_exp_id) for exp in experiences) if source_exp_id else False

    best_similarity = 0.0
    best_match = None
    if expected_result and experiences:
        expected_emb = embed(expected_result)
        for exp in experiences:
            content = exp.get("content", {})
            if isinstance(content, str):
                content = json.loads(content)
            conclusion = content.get("conclusion", "") if isinstance(content, dict) else ""
            if conclusion:
                conc_emb = embed(conclusion)
                similarity = cosine_similarity(expected_emb, conc_emb)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = conclusion[:100]

    if source_found:
        score = 0.50 + (best_similarity * 0.50)
    elif best_similarity > 0.60:
        score = 0.35 + (best_similarity * 0.40)
    elif best_similarity > 0.40:
        score = best_similarity * 0.60
    else:
        score = 0.0

    passed = score >= 0.50
    reason = (
        f"Source encontrada: {source_found} | Similitud: {best_similarity:.3f}"
        if experiences else "Sin experiences recuperadas - cold start"
    )

    return TestResult(
        test_id=str(test.get("id", uuid.uuid4())),
        test_type="factual_retrieval",
        partition=test.get("partition", "val"),
        difficulty=test.get("difficulty", 2),
        passed=passed,
        score=round(score, 4),
        reason=reason,
        response_preview=best_match or "(sin experience recuperada)",
        duration_ms=(time.time() - start) * 1000,
    )


def _evaluate_negative(test: dict, context: dict) -> TestResult:
    start = time.time()
    input_query = test["input_query"]
    retrieved = retrieve_for_task(input_query, skill_context=test.get("skill_name"))
    experiences = retrieved.get("experiences", [])

    not_app_signals = 0
    for exp in experiences:
        not_app = exp.get("not_applicable_cases")
        if isinstance(not_app, str):
            not_app = json.loads(not_app)
        when_not = not_app.get("when_not", "") if isinstance(not_app, dict) else ""
        if when_not and len(when_not) > 5:
            not_app_signals += 1

    if not_app_signals > 0:
        score = min(1.0, 0.60 + (not_app_signals * 0.20))
    elif experiences:
        score = 0.40
    else:
        score = 0.20

    passed = score >= 0.50
    reason = (
        f"NOT_APPLICABLE signals: {not_app_signals}/{len(experiences)} experiences "
        f"tienen not_applicable_cases definido"
    )

    return TestResult(
        test_id=str(test.get("id", uuid.uuid4())),
        test_type="negative_case",
        partition=test.get("partition", "val"),
        difficulty=test.get("difficulty", 3),
        passed=passed,
        score=round(score, 4),
        reason=reason,
        response_preview=f"{not_app_signals} signals detectados",
        duration_ms=(time.time() - start) * 1000,
    )


def _load_skill_document(skill_name: str) -> str:
    base = Path(__file__).resolve().parents[2]
    candidates = [
        base / skill_name / "SKILL.md",
        base / "MCUM" / "SKILL.md" if skill_name == "mcum-orchestrator" else base / "_missing",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").lower()
    return ""


def _keyword_tokens(text: str) -> set[str]:
    cleaned: list[str] = []
    for raw in (text or "").lower().replace("\n", " ").split():
        token = raw.strip(".,;:!?()[]{}\"'`")
        if len(token) > 4:
            cleaned.append(token)
    return set(cleaned)


def _evaluate_adversarial(test: dict, context: dict) -> TestResult:
    start = time.time()
    input_query = test["input_query"]
    expected_result = test.get("expected_result") or ""
    pass_condition = test.get("pass_condition") or ""
    skill_name = test.get("skill_name") or context.get("skill_name") or ""

    retrieved = retrieve_for_task(input_query, skill_context=skill_name or None)
    experiences = retrieved.get("experiences", [])
    failures = list(retrieved.get("failure_patterns", []))
    failures.extend(
        exp for exp in experiences
        if str(exp.get("category", "")) == "failure_pattern"
    )

    skill_doc = _load_skill_document(skill_name)
    expected_terms = _keyword_tokens(f"{expected_result} {pass_condition}")
    keyword_hits = sum(1 for term in expected_terms if term in skill_doc)

    score = 0.0
    if failures:
        score += 0.45
    if keyword_hits >= 3:
        score += 0.40
    elif keyword_hits == 2:
        score += 0.30
    elif keyword_hits == 1:
        score += 0.15
    if retrieved.get("warnings"):
        score += 0.10

    score = min(score, 1.0)
    passed = bool(failures) and keyword_hits >= 2 and score >= 0.55
    preview = ""
    if failures:
        content = failures[0].get("content", {})
        if isinstance(content, str):
            content = json.loads(content)
        if isinstance(content, dict):
            preview = content.get("conclusion", "")[:100]

    return TestResult(
        test_id=str(test.get("id", uuid.uuid4())),
        test_type=str(test.get("test_type", "conflict_resolution")),
        partition=test.get("partition", "adversarial"),
        difficulty=test.get("difficulty", 4),
        passed=passed,
        score=round(score, 4),
        reason=f"failure_signals={len(failures)} | skill_doc_keyword_hits={keyword_hits}",
        response_preview=preview or "(sin warning documentado)",
        duration_ms=(time.time() - start) * 1000,
    )


def run_evaluation(
    skill_name: str,
    skill_version: str = "1.0.0",
    partition: str | None = None,
    verbose: bool = True,
) -> SkillEvalResult:
    start_ts = time.time()
    result = SkillEvalResult(skill_name=skill_name, skill_version=skill_version)

    partitions = [partition] if partition else ["val", "adversarial"]
    all_tests: list[dict] = []
    for part in partitions:
        all_tests.extend(get_test_suite(skill_name, partition=part))

    if not all_tests:
        if verbose:
            print(f"  Warning: sin tests en DB para '{skill_name}'.")
        result.duration_sec = time.time() - start_ts
        return result

    if verbose:
        print(f"  Evaluando {len(all_tests)} tests...")

    for index, test in enumerate(all_tests, start=1):
        test_type = test.get("test_type", "factual_retrieval")
        part = test.get("partition", "val")

        if verbose:
            print(f"  [{index:02d}/{len(all_tests)}] {test_type:<20} [{part}] ...", end="", flush=True)

        if part == "adversarial":
            test_result = _evaluate_adversarial(test, {"skill_name": skill_name})
        elif test_type == "factual_retrieval":
            test_result = _evaluate_factual(test, {})
        else:
            test_result = _evaluate_negative(test, {})

        result.test_results.append(test_result)

        if part == "val":
            result.val_total += 1
            if test_result.passed:
                result.val_pass += 1
        elif part == "adversarial":
            result.adv_total += 1
            if test_result.passed:
                result.adv_pass += 1

        if verbose:
            icon = "OK" if test_result.passed else "FAIL"
            print(f" {icon} score={test_result.score:.3f} ({test_result.duration_ms:.0f}ms)")

    if result.val_total > 0 and result.adv_total > 0:
        result.ckl_score = (result.val_score * 0.70) + (result.adv_score * 0.30)
    elif result.val_total > 0:
        result.ckl_score = result.val_score
    elif result.adv_total > 0:
        result.ckl_score = result.adv_score
    else:
        result.ckl_score = 0.0

    result.duration_sec = time.time() - start_ts
    return result


def save_eval_to_db(eval_result: SkillEvalResult) -> str:
    version_id = str(uuid.uuid4())
    passed = sum(1 for item in eval_result.test_results if item.passed)
    total = len(eval_result.test_results)
    failures = [
        {"test_id": item.test_id, "reason": item.reason, "score": item.score}
        for item in eval_result.test_results
        if not item.passed
    ]

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.skill_versions (
                    id, skill_name, version_semver,
                    ckl_score, test_pass_count, test_total_count,
                    status, changes_description, diff_patch, improvement_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    version_id,
                    eval_result.skill_name,
                    eval_result.skill_version,
                    round(eval_result.ckl_score, 4),
                    passed,
                    total,
                    "testing",
                    f"Evaluacion SISL automatica. CKL={eval_result.ckl_score:.3f} ({passed}/{total})",
                    json.dumps(failures[:10]),
                    "sisl_loop",
                ),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else version_id


if __name__ == "__main__":
    print("MCUM SISL - Test Runner")
    print("-" * 50)

    skill = "mcum-orchestrator"
    print(f"\nEvaluando skill: {skill}\n")

    eval_result = run_evaluation(skill, verbose=True)

    print("\n" + ("-" * 50))
    print(f"CKL Score para '{skill}':")
    print(eval_result.summary())

    if eval_result.ckl_score > 0:
        eval_id = save_eval_to_db(eval_result)
        print(f"\nEvaluacion guardada en DB: {eval_id[:8]}...")
