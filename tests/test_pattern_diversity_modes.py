"""
Fase 2 - Tests del modo de diversidad de proyectos/skills en
build_pattern_candidates.

Verifica el contrato del parametro project_diversity_mode. Como el
bucketing actual separa candidatos por skill, el modo operativo por
defecto usa diversidad de contextos para relajar el gate global sin
convertirlo en un bypass.
"""
from __future__ import annotations

from MCUM.core import pattern_discovery
from MCUM.policy import load_pattern_policy


def _experience(
    index: int,
    project_id: str,
    *,
    skill_name: str = "mcum-orchestrator",
    category: str = "failure_pattern",
    confidence: float = 0.9,
) -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "category": category,
        "title": f"Retry timeout recovery {index}",
        "content": {"conclusion": "Retry bounded operations after timeout and validate the result."},
        "applicability": {"when": "A bounded worker times out."},
        "current_confidence": confidence,
        "unique_context_count": 1,
        "contradiction_penalty": 0.0,
        "conflict_refs": [],
        "project_id": project_id,
        "skill_name": skill_name,
        "task_description": f"recover timeout context {index}",
    }


def _embeddings(experiences: list[dict]) -> dict[str, list[float]]:
    return {experience["id"]: [1.0, 0.0, 0.0] for experience in experiences}


def _policy_with_mode(mode: str | None) -> dict:
    policy = load_pattern_policy()
    policy["quality_gates"]["project_diversity_mode"] = mode or "projects_only"
    return policy


def test_policy_default_includes_new_keys() -> None:
    """El policy debe declarar las nuevas llaves con defaults sensatos."""
    policy = load_pattern_policy()
    assert "project_diversity_mode" in policy["quality_gates"]
    assert "min_distinct_skills_global" in policy["quality_gates"]
    assert "min_distinct_contexts_global" in policy["quality_gates"]
    assert policy["quality_gates"]["project_diversity_mode"] in (
        "projects_only",
        "projects_or_skills",
        "projects_or_contexts",
        "projects_or_skills_or_contexts",
    )
    assert int(policy["quality_gates"]["min_distinct_skills_global"]) >= 2
    assert int(policy["quality_gates"]["min_distinct_contexts_global"]) >= 3


def test_metadata_records_diversity_mode_for_audit() -> None:
    """El metadata del candidato debe llevar project_diversity_mode y
    project_diversity_source para auditoria posterior, sin importar
    si la fuente es projects, skills o none."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
        _experience(3, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_skills"),
    )
    assert len(cands) == 1
    g = cands[0]["metadata"]["quality_gates"]
    assert g["project_diversity_mode"] == "projects_or_skills"
    # 2 proyectos distintos en el bucket => source=projects
    assert g["project_diversity_source"] == "projects"
    assert g["project_diversity"] is True


def test_single_project_candidate_records_source_none() -> None:
    """Cuando un candidato tiene 1 solo proyecto (caso comun en este
    workspace), project_diversity_source debe ser 'none' aunque el modo
    sea 'projects_or_skills' (porque dentro del bucket actual solo hay
    1 skill)."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(3, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_skills"),
    )
    assert len(cands) == 1
    g = cands[0]["metadata"]["quality_gates"]
    assert g["project_diversity_source"] == "none"
    assert g["project_diversity"] is False
    assert cands[0]["quality_ready"] is False


def test_projects_only_blocks_single_project() -> None:
    """Modo conservador: 1 proyecto, calidad bloqueada por project_gate."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(3, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_only"),
    )
    assert len(cands) == 1
    assert cands[0]["quality_ready"] is False
    g = cands[0]["metadata"]["quality_gates"]
    assert g["project_diversity_mode"] == "projects_only"
    assert g["project_diversity_source"] == "none"


def test_projects_or_contexts_accepts_single_project_with_distinct_contexts() -> None:
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(3, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_contexts"),
    )
    assert len(cands) == 1
    assert cands[0]["quality_ready"] is True
    gates = cands[0]["metadata"]["quality_gates"]
    assert gates["project_diversity_source"] == "contexts"
    assert gates["project_diversity"] is True


def test_projects_or_contexts_still_requires_global_context_threshold() -> None:
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(3, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
    ]
    experiences[1]["task_description"] = experiences[0]["task_description"]
    experiences[2]["task_description"] = experiences[0]["task_description"]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_contexts"),
    )
    assert len(cands) == 1
    assert cands[0]["quality_ready"] is False
    assert cands[0]["metadata"]["quality_gates"]["project_diversity_source"] == "none"


def test_low_average_confidence_blocks_quality_ready() -> None:
    experiences = [
        _experience(
            index,
            "10000000-0000-0000-0000-000000000001",
            skill_name="skill-a",
            confidence=0.60,
        )
        for index in range(1, 4)
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_contexts"),
    )
    assert len(cands) == 1
    assert cands[0]["quality_ready"] is False
    assert cands[0]["metadata"]["quality_gates"]["avg_confidence"] is False


def test_projects_only_accepts_multi_project() -> None:
    """Modo conservador: 2 proyectos => quality_ready=True via projects."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
        _experience(3, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_only"),
    )
    assert len(cands) == 1
    assert cands[0]["quality_ready"] is True
    assert cands[0]["metadata"]["quality_gates"]["project_diversity_source"] == "projects"


def test_project_scoped_run_records_explicit_scope() -> None:
    """Cuando se pasa project_id, la fuente es siempre 'explicit_scope'
    independientemente del modo configurado."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(3, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
    ]
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=_embeddings(experiences),
        policy=_policy_with_mode("projects_or_skills"),
        project_id="10000000-0000-0000-0000-000000000001",
    )
    assert len(cands) == 1
    g = cands[0]["metadata"]["quality_gates"]
    assert g["project_diversity_source"] == "explicit_scope"
    assert cands[0]["quality_ready"] is True


def test_other_gates_still_apply_when_diversity_relaxes() -> None:
    """Aunque project_gate pase, cohesion/context_diversity/open_conflicts
    deben seguir bloqueando si fallan - no se relaja la gobernanza."""
    experiences = [
        _experience(1, "10000000-0000-0000-0000-000000000001", skill_name="skill-a"),
        _experience(2, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
        _experience(3, "20000000-0000-0000-0000-000000000002", skill_name="skill-a"),
    ]
    # Embeddings ortogonales => no hay componente conectado con cohesion
    embeddings = {
        "00000000-0000-0000-0000-000000000001": [1.0, 0.0, 0.0],
        "00000000-0000-0000-0000-000000000002": [0.0, 1.0, 0.0],
        "00000000-0000-0000-0000-000000000003": [0.0, 0.0, 1.0],
    }
    cands, _ = pattern_discovery.build_pattern_candidates(
        experiences=experiences,
        embeddings=embeddings,
        policy=_policy_with_mode("projects_or_skills"),
    )
    # Sin cluster conectado, no hay candidatos
    assert all(not c["quality_ready"] for c in cands)
