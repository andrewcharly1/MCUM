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

    def _unexpected_semantic_call(*args, **kwargs) -> list[dict]:
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
        lambda *args, **kwargs: [
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
        lambda *args, **kwargs: [
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


def test_dispatch_prefers_prior_successful_skill_when_anti_loop_bias_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [_skill("mcum-orchestrator", priority=8), _skill("validator-skill", priority=6)],
    )
    monkeypatch.setattr(
        dispatcher,
        "_rank_by_semantics",
        lambda *args, **kwargs: [
            {"name": "mcum-orchestrator", "score": 0.45, "semantic_score": 0.45, "priority": 8},
            {"name": "validator-skill", "score": 0.41, "semantic_score": 0.41, "priority": 6},
        ],
    )

    result = dispatcher.dispatch(
        "Reintentar una tarea repetidamente fallida con ruta alternativa",
        dispatch_hints={
            "enabled": True,
            "loop_risk": 0.78,
            "recommendation": "switch_strategy_before_retry",
            "warning_risk_threshold": 0.35,
            "preferred_skills": ["validator-skill"],
            "preferred_score_boost": 0.08,
            "preferred_priority_boost": 0.5,
        },
    )

    assert result.skill_name == "validator-skill"
    assert result.match_method == "semantic"
    assert result.semantic_score == 0.41
    assert any("Anti-loop dispatch bias" in warning for warning in result.warnings)


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
    monkeypatch.setattr(dispatcher, "_rank_by_semantics", lambda *args, **kwargs: ranking)

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
        lambda *args, **kwargs: [{"name": "mcum-orchestrator", "score": 0.44, "priority": 8}],
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


def test_load_dynamic_skills_prefers_frontmatter_routing_over_legacy_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "discover_local_skills",
        lambda: [
            {
                "skill_name": "html-dashboard-expert",
                "skill_dir_name": "html-dashboard-expert",
                "description": "Dashboards HTML puros",
                "metadata": {
                    "routing": {
                        "enabled": True,
                        "triggers": ["dashboard", "html puro"],
                        "anti": ["react"],
                        "priority": 11,
                        "profile": "Frontmatter profile",
                        "has_explicit_routing": True,
                    }
                },
            }
        ],
    )

    loaded = dispatcher._load_dynamic_skills()

    assert loaded[0]["name"] == "html-dashboard-expert"
    assert loaded[0]["triggers"] == ["dashboard", "html puro"]
    assert loaded[0]["anti"] == ["react"]
    assert loaded[0]["priority"] == 11
    assert loaded[0]["profile"] == "Frontmatter profile"
    assert loaded[0]["routing_source"] == "frontmatter"


def test_load_dynamic_skills_no_longer_uses_legacy_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatcher, "discover_local_skills", lambda: [])

    loaded = dispatcher._load_dynamic_skills()

    assert loaded == []


def test_rank_by_semantics_applies_dispatch_learning_adjustments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dispatcher,
        "_get_skill_registry",
        lambda **kwargs: [
            _skill("ui-ux-pro-max", priority=7),
            _skill("html-dashboard-expert", priority=8),
        ],
    )

    embedding_map = {
        "Crear dashboard ejecutivo de flota": [1.0, 0.0],
        "profile for ui-ux-pro-max": [0.79, 0.0],
        "profile for html-dashboard-expert": [0.78, 0.0],
    }

    monkeypatch.setattr(dispatcher, "embed", lambda text: embedding_map[text])
    monkeypatch.setattr(
        dispatcher,
        "cosine_similarity",
        lambda left, right: round(sum(a * b for a, b in zip(left, right)), 4),
    )

    ranking = dispatcher._rank_by_semantics(
        "Crear dashboard ejecutivo de flota",
        dispatch_learning_profile={
            "active": True,
            "priority_adjustments": {
                "html-dashboard-expert": 0.9,
                "ui-ux-pro-max": -0.8,
            },
            "score_adjustments": {
                "html-dashboard-expert": 0.03,
                "ui-ux-pro-max": -0.03,
            },
        },
    )

    assert ranking[0]["name"] == "html-dashboard-expert"
    assert ranking[0]["semantic_score"] == 0.78
    assert ranking[0]["score"] > ranking[1]["score"]
    assert ranking[0]["priority_delta"] > 0
