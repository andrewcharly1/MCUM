"""Seed and backfill taxonomy metadata for the governed knowledge library."""

from __future__ import annotations

import json
import importlib.util
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .connection import get_cursor, get_db

_TAXONOMY_MODULE_PATH = Path(__file__).resolve().parent.parent / "core" / "knowledge_library_taxonomy.py"
_TAXONOMY_SPEC = importlib.util.spec_from_file_location(
    "mcum_live_knowledge_library_taxonomy_backfill",
    _TAXONOMY_MODULE_PATH,
)
if _TAXONOMY_SPEC is None or _TAXONOMY_SPEC.loader is None:
    raise ImportError(f"Unable to load taxonomy helpers from {_TAXONOMY_MODULE_PATH}")
_TAXONOMY_MODULE = importlib.util.module_from_spec(_TAXONOMY_SPEC)
sys.modules[_TAXONOMY_SPEC.name] = _TAXONOMY_MODULE
_TAXONOMY_SPEC.loader.exec_module(_TAXONOMY_MODULE)

iter_concept_definitions = _TAXONOMY_MODULE.iter_concept_definitions
iter_methodology_definitions = _TAXONOMY_MODULE.iter_methodology_definitions
normalize_text = _TAXONOMY_MODULE.normalize_text
score_concept_matches = _TAXONOMY_MODULE.score_concept_matches
score_methodology_matches = _TAXONOMY_MODULE.score_methodology_matches


@dataclass(slots=True)
class TaxonomyBackfillReport:
    documents_scanned: int = 0
    documents_reclassified: int = 0
    methodologies_seeded: int = 0
    concepts_seeded: int = 0
    document_methodologies_linked: int = 0
    document_concepts_linked: int = 0
    section_concepts_linked: int = 0
    chunk_concepts_linked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _authority_for_document(document: dict[str, Any]) -> str:
    repository = str(document.get("source_repository") or "").strip() or "LOCAL_PDFS"
    title = normalize_text(document.get("title"))
    source_path = normalize_text(document.get("source_path"))

    if repository == "LOCAL_PDFS":
        if "pmbok" in title or "agilepracticeguide" in title or "agile practice guide" in title:
            return "canonical"
        if "rita" in title or "exam prep" in title:
            return "secondary"
        if any(marker in title for marker in ["faq", "memory", "fundamental", "examtopics", "mapping processes"]):
            return "secondary"
        return "primary"
    if repository == "sap-ddd-knowledgebase":
        return "secondary"
    if repository == "team-topologies-community-materials":
        return "community"
    if repository == "devops-roadmap":
        return "community"
    if repository == "alysivji-notes":
        return "secondary"
    if repository == "software-engineer-library":
        return "community"
    if "github" in source_path:
        return "community"
    return "internal"


def _document_text(document: dict[str, Any]) -> str:
    parts = [
        document.get("title"),
        document.get("subtitle"),
        document.get("description"),
        document.get("notes"),
        document.get("author"),
        document.get("publisher"),
        document.get("edition"),
        document.get("source_repository"),
        document.get("source_path"),
    ]
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _section_text(section: dict[str, Any]) -> str:
    parts = [section.get("heading"), section.get("section_path"), section.get("section_type")]
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _chunk_text(chunk: dict[str, Any]) -> str:
    parts = [chunk.get("summary_excerpt"), chunk.get("content")]
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _latest_completed_documents(cur: Any) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            d.id AS document_id,
            d.title,
            d.subtitle,
            d.description,
            d.notes,
            d.author,
            d.publisher,
            d.edition,
            d.source_path,
            COALESCE(d.source_repository, CASE WHEN d.source_kind = 'pdf' THEN 'LOCAL_PDFS' END) AS source_repository,
            d.authority_tier::text AS authority_tier,
            dv.id AS document_version_id
        FROM knowledge_library.documents d
        JOIN LATERAL (
            SELECT id
            FROM knowledge_library.document_versions
            WHERE document_id = d.id
              AND ingestion_status = 'completed'
            ORDER BY COALESCE(ingested_at, finished_at, created_at) DESC, created_at DESC
            LIMIT 1
        ) dv ON TRUE
        WHERE d.status <> 'disabled'
        ORDER BY d.title ASC
        """
    )
    return list(cur.fetchall())


def _sections_by_version(cur: Any) -> dict[str, list[dict[str, Any]]]:
    cur.execute(
        """
        SELECT
            id AS section_id,
            document_version_id,
            heading,
            section_path,
            section_type
        FROM knowledge_library.sections
        ORDER BY document_version_id, section_order, section_level, id
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        grouped.setdefault(str(row["document_version_id"]), []).append(row)
    return grouped


