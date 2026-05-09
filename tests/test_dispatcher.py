from __future__ import annotations

import pytest

from MCUM.core import dispatcher


def _skill(
    name: str,
    *,
    triggers: list[str] | None = None,
    anti: list[str] | None = None,
    priority: int = 5,
) -> dict:
    return {
        "name": name,
        "file": name,
        "triggers": triggers or [],
        "anti": anti or [],
        "profile": f"profile for {name}",
        "priority": priority,
    }


def test_dispatch_respects_force_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [_skill("html-dashboard-expert"), _skill("kaizen")],
    )

    result = dispatcher.dispatch("tarea generica", force_skill="html-dashboard-expert")

    assert result.skill_name == "html-dashboard-expert"
    assert result.match_method == "forced_by_user"
    assert result.confidence == 1.0
    assert result.triggered_by == "user_override"


def test_dispatch_uses_single_trigger_without_semantic_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [
            _skill("html-dashboard-expert", triggers=["dashboard"], priority=8),
            _skill("kaizen", triggers=["kaizen"], priority=5),
        ],
    )

    def _unexpected_semantic_call(_: str) -> list[dict]:
        raise AssertionError("semantic ranking should not run when there is a single trigger match")

    monkeypatch.setattr(dispatcher, "_rank_by_semantics", _unexpected_semantic_call)

    result = dispatcher.dispatch("Necesito un dashboard ejecutivo para gerencia")

    assert result.skill_name == "html-dashboard-expert"
    assert result.match_method == "trigger_exact"
    assert result.triggered_by == "dashboard"
    assert result.confidence == dispatcher.TRIGGER_CONFIDENCE


def test_dispatch_uses_semantics_to_break_multi_trigger_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [
            _skill("backend-analyzer-coder", triggers=["api"], priority=7),
            _skill("nextjs-supabase-auth", triggers=["api"], priority=8),
            _skill("kaizen", triggers=["mejorar"], priority=5),
        ],
    )
    monkeypatch.setattr(
        dispatcher,
        "_rank_by_semantics",
        lambda _: [
            {"name": "nextjs-supabase-auth", "score": 0.82, "priority": 8},
            {"name": "backend-analyzer-coder", "score": 0.75, "priority": 7},
        ],
    )

    result = dispatcher.dispatch("Necesito una API con autenticacion y middleware")

    assert result.skill_name == "nextjs-supabase-auth"
    assert result.match_method == "trigger_multi_semantic_tiebreak"
    assert result.triggered_by == "api"
    assert result.semantic_score == 0.82
    assert result.alternatives == [{"name": "backend-analyzer-coder", "trigger": "api"}]
    assert any("Multi-trigger" in warning for warning in result.warnings)


def test_dispatch_returns_semantic_match_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [_skill("mcum-orchestrator"), _skill("kaizen")],
    )
    monkeypatch.setattr(
        dispatcher,
        "_rank_by_semantics",
        lambda _: [
            {"name": "mcum-orchestrator", "score": 0.41, "priority": 8},
            {"name": "kaizen", "score": 0.28, "priority": 5},
        ],
    )

    result = dispatcher.dispatch("Orquestar una validacion del skill MCUM")

    assert result.skill_name == "mcum-orchestrator"
    assert result.match_method == "semantic"
    assert result.semantic_score == 0.41
    assert result.confidence == pytest.approx(0.60 + (0.41 * 0.35))
    assert result.alternatives == [{"name": "kaizen", "score": 0.28, "priority": 5}]


def test_dispatch_falls_back_to_default_when_semantics_are_too_weak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranking = [
        {"name": "mcum-orchestrator", "score": 0.12, "priority": 8},
        {"name": "html-dashboard-expert", "score": 0.09, "priority": 8},
    ]
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [_skill("mcum-orchestrator"), _skill("html-dashboard-expert")],
    )
    monkeypatch.setattr(dispatcher, "_rank_by_semantics", lambda _: ranking)

    result = dispatcher.dispatch("Tarea difusa sin trigger claro")

    assert result.skill_name == dispatcher.DEFAULT_SKILL
    assert result.match_method == "default"
    assert result.triggered_by == "no_match_found"
    assert result.alternatives == ranking[:3]
    assert any("threshold" in warning for warning in result.warnings)


def test_dispatch_excludes_candidate_skills_from_automatic_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "filter_dispatchable_skills",
        lambda registry, include_candidates=False: [
            skill
            for skill in registry
            if skill["name"] != "candidate-skill" or include_candidates
        ],
    )
    monkeypatch.setattr(
        dispatcher,
        "_load_dynamic_skills",
        lambda: [
            _skill("candidate-skill", triggers=["candidate"], priority=9),
            _skill("mcum-orchestrator", triggers=["mcum"], priority=8),
        ],
    )
    monkeypatch.setattr(
        dispatcher,
        "_rank_by_semantics",
        lambda _: [{"name": "mcum-orchestrator", "score": 0.44, "priority": 8}],
    )

    automatic = dispatcher.dispatch("candidate workflow without force")
    forced = dispatcher.dispatch("candidate workflow with force", force_skill="candidate-skill")

    assert automatic.skill_name == "mcum-orchestrator"
    assert forced.skill_name == "candidate-skill"
    assert forced.match_method == "forced_by_user"


def test_dispatch_respects_learned_anti_triggers_and_priority_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [
            _skill("ui-ux-pro-max", triggers=["dashboard"], anti=["minera"], priority=6),
            _skill("html-dashboard-expert", triggers=["dashboard", "minera"], priority=9),
        ],
    )

    result = dispatcher.dispatch("Necesito un dashboard ejecutivo para flota minera")

    assert result.skill_name == "html-dashboard-expert"
    assert result.match_method == "trigger_exact"
    assert result.triggered_by == "dashboard"


def test_dispatch_does_not_match_substring_trigger_inside_another_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [
            _skill("go-industrial-expert", triggers=["gin"], priority=9),
            _skill("nextjs-supabase-auth", triggers=["login", "middleware"], priority=8),
        ],
    )

    result = dispatcher.dispatch("Implementar middleware y rutas protegidas para login")

    assert result.skill_name == "nextjs-supabase-auth"
    assert result.triggered_by in {"login", "middleware"}
