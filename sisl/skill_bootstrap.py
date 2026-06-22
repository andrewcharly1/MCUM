"""
Bootstrap cold-start skills from their local SKILL.md documents.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db.experience_store import save_experience
from .test_generator import generate_and_save


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CASE_HEADING_RE = re.compile(r"^#{3,6}\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillSection:
    title: str
    normalized: str
    level: int
    content: str


def resolve_skill_doc_path(skill_name: str) -> Path | None:
    base = Path(__file__).resolve().parents[2]
    candidates = [
        base / skill_name / "SKILL.md",
        base / "MCUM" / "SKILL.md" if skill_name == "mcum-orchestrator" else base / "_missing",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _clean_inline(text: str) -> str:
    cleaned = re.sub(r"`+", "", text or "")
    cleaned = re.sub(r"\*\*|__", "", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _frontmatter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return 0


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    start = _frontmatter_end(lines)
    if start == 0:
        return {}

    metadata: dict[str, str] = {}
    index = 1
    while index < start - 1:
        line = lines[index]
        if ":" not in line or line.startswith(" "):
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "|":
            index += 1
            block: list[str] = []
            while index < start - 1 and (lines[index].startswith(" ") or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            metadata[key] = _clean_inline(" ".join(part for part in block if part))
            continue
        metadata[key] = value.strip("\"'")
        index += 1
    return metadata


def _parse_sections(text: str) -> list[SkillSection]:
    lines = text.splitlines()
    start_index = _frontmatter_end(lines)
    headings: list[tuple[int, int, str]] = []
    for index in range(start_index, len(lines)):
        match = HEADING_RE.match(lines[index])
        if match:
            headings.append((index, len(match.group(1)), _clean_inline(match.group(2))))

    sections: list[SkillSection] = []
    for pos, (line_index, level, title) in enumerate(headings):
        end_index = len(lines)
        for next_line, next_level, _ in headings[pos + 1 :]:
            if next_level <= level:
                end_index = next_line
                break
        content = "\n".join(lines[line_index + 1 : end_index]).strip()
        sections.append(
            SkillSection(
                title=title,
                normalized=_normalize_text(title),
                level=level,
                content=content,
            )
        )
    return sections


def _match_section(sections: list[SkillSection], aliases: list[str]) -> SkillSection | None:
    best: SkillSection | None = None
    best_score = 0
    for alias in aliases:
        alias_norm = _normalize_text(alias)
        if not alias_norm:
            continue
        alias_tokens = set(alias_norm.split())
        for section in sections:
            score = 0
            if section.normalized == alias_norm:
                score = 6
            elif alias_norm in section.normalized:
                score = 5
            elif section.normalized in alias_norm:
                score = 4
            else:
                overlap = len(alias_tokens & set(section.normalized.split()))
                if overlap:
                    score = 2 + overlap
            if score > best_score:
                best = section
                best_score = score
    return best


def _extract_bullets(content: str, limit: int = 8) -> list[str]:
    bullets: list[str] = []
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        bullet = ""
        if line.startswith(("- ", "* ")):
            bullet = line[2:].strip()
        else:
            numbered = re.match(r"^\d+\.\s+(.*)$", line)
            if numbered:
                bullet = numbered.group(1).strip()
        bullet = _clean_inline(bullet)
        if bullet and bullet not in bullets:
            bullets.append(bullet)
        if len(bullets) >= limit:
            break
    return bullets


def _extract_guardrails_from_rules(rule_bullets: list[str]) -> list[str]:
    guardrails: list[str] = []
    for bullet in rule_bullets:
        lowered = _normalize_text(bullet)
        if any(token in lowered for token in ("prohibido", "no ", "nunca", "avoid", "evitar", "panic")):
            if bullet not in guardrails:
                guardrails.append(bullet)
    return guardrails


def _extract_case_blocks(text: str) -> list[dict[str, str]]:
    matches = list(CASE_HEADING_RE.finditer(text))
    cases: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        heading = _clean_inline(match.group(1))
        normalized = _normalize_text(heading)
        if not any(token in normalized for token in ("caso", "case", "escenario")):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[start:end]

        input_match = re.search(r"(?im)^\s*-\s*\*\*(input|escenario)\*\*:\s*(.+)$", block)
        output_match = re.search(
            r"(?im)^\s*-\s*\*\*(output esperado|comportamiento esperado)\*\*:\s*(.+)$",
            block,
        )
        case_type = "valid"
        if any(token in normalized for token in ("invalido", "invalid", "borde", "edge")):
            case_type = "negative"

        cases.append(
            {
                "heading": heading,
                "type": case_type,
                "input": _clean_inline(input_match.group(2)) if input_match else _clean_inline(heading),
                "expected": _clean_inline(output_match.group(2)) if output_match else _clean_inline(block[:240]),
            }
        )
    return cases


def _first_nonempty(*values: str) -> str:
    for value in values:
        cleaned = _clean_inline(value)
        if cleaned:
            return cleaned
    return ""


def derive_bootstrap_payload(skill_name: str, skill_md_path: str | None = None) -> dict[str, Any]:
    path = Path(skill_md_path) if skill_md_path else resolve_skill_doc_path(skill_name)
    if path is None or not path.exists():
        raise FileNotFoundError(f"SKILL.md not found for {skill_name}")

    text = path.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(text)
    sections = _parse_sections(text)
    cases = _extract_case_blocks(text)

    overview = _match_section(sections, ["objetivo", "overview", "proposito", "purpose"])
    use_when = _match_section(sections, ["use when", "when to apply", "capabilities", "capacidades"])
    requirements = _match_section(
        sections,
        ["input contract", "requisitos", "requirements", "prerequisites", "campos obligatorios"],
    )
    rules = _match_section(sections, ["directivas deterministas", "instrucciones y estandares", "workflow", "principios fundamentales"])
    guardrails = _match_section(sections, ["anti-patrones", "anti patterns", "do not use when", "boundaries", "contingencia"])
    patterns = _match_section(sections, ["golden dataset", "patrones", "quick reference", "rule categories"])

    description = _first_nonempty(frontmatter.get("description", ""), overview.content if overview else "")
    use_when_bullets = _extract_bullets(use_when.content if use_when else "", limit=6)
    requirement_bullets = _extract_bullets(requirements.content if requirements else "", limit=6)
    rule_bullets = _extract_bullets(rules.content if rules else "", limit=6)
    guardrail_bullets = _extract_bullets(guardrails.content if guardrails else "", limit=6)
    pattern_bullets = _extract_bullets(patterns.content if patterns else "", limit=6)

    if not guardrail_bullets:
        guardrail_bullets = _extract_guardrails_from_rules(rule_bullets)[:4]
    if not guardrail_bullets:
        negative_cases = [case["input"] for case in cases if case["type"] == "negative"]
        guardrail_bullets = negative_cases[:4]

    experiences: list[dict[str, Any]] = []
    skill_version = frontmatter.get("skill_version")
    common_not_app = {
        "when_not": "; ".join(guardrail_bullets[:3]) or "When the task violates the documented boundaries of the skill.",
    }

    core_conclusion = _first_nonempty(
        description,
        ". ".join(rule_bullets[:3]),
        ". ".join(use_when_bullets[:3]),
    )
    if core_conclusion:
        experiences.append(
            {
                "category": "implementation_recipe",
                "title": f"{skill_name} bootstrap core profile",
                "content": {
                    "conclusion": core_conclusion,
                    "context": f"Bootstrap seeded from {path.name}.",
                },
                "task_description": description or skill_name,
                "applicability": {
                    "when": "; ".join(use_when_bullets[:3]) or f"When the task clearly matches {skill_name}.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.76,
                "skill_version": skill_version,
            }
        )

    if requirement_bullets:
        experiences.append(
            {
                "category": "evaluation_policy",
                "title": f"{skill_name} bootstrap input contract",
                "content": {
                    "conclusion": "Validate required inputs before proceeding: " + "; ".join(requirement_bullets[:4]),
                    "context": "Input contract inferred from SKILL.md requirements.",
                },
                "task_description": f"Validate inputs before using {skill_name}",
                "applicability": {
                    "when": "Before executing the skill on a new request.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.74,
                "skill_version": skill_version,
            }
        )

    if guardrail_bullets:
        experiences.append(
            {
                "category": "failure_pattern",
                "title": f"{skill_name} bootstrap guardrails",
                "content": {
                    "conclusion": "Do not proceed blindly. Guardrails: " + "; ".join(guardrail_bullets[:4]),
                    "context": "Failure and anti-pattern guidance extracted from SKILL.md.",
                },
                "task_description": "; ".join(guardrail_bullets[:2]) or f"Invalid use of {skill_name}",
                "applicability": {
                    "when": f"When a task superficially resembles {skill_name} but may violate its boundaries.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.73,
                "skill_version": skill_version,
            }
        )

    valid_cases = [case for case in cases if case["type"] == "valid"]
    if valid_cases:
        first_case = valid_cases[0]
        experiences.append(
            {
                "category": "implementation_recipe",
                "title": f"{skill_name} bootstrap canonical example",
                "content": {
                    "conclusion": first_case["expected"],
                    "context": first_case["input"],
                },
                "task_description": first_case["input"],
                "applicability": {
                    "when": first_case["input"],
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.75,
                "skill_version": skill_version,
            }
        )
    elif pattern_bullets:
        experiences.append(
            {
                "category": "testing_strategy",
                "title": f"{skill_name} bootstrap patterns",
                "content": {
                    "conclusion": "Key patterns to cover: " + "; ".join(pattern_bullets[:4]),
                    "context": "Pattern list derived from SKILL.md.",
                },
                "task_description": f"Pattern coverage for {skill_name}",
                "applicability": {
                    "when": f"When testing or applying {skill_name} in a new context.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.72,
                "skill_version": skill_version,
            }
        )

    if len(experiences) < 3 and rule_bullets:
        experiences.append(
            {
                "category": "testing_strategy",
                "title": f"{skill_name} bootstrap operational checklist",
                "content": {
                    "conclusion": "Operational checklist: " + "; ".join(rule_bullets[:4]),
                    "context": "Checklist synthesized from deterministic rules in SKILL.md.",
                },
                "task_description": f"Checklist before applying {skill_name}",
                "applicability": {
                    "when": f"Before implementing or reviewing work with {skill_name}.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.72,
                "skill_version": skill_version,
            }
        )

    if len(experiences) < 3 and use_when_bullets:
        experiences.append(
            {
                "category": "prompting_heuristic",
                "title": f"{skill_name} bootstrap routing hints",
                "content": {
                    "conclusion": "Prefer this skill for: " + "; ".join(use_when_bullets[:4]),
                    "context": "Bootstrap routing hints extracted from SKILL.md.",
                },
                "task_description": f"Routing hints for {skill_name}",
                "applicability": {
                    "when": "; ".join(use_when_bullets[:3]),
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.71,
                "skill_version": skill_version,
            }
        )

    if len(experiences) < 3:
        experiences.append(
            {
                "category": "prompting_heuristic",
                "title": f"{skill_name} bootstrap fallback routing hints",
                "content": {
                    "conclusion": "Prefer this skill when the request aligns with its documented specialization and constraints.",
                    "context": "Fallback bootstrap hint generated from SKILL.md summary.",
                },
                "task_description": f"Fallback routing hints for {skill_name}",
                "applicability": {
                    "when": description or f"When the task matches {skill_name}.",
                },
                "not_applicable_cases": common_not_app,
                "initial_score": 0.70,
                "skill_version": skill_version,
            }
        )

    return {
        "skill_name": skill_name,
        "skill_md_path": str(path),
        "skill_version": skill_version,
        "frontmatter": frontmatter,
        "experiences": experiences[:5],
        "cases": cases[:6],
    }


def bootstrap_skill_from_doc(
    skill_name: str,
    *,
    project_id: str | None = None,
    skill_md_path: str | None = None,
    max_tests: int = 8,
) -> dict[str, Any]:
    payload = derive_bootstrap_payload(skill_name, skill_md_path=skill_md_path)
    inserted_ids: list[str] = []
    for experience in payload["experiences"]:
        inserted_ids.append(
            save_experience(
                category=experience["category"],
                title=experience["title"],
                content=experience["content"],
                skill_name=skill_name,
                project_id=project_id,
                task_description=experience["task_description"],
                applicability=experience["applicability"],
                not_applicable_cases=experience["not_applicable_cases"],
                review_notes="Synthetic bootstrap generated from SKILL.md to break cold start.",
                initial_score=float(experience["initial_score"]),
                tested_by="bootstrap",
                skill_version=experience.get("skill_version"),
                is_synthetic=True,
            )
        )

    tests = generate_and_save(skill_name, max_tests=max_tests)
    return {
        "skill_name": skill_name,
        "skill_md_path": payload["skill_md_path"],
        "skill_version": payload.get("skill_version"),
        "experience_ids": inserted_ids,
        "experiences_seeded": len(inserted_ids),
        "tests_generated": tests["total_generated"],
        "tests_saved": len(tests["saved_ids"]),
        "test_breakdown": tests["breakdown"],
        "cases_detected": len(payload["cases"]),
    }
