from __future__ import annotations

import pytest

from MCUM.sisl import test_generator


def _experience(index: int) -> dict:
    return {
        "id": f"exp-{index}",
        "title": f"Experience {index}",
        "content": {"conclusion": f"Conclusion {index}", "context": f"Context {index}"},
        "not_applicable_cases": {"when_not": f"When not {index}"},
    }


def _failure_pattern(index: int) -> dict:
    return {
        "id": f"fp-{index}",
        "title": f"Failure {index}",
        "task_description": f"Task {index}",
        "content": {"conclusion": f"Risk {index}"},
    }


@pytest.mark.parametrize(
    ("max_tests", "expected"),
    [
        (0, {"factual": 0, "negative": 0, "adversarial": 0}),
        (1, {"factual": 1, "negative": 0, "adversarial": 0}),
        (5, {"factual": 3, "negative": 1, "adversarial": 1}),
        (15, {"factual": 9, "negative": 3, "adversarial": 3}),
    ],
)
def test_allocate_test_budgets_is_exact(max_tests: int, expected: dict[str, int]) -> None:
    budgets = test_generator._allocate_test_budgets(max_tests)
    assert budgets == expected
    assert sum(budgets.values()) == max_tests


def test_generate_tests_for_skill_uses_full_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_search_by_keywords(*, keywords, skill_name=None, min_confidence=0.0, limit=10, **kwargs):
        if keywords:
            return [_experience(index) for index in range(limit)]
        return [_experience(index) for index in range(limit)]

    def fake_failure_patterns(*, query_text, min_confidence=0.0, limit=10, **kwargs):
        return [_failure_pattern(index) for index in range(limit)]

    monkeypatch.setattr(test_generator, "search_by_keywords", fake_search_by_keywords)
    monkeypatch.setattr(test_generator, "get_failure_patterns", fake_failure_patterns)
    monkeypatch.setattr(
        test_generator,
        "generate_adversarial_tests",
        lambda skill_name, limit=0, min_confidence=0.0: [_failure_pattern(index) | {"partition": "adversarial", "test_type": "conflict_resolution"} for index in range(limit)],
    )

    tests = test_generator.generate_tests_for_skill("mcum-orchestrator", max_tests=15)

    factual = [item for item in tests if item["test_type"] == "factual_retrieval"]
    negative = [item for item in tests if item["partition"] == "val" and item["test_type"] == "negative_case"]
    adversarial = [item for item in tests if item["partition"] == "adversarial"]

    assert len(tests) == 15
    assert len(factual) == 9
    assert len(negative) == 3
    assert len(adversarial) == 3
