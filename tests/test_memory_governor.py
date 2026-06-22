from __future__ import annotations

from MCUM.memory_governor import apply_memory_governor


def test_apply_memory_governor_assist_annotates_and_prioritizes_stronger_memory() -> None:
    items = [
        {
            "id": "exp-good",
            "project_id": "project-1",
            "title": "Validated wrapper recovery",
            "content": {"conclusion": "Use the validated wrapper recovery path."},
            "current_confidence": 0.91,
            "revalidation_count": 3,
            "unique_context_count": 2,
            "source_artifacts": [{"path": "workspace_session.py"}],
            "applicability": {"when": "Repairing the wrapper"},
            "_combined_score": 0.88,
        },
        {
            "id": "exp-noisy",
            "project_id": "project-2",
            "title": "Verbose low-signal note",
            "content": {"note": ("noise " * 320).strip()},
            "current_confidence": 0.34,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "_combined_score": 0.79,
        },
    ]

    kept, summary = apply_memory_governor(
        items,
        item_kind="experience",
        policy={"memory_governor": {"enabled": True, "mode": "assist"}},
        active_project_id="project-1",
        preserve_at_least=1,
    )

    assert [item["id"] for item in kept] == ["exp-good", "exp-noisy"]
    assert kept[0]["_memory_governor_state"] in {"hot", "warm"}
    assert kept[1]["_memory_governor_state"] in {"cold", "quarantined"}
    assert kept[0]["_memory_governor_score"] > kept[1]["_memory_governor_score"]
    assert summary["filtered_count"] == 0
    assert summary["states"]["hot"] + summary["states"]["warm"] >= 1


def test_apply_memory_governor_soft_filter_uses_fallback_if_filtering_would_empty_results() -> None:
    items = [
        {
            "id": "exp-risky",
            "project_id": "project-9",
            "title": "Unvalidated verbose memory",
            "content": {"note": ("noise " * 420).strip()},
            "current_confidence": 0.22,
            "revalidation_count": 0,
            "unique_context_count": 0,
            "_combined_score": 0.31,
        }
    ]

    kept, summary = apply_memory_governor(
        items,
        item_kind="experience",
        policy={"memory_governor": {"enabled": True, "mode": "soft_filter"}},
        active_project_id="project-1",
        preserve_at_least=1,
    )

    assert [item["id"] for item in kept] == ["exp-risky"]
    assert summary["filtered_count"] == 1
    assert summary["fallback_applied"] is True
    assert any("fallback" in warning.lower() for warning in summary["warnings"])


def test_apply_memory_governor_assist_can_adaptively_filter_local_quarantine_pressure() -> None:
    items = [
        {
            "id": "pb-good-1",
            "project_id": "project-1",
            "title": "Validated wrapper guide",
            "output_summary": "Apply the validated wrapper fix.",
            "validation_summary": "Validated with smoke tests.",
            "confidence_score": 0.9,
            "reuse_count": 2,
            "_compactness_score": 0.9,
            "_combined_score": 0.88,
        },
        {
            "id": "pb-good-2",
            "project_id": "project-1",
            "title": "Compact validator guide",
            "output_summary": "Run validator and confirm fix.",
            "validation_summary": "Validated with tests.",
            "confidence_score": 0.87,
            "reuse_count": 2,
            "_compactness_score": 0.85,
            "_combined_score": 0.84,
        },
        {
            "id": "pb-noisy-1",
            "project_id": "project-2",
            "title": "Verbose weak guide",
            "output_summary": ("noise " * 220).strip(),
            "validation_summary": "",
            "confidence_score": 0.18,
            "reuse_count": 0,
            "_compactness_score": 0.1,
            "_combined_score": 0.62,
        },
        {
            "id": "pb-noisy-2",
            "project_id": "project-2",
            "title": "Another verbose weak guide",
            "output_summary": ("noise " * 220).strip(),
            "validation_summary": "",
            "confidence_score": 0.16,
            "reuse_count": 0,
            "_compactness_score": 0.08,
            "_combined_score": 0.61,
        },
    ]

    kept, summary = apply_memory_governor(
        items,
        item_kind="playbook",
        policy={"memory_governor": {"enabled": True, "mode": "assist"}},
        active_project_id="project-1",
        preserve_at_least=1,
    )

    assert [item["id"] for item in kept] == ["pb-good-1", "pb-good-2"]
    assert summary["adaptive_filter_applied"] is True
    assert summary["adaptive_filter_reason"] == "local_quarantine_pressure"
    assert summary["effective_mode"] == "assist_plus_local_filter"
    assert summary["filtered_count"] == 2
    assert any("adaptively filtered" in warning.lower() for warning in summary["warnings"])