def _chunks_by_version(cur: Any) -> dict[str, list[dict[str, Any]]]:
    cur.execute(
        """
        SELECT
            id AS chunk_id,
            document_version_id,
            section_id,
            summary_excerpt,
            content
        FROM knowledge_library.chunks
        ORDER BY document_version_id, chunk_order, id
        """
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        grouped.setdefault(str(row["document_version_id"]), []).append(row)
    return grouped


def _seed_methodologies(cur: Any) -> dict[str, str]:
    methodology_ids: dict[str, str] = {}
    for definition in iter_methodology_definitions():
        notes = json.dumps({"repositories": definition["repositories"], "lenses": definition["lenses"]})
        cur.execute(
            """
            INSERT INTO knowledge_library.methodologies (
                methodology_slug,
                name,
                description,
                authority_tier,
                notes
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (methodology_slug)
            DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                authority_tier = EXCLUDED.authority_tier,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            RETURNING id
            """,
            (
                definition["slug"],
                definition["name"],
                definition["description"],
                definition["authority_tier"],
                notes,
            ),
        )
        methodology_ids[definition["slug"]] = str(cur.fetchone()["id"])
    return methodology_ids


def _seed_concepts(cur: Any) -> dict[str, str]:
    concept_ids: dict[str, str] = {}
    for definition in iter_concept_definitions():
        cur.execute(
            """
            INSERT INTO knowledge_library.concepts (
                concept_slug,
                concept_name,
                concept_type,
                description,
                aliases,
                authority_tier,
                notes
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (concept_slug)
            DO UPDATE SET
                concept_name = EXCLUDED.concept_name,
                concept_type = EXCLUDED.concept_type,
                description = EXCLUDED.description,
                aliases = EXCLUDED.aliases,
                authority_tier = EXCLUDED.authority_tier,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            RETURNING id
            """,
            (
                definition["slug"],
                definition["name"],
                definition["concept_type"],
                definition["description"],
                json.dumps(definition["aliases"]),
                definition["authority_tier"],
                f"methodology={definition['methodology_slug']}",
            ),
        )
        concept_ids[definition["slug"]] = str(cur.fetchone()["id"])
    return concept_ids


def backfill_knowledge_library_taxonomy(*, clear_existing_links: bool = True) -> dict[str, Any]:
    """Populate methodologies, concepts, and association tables for live retrieval."""

    report = TaxonomyBackfillReport()

    with get_db() as conn:
        with get_cursor(conn) as cur:
            if clear_existing_links:
                cur.execute("DELETE FROM knowledge_library.chunk_concepts")
                cur.execute("DELETE FROM knowledge_library.section_concepts")
                cur.execute("DELETE FROM knowledge_library.document_concepts")
                cur.execute("DELETE FROM knowledge_library.document_methodologies")

            cur.execute(
                """
                UPDATE knowledge_library.documents
                SET source_repository = 'LOCAL_PDFS',
                    updated_at = NOW()
                WHERE source_repository IS NULL
                  AND source_kind = 'pdf'
                """
            )

            methodology_ids = _seed_methodologies(cur)
            concept_ids = _seed_concepts(cur)
            report.methodologies_seeded = len(methodology_ids)
            report.concepts_seeded = len(concept_ids)

            documents = _latest_completed_documents(cur)
            sections_by_version = _sections_by_version(cur)
            chunks_by_version = _chunks_by_version(cur)

            for document in documents:
                report.documents_scanned += 1
                document_id = str(document["document_id"])
                version_id = str(document["document_version_id"])
                source_repository = str(document.get("source_repository") or "").strip() or "LOCAL_PDFS"
                text = _document_text(document)
                authority_tier = _authority_for_document(document)
                if authority_tier != str(document.get("authority_tier") or ""):
                    cur.execute(
                        """
                        UPDATE knowledge_library.documents
                        SET authority_tier = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (authority_tier, document_id),
                    )
                    report.documents_reclassified += 1

                methodology_matches = score_methodology_matches(text, source_repository=source_repository)
                ranked_methodologies = sorted(
                    methodology_matches.items(),
                    key=lambda item: item[1].get("score") or 0.0,
                    reverse=True,
                )
                top_methodologies = [slug for slug, _match in ranked_methodologies[:2]]
                for methodology_slug, match in ranked_methodologies:
                    if float(match.get("score") or 0.0) < 0.26:
                        continue
                    methodology_id = methodology_ids.get(methodology_slug)
                    if not methodology_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO knowledge_library.document_methodologies (
                            document_id,
                            methodology_id,
                            relevance_score,
                            notes
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id, methodology_id)
                        DO UPDATE SET
                            relevance_score = EXCLUDED.relevance_score,
                            notes = EXCLUDED.notes,
                            created_at = NOW()
                        """,
                        (
                            document_id,
                            methodology_id,
                            float(match.get("score") or 0.0),
                            json.dumps({"matched_terms": list(match.get("matched_terms") or [])}),
                        ),
                    )
                    report.document_methodologies_linked += 1

                document_concepts = score_concept_matches(text, methodology_slugs=top_methodologies)
                for concept_slug, match in document_concepts.items():
                    concept_id = concept_ids.get(concept_slug)
                    if not concept_id or float(match.get("score") or 0.0) < 0.3:
                        continue
                    cur.execute(
                        """
                        INSERT INTO knowledge_library.document_concepts (
                            document_id,
                            concept_id,
                            relevance_score,
                            notes
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id, concept_id)
                        DO UPDATE SET
                            relevance_score = EXCLUDED.relevance_score,
                            notes = EXCLUDED.notes,
                            created_at = NOW()
                        """,
                        (
                            document_id,
                            concept_id,
                            float(match.get("score") or 0.0),
                            json.dumps({"matched_terms": list(match.get("matched_terms") or [])}),
                        ),
                    )
                    report.document_concepts_linked += 1

                for section in sections_by_version.get(version_id, []):
                    section_text = _section_text(section)
                    if not section_text:
                        continue
                    section_matches = score_concept_matches(section_text, methodology_slugs=top_methodologies)
                    for concept_slug, match in section_matches.items():
                        concept_id = concept_ids.get(concept_slug)
                        if not concept_id or float(match.get("score") or 0.0) < 0.32:
                            continue
                        cur.execute(
                            """
                            INSERT INTO knowledge_library.section_concepts (
                                section_id,
                                concept_id,
                                relevance_score,
                                notes
                            )
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (section_id, concept_id)
                            DO UPDATE SET
                                relevance_score = EXCLUDED.relevance_score,
                                notes = EXCLUDED.notes,
                                created_at = NOW()
                            """,
                            (
                                str(section["section_id"]),
                                concept_id,
                                float(match.get("score") or 0.0),
                                json.dumps({"matched_terms": list(match.get("matched_terms") or [])}),
                            ),
                        )
                        report.section_concepts_linked += 1

                for chunk in chunks_by_version.get(version_id, []):
                    chunk_text = _chunk_text(chunk)
                    if not chunk_text:
                        continue
                    chunk_matches = score_concept_matches(chunk_text, methodology_slugs=top_methodologies)
                    for concept_slug, match in chunk_matches.items():
                        concept_id = concept_ids.get(concept_slug)
                        if not concept_id or float(match.get("score") or 0.0) < 0.34:
                            continue
                        cur.execute(
                            """
                            INSERT INTO knowledge_library.chunk_concepts (
                                chunk_id,
                                concept_id,
                                relevance_score,
                                notes
                            )
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (chunk_id, concept_id)
                            DO UPDATE SET
                                relevance_score = EXCLUDED.relevance_score,
                                notes = EXCLUDED.notes,
                                created_at = NOW()
                            """,
                            (
                                str(chunk["chunk_id"]),
                                concept_id,
                                float(match.get("score") or 0.0),
                                json.dumps({"matched_terms": list(match.get("matched_terms") or [])}),
                            ),
                        )
                        report.chunk_concepts_linked += 1

    return report.to_dict()
