"""
Structured writeback helpers for SISL candidate patches with backup and rollback.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SISL_WRITEBACK_START = "<!-- MCUM_SISL_WRITEBACK_START:"
SISL_WRITEBACK_END = "<!-- MCUM_SISL_WRITEBACK_END:"
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")

SECTION_RULES = {
    "workflow": {
        "default_title": "Workflow",
        "aliases": [
            "workflow",
            "operating rules",
            "directivas deterministas",
            "protocolo de ejecucion",
            "rules",
            "reglas",
        ],
    },
    "guardrails": {
        "default_title": "Do Not Use When",
        "aliases": [
            "do not use when",
            "boundaries",
            "not applicable",
            "input contract",
            "strict intake gate",
            "need info",
        ],
    },
    "coverage": {
        "default_title": "Use When",
        "aliases": [
            "use when",
            "objective",
            "objetivo",
            "output contract",
            "golden dataset",
            "examples",
            "key outputs",
            "casos de uso",
            "trigger keywords",
        ],
    },
}

PROPOSAL_SECTION_MAP = {
    "add_failure_warning": "workflow",
    "fix_pass_condition": "workflow",
    "add_not_applicable": "guardrails",
    "add_case": "coverage",
    "cold_start_coverage": "coverage",
    "clarify_trigger": "coverage",
    "add_example": "coverage",
}

PROPOSAL_LABELS = {
    "add_case": "Coverage",
    "add_not_applicable": "Guardrail",
    "add_failure_warning": "Warning",
    "clarify_trigger": "Trigger",
    "add_example": "Example",
    "fix_pass_condition": "Pass condition",
    "cold_start_coverage": "Cold start",
}


@dataclass(frozen=True)
class SectionRef:
    title: str
    normalized: str
    level: int
    start_line: int
    end_line: int


def resolve_skill_md_path(skill_name: str) -> Path | None:
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


def _normalize_section_key(text: str) -> str:
    normalized = _normalize_text(text)
    return normalized.replace(" ", "-")


def _frontmatter_end_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return 0


def _parse_sections(text: str) -> list[SectionRef]:
    lines = text.splitlines(keepends=True)
    start_index = _frontmatter_end_index(lines)
    headings: list[tuple[int, int, str]] = []
    for index in range(start_index, len(lines)):
        match = HEADING_RE.match(lines[index].rstrip("\n"))
        if not match:
            continue
        headings.append((index, len(match.group(1)), match.group(2).strip()))

    sections: list[SectionRef] = []
    for index, (start_line, level, title) in enumerate(headings):
        end_line = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        sections.append(
            SectionRef(
                title=title,
                normalized=_normalize_text(title),
                level=level,
                start_line=start_line,
                end_line=end_line,
            )
        )
    return sections


def _find_best_section(sections: list[SectionRef], aliases: Iterable[str]) -> SectionRef | None:
    best_match: SectionRef | None = None
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
                best_match = section
                best_score = score
    return best_match


def _proposal_section_rule(proposal) -> dict:
    section_key = PROPOSAL_SECTION_MAP.get(proposal.improvement_type, "workflow")
    return SECTION_RULES.get(section_key, SECTION_RULES["workflow"]) | {"key": section_key}


def _resolve_targets(text: str, proposals) -> dict[str, dict]:
    sections = _parse_sections(text)
    grouped: dict[str, dict] = {}

    for proposal in proposals:
        rule = _proposal_section_rule(proposal)
        aliases = [proposal.section_to_edit] if proposal.section_to_edit else []
        aliases.extend(rule["aliases"])
        match = _find_best_section(sections, aliases)
        if match:
            section_id = _normalize_section_key(match.title)
            title = match.title
            matched_existing = True
        else:
            section_id = rule["key"]
            title = rule["default_title"]
            matched_existing = False

        bucket = grouped.setdefault(
            section_id,
            {
                "section_id": section_id,
                "title": title,
                "matched_existing": matched_existing,
                "proposals": [],
            },
        )
        bucket["proposals"].append(proposal)
        bucket["matched_existing"] = bucket["matched_existing"] or matched_existing
    return grouped


def _marker_start(section_id: str) -> str:
    return f"{SISL_WRITEBACK_START}{section_id} -->"


def _marker_end(section_id: str) -> str:
    return f"{SISL_WRITEBACK_END}{section_id} -->"


def _proposal_lines(proposal) -> list[str]:
    label = PROPOSAL_LABELS.get(proposal.improvement_type, proposal.improvement_type.replace("_", " ").title())
    lines = [f"- {label}: {proposal.proposed_text}", f"  Evidence: {proposal.evidence}"]
    if proposal.test_ids_failing:
        lines.append(f"  Failing tests: {', '.join(proposal.test_ids_failing[:5])}")
    lines.append(f"  Confidence: {proposal.confidence:.2f}")
    return lines


def build_candidate_block(skill_name: str, proposals, *, section_id: str, section_title: str) -> str:
    lines = [
        _marker_start(section_id),
        "### MCUM SISL Candidate Writeback",
        "",
        f"Target section: {section_title}",
        f"Skill: {skill_name}",
        "This block is managed by MCUM SISL candidate writeback.",
        "",
    ]
    for proposal in proposals:
        lines.extend(_proposal_lines(proposal))
        lines.append("")
    lines.append(_marker_end(section_id))
    return "\n".join(lines)


def _replace_existing_block(text: str, section_id: str, block: str) -> tuple[str, str] | None:
    start_marker = _marker_start(section_id)
    end_marker = _marker_end(section_id)
    if start_marker not in text or end_marker not in text:
        return None

    start_index = text.index(start_marker)
    end_index = text.index(end_marker) + len(end_marker)
    updated = text[:start_index].rstrip() + "\n\n" + block + "\n" + text[end_index:].lstrip("\n")
    return updated, "replace"


def _insert_into_existing_section(text: str, section_title: str, block: str) -> tuple[str, str] | None:
    sections = _parse_sections(text)
    match = _find_best_section(sections, [section_title])
    if match is None:
        return None

    lines = text.splitlines(keepends=True)
    block_text = block.strip() + "\n\n"
    lines.insert(match.end_line, block_text)
    return "".join(lines), "insert_section"


def _append_new_section(text: str, section_title: str, block: str) -> tuple[str, str]:
    heading = f"## {section_title}"
    updated = text.rstrip() + "\n\n" + heading + "\n\n" + block.strip() + "\n"
    return updated, "create_section"


def apply_sisl_proposals(skill_name: str, proposals, skill_md_path: str | None = None) -> dict:
    target_path = Path(skill_md_path) if skill_md_path else resolve_skill_md_path(skill_name)
    if target_path is None or not target_path.exists():
        return {
            "applied": False,
            "note": "SKILL.md no encontrado para writeback SISL.",
        }

    proposal_list = list(proposals)
    if not proposal_list:
        return {
            "applied": False,
            "note": "No hay propuestas SISL para aplicar.",
        }

    original = target_path.read_text(encoding="utf-8")
    backup_path = target_path.with_suffix(target_path.suffix + ".bak")
    backup_path.write_text(original, encoding="utf-8")

    updated = original
    section_results: list[dict] = []
    grouped_targets = _resolve_targets(original, proposal_list)
    for section_id, entry in grouped_targets.items():
        block = build_candidate_block(
            skill_name,
            entry["proposals"],
            section_id=section_id,
            section_title=entry["title"],
        )
        result = _replace_existing_block(updated, section_id, block)
        if result is None and entry["matched_existing"]:
            result = _insert_into_existing_section(updated, entry["title"], block)
        if result is None:
            result = _append_new_section(updated, entry["title"], block)

        updated, mode = result
        section_results.append(
            {
                "section_id": section_id,
                "section_title": entry["title"],
                "mode": mode,
                "proposal_count": len(entry["proposals"]),
            }
        )

    target_path.write_text(updated, encoding="utf-8")
    return {
        "applied": True,
        "path": str(target_path),
        "backup_path": str(backup_path),
        "mode": "structured",
        "applied_count": len(proposal_list),
        "sections": section_results,
    }


def rollback_sisl_writeback(skill_md_path: str, backup_path: str | None = None) -> bool:
    target_path = Path(skill_md_path)
    resolved_backup = Path(backup_path) if backup_path else target_path.with_suffix(target_path.suffix + ".bak")
    if not target_path.exists() or not resolved_backup.exists():
        return False

    target_path.write_text(resolved_backup.read_text(encoding="utf-8"), encoding="utf-8")
    return True
