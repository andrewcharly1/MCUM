from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from MCUM.memory_freshness import (
    _normalize_path,
    apply_dispatch_hint_freshness,
    apply_memory_freshness,
    build_project_structure_snapshots,
    build_source_snapshots,
    utc_now,
    utc_now_iso,
)


def test_apply_memory_freshness_penalizes_changed_and_old_evidence(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1", encoding="utf-8")
    stale_snapshot = build_source_snapshots([str(tracked)])
    tracked.write_text("v2-updated", encoding="utf-8")
    fresh_snapshot = build_source_snapshots([str(tracked)])

    items = [
        {
            "id": "exp-stale",
            "title": "Old auth recipe",
            "skill_name": "nextjs-supabase-auth",
            "skill_version": "1.0.0",
            "last_validated_at": (utc_now() - timedelta(days=150)).isoformat(),
            "current_confidence": 0.9,
            "_combined_score": 0.9,
            "source_artifacts": stale_snapshot,
        },
        {
            "id": "exp-fresh",
            "title": "Fresh auth recipe",
            "skill_name": "nextjs-supabase-auth",
            "skill_version": "2.0.0",
            "last_validated_at": utc_now_iso(),
            "current_confidence": 0.8,
            "_combined_score": 0.8,
            "source_artifacts": fresh_snapshot,
        },
    ]

    enriched = apply_memory_freshness(
        items,
        kind="experience",
        current_skill_version="2.0.0",
        score_key="_combined_score",
    )

    assert [item["id"] for item in enriched] == ["exp-fresh", "exp-stale"]
    assert enriched[0]["_freshness_state"] == "fresh"
    assert enriched[1]["_freshness_state"] in {"stale", "invalidated"}
    assert enriched[1]["_combined_score"] < enriched[1]["_base_score"]
    assert any("skill version drift" in reason for reason in enriched[1]["_freshness_reasons"])
    assert any("path " in reason for reason in enriched[1]["_freshness_reasons"])


def test_normalize_path_falls_back_when_resolve_fails(monkeypatch) -> None:
    def fail_resolve(self, strict=False):
        raise OSError("inaccessible path")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    normalized = _normalize_path(r"\\wsl.localhost\\Ubuntu-24.04\\missing")
    assert normalized.endswith("/wsl.localhost/Ubuntu-24.04/missing")


def test_apply_dispatch_hint_freshness_ages_priority_delta() -> None:
    fresh = apply_dispatch_hint_freshness(
        {
            "triggers": ["dashboard"],
            "priority_delta": 2,
            "updated_at": utc_now_iso(),
        }
    )
    stale = apply_dispatch_hint_freshness(
        {
            "triggers": ["dashboard"],
            "priority_delta": 2,
            "updated_at": (utc_now() - timedelta(days=120)).isoformat(),
        }
    )

    assert fresh["priority_delta"] == 2
    assert fresh["_freshness_state"] == "fresh"
    assert stale["priority_delta"] < 2
    assert stale["_freshness_state"] in {"aging", "stale"}
    assert stale["_freshness_score"] < fresh["_freshness_score"]


def test_project_structure_snapshots_detect_manifest_drift(tmp_path: Path) -> None:
    package_json = tmp_path / "package.json"
    schema_sql = tmp_path / "db" / "schema.sql"
    schema_sql.parent.mkdir(parents=True, exist_ok=True)
    package_json.write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
    schema_sql.write_text("create table demo(id int);", encoding="utf-8")

    snapshots = build_project_structure_snapshots(str(tmp_path))
    assert any(entry["snapshot_type"] == "project_structure" for entry in snapshots)
    assert any(entry.get("role") == "package_manifest" for entry in snapshots)

    package_json.write_text('{"name":"demo","version":"2.0.0"}', encoding="utf-8")
    enriched = apply_memory_freshness(
        [
            {
                "id": "exp-struct",
                "title": "Project scaffold recipe",
                "skill_name": "nextjs-supabase-auth",
                "skill_version": "2.0.0",
                "last_validated_at": utc_now_iso(),
                "_combined_score": 0.85,
                "source_artifacts": snapshots,
            }
        ],
        kind="experience",
        current_skill_version="2.0.0",
        score_key="_combined_score",
    )

    assert enriched[0]["_freshness_state"] in {"aging", "stale", "invalidated"}
    assert any("project structure content changed" in reason for reason in enriched[0]["_freshness_reasons"])
