"""Anti-vanity adoption scorecard for MCUM operational memory."""

from __future__ import annotations

import json
from typing import Any

from ..db.connection import get_cursor, get_db


def _ratio(numerator: Any, denominator: Any) -> float:
    total = int(denominator or 0)
    return round(int(numerator or 0) / total, 4) if total else 0.0


def assess_adoption(metrics: dict[str, Any]) -> dict[str, Any]:
    playbooks = dict(metrics.get("playbooks") or {})
    patterns = dict(metrics.get("patterns") or {})
    skills = dict(metrics.get("skills") or {})
    retrievals = dict(metrics.get("retrievals") or {})
    playbook_never_reused_ratio = _ratio(
        playbooks.get("never_reused"),
        playbooks.get("total"),
    )
    retrieval_failure_ratio = _ratio(
        retrievals.get("failure"),
        retrievals.get("total"),
    )
    return {
        "playbook_never_reused_ratio": playbook_never_reused_ratio,
        "playbook_target_met": playbook_never_reused_ratio <= 0.45,
        "patterns_eligible_for_health_decision": int(patterns.get("eligible_for_health") or 0),
        "patterns_observing": int(patterns.get("observing") or 0),
        "active_skills_without_evidence": int(skills.get("active_without_evidence") or 0),
        "retrieval_failure_ratio": retrieval_failure_ratio,
        "retrieval_failures": int(retrievals.get("failure") or 0),
        "recommendations": [
            *(
                ["review playbook ranking and consolidation in a paired shadow evaluation"]
                if playbook_never_reused_ratio > 0.45
                else []
            ),
            *(
                ["keep low-sample patterns observing until five real uses"]
                if int(patterns.get("observing") or 0) > 0
                else []
            ),
            *(
                ["review active skills without evidence; do not auto-demote from counters alone"]
                if int(skills.get("active_without_evidence") or 0) > 0
                else []
            ),
            *(
                ["analyze retrieval failure reasons before changing retrieval policy"]
                if int(retrievals.get("failure") or 0) > 0
                else []
            ),
        ],
    }


def build_adoption_scorecard() -> dict[str, Any]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE COALESCE(reuse_count, 0) = 0) AS never_reused,
                       COUNT(*) FILTER (WHERE COALESCE(reuse_count, 0) > 0) AS reused
                FROM core_brain.session_playbooks
                """
            )
            playbooks = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'active') AS active,
                       COUNT(*) FILTER (WHERE status = 'active' AND usage_count >= 5) AS eligible_for_health,
                       COUNT(*) FILTER (WHERE status = 'active' AND usage_count < 5) AS observing,
                       COALESCE(SUM(usage_count), 0) AS real_usage_events
                FROM core_brain.patterns
                """
            )
            patterns = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE status = 'active') AS active,
                       COUNT(*) FILTER (
                           WHERE status = 'active'
                             AND experience_count = 0
                             AND active_test_count = 0
                       ) AS active_without_evidence
                FROM project_registry.skill_catalog
                """
            )
            skills = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE outcome_status = 'success') AS success,
                       COUNT(*) FILTER (WHERE outcome_status = 'partial') AS partial,
                       COUNT(*) FILTER (WHERE outcome_status = 'failure') AS failure,
                       COUNT(*) FILTER (WHERE outcome_status IS NULL) AS unknown
                FROM core_brain.retrieval_runs
                """
            )
            retrievals = dict(cur.fetchone() or {})
    metrics = {
        "playbooks": playbooks,
        "patterns": patterns,
        "skills": skills,
        "retrievals": retrievals,
    }
    return {
        "status": "observing",
        "anti_vanity": {
            "playbook_reuse_requires_reuse_count": True,
            "pattern_health_min_real_uses": 5,
            "no_automatic_skill_demotion": True,
        },
        "metrics": metrics,
        "assessment": assess_adoption(metrics),
    }


def main() -> int:
    print(json.dumps(build_adoption_scorecard(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
