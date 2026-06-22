"""Governed artifact extraction with optional metadata, OCR and transcript adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .base import (
    AdapterPayload,
    ArtifactBudget,
    ArtifactSection,
    ExtractionDiagnostic,
    ExtractionResult,
    Provenance,
    sanitize_metadata,
    stable_id,
)
from .media_adapters import default_artifact_adapters


EXTRACTOR_VERSION = "artifact-v1"

TEXT_SUFFIXES = {".txt", ".log", ".csv", ".json", ".yaml", ".yml", ".xml"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
DEFAULT_ALLOWLIST = TEXT_SUFFIXES | MARKDOWN_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES | VIDEO_SUFFIXES


@dataclass
class ArtifactPolicy:
    allowlist: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWLIST))
    max_bytes: int = 10 * 1024 * 1024
    max_sections: int = 200
    max_section_chars: int = 100_000
    enable_ocr: bool = False
    enable_transcription: bool = False
    budget: ArtifactBudget = field(default_factory=ArtifactBudget)

    def __post_init__(self) -> None:
        self.allowlist = {
            suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
            for suffix in self.allowlist
        }
        self.max_bytes = max(1, int(self.max_bytes))
        self.max_sections = max(1, int(self.max_sections))
        self.max_section_chars = max(1, int(self.max_section_chars))


class ArtifactExtractor:
    """Extract text and optional media metadata under a restrictive policy."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: ArtifactPolicy | None = None,
        adapters: Mapping[str, Any] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy or ArtifactPolicy()
        self.adapters = {**default_artifact_adapters(), **dict(adapters or {})}
        self._seen_hashes: dict[str, str] = {}

    def extract(
        self,
        path: str | Path,
        *,
        enable_ocr: bool | None = None,
        enable_transcription: bool | None = None,
        budget: ArtifactBudget | None = None,
    ) -> ExtractionResult:
        resolved, relative_path, path_error = self._resolve_path(path)
        if path_error is not None:
            return ExtractionResult(source_path=relative_path, diagnostics=[path_error])

        assert resolved is not None
        suffix = resolved.suffix.lower()
        if suffix not in self.policy.allowlist:
            return ExtractionResult(
                source_path=relative_path,
                diagnostics=[
                    ExtractionDiagnostic(
                        code="artifact_type_not_allowed",
                        message="Artifact extension is not in the configured allowlist.",
                        severity="error",
                        recoverable=False,
                        metadata={"suffix": suffix},
                    )
                ],
                metadata={"suffix": suffix, "allowed": False},
            )

        size = resolved.stat().st_size
        if size > self.policy.max_bytes:
            return ExtractionResult(
                source_path=relative_path,
                diagnostics=[
                    ExtractionDiagnostic(
                        code="artifact_too_large",
                        message="Artifact exceeds the configured byte limit.",
                        severity="error",
                        recoverable=False,
                        metadata={"bytes": size, "max_bytes": self.policy.max_bytes},
                    )
                ],
                metadata={"bytes": size, "suffix": suffix},
            )

        content_hash = self._hash_file(resolved)
        duplicate_of = self._seen_hashes.get(content_hash)
        if duplicate_of is not None:
            return ExtractionResult(
                source_path=relative_path,
                content_hash=content_hash,
                diagnostics=[
                    ExtractionDiagnostic(
                        code="artifact_duplicate",
                        message="Artifact content was already extracted; sections were skipped.",
                        severity="info",
                        fallback="dedupe_skip",
                        metadata={"duplicate_of": duplicate_of},
                    )
                ],
                metadata={
                    "bytes": size,
                    "suffix": suffix,
                    "deduplicated": True,
                    "duplicate_of": duplicate_of,
                },
            )
        self._seen_hashes[content_hash] = relative_path

        kind = self._artifact_kind(suffix)
        active_budget = budget or self.policy.budget
        ocr_active = bool(self.policy.enable_ocr and enable_ocr is not False and active_budget.ocr_chars > 0)
        transcription_active = bool(
            self.policy.enable_transcription
            and enable_transcription is not False
            and active_budget.transcription_chars > 0
        )
        provenance = Provenance(
            source_path=relative_path,
            extractor="artifact",
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
            adapter="builtin" if kind in {"text", "markdown"} else self._adapter_name(kind),
            metadata={"artifact_kind": kind},
        )
        result = ExtractionResult(
            source_path=relative_path,
            content_hash=content_hash,
            metadata={
                "artifact_kind": kind,
                "bytes": size,
                "suffix": suffix,
                "deduplicated": False,
                "ocr_enabled": ocr_active,
                "transcription_enabled": transcription_active,
            },
        )

        if kind in {"text", "markdown"}:
            text = resolved.read_bytes().decode("utf-8", errors="replace")
            raw_sections = self._markdown_sections(text) if kind == "markdown" else [("text", text, {})]
            result.sections.extend(self._govern_sections(raw_sections, provenance, active_budget, result))
            return result

        adapter = self.adapters.get(kind)
        if adapter is None:
            result.diagnostics.append(
                ExtractionDiagnostic(
                    code="artifact_adapter_unavailable",
                    message="No optional adapter is configured; only governed file metadata was returned.",
                    fallback="metadata_only",
                    metadata={"artifact_kind": kind},
                )
            )
            result.metadata["fallback"] = "metadata_only"
            return result

        try:
            payload = self._call_adapter(
                adapter,
                resolved,
                enable_ocr=ocr_active,
                enable_transcription=transcription_active,
                budget=active_budget,
            )
        except Exception as exc:
            result.diagnostics.append(
                ExtractionDiagnostic(
                    code="artifact_adapter_failed",
                    message="Optional artifact adapter failed; only governed file metadata was returned.",
                    fallback="metadata_only",
                    metadata={"artifact_kind": kind, "error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            result.metadata["fallback"] = "metadata_only"
            return result

        result.metadata["adapter_metadata"] = sanitize_metadata(payload.metadata)
        result.diagnostics.extend(self._coerce_diagnostics(payload.diagnostics))
        raw_sections = self._coerce_sections(payload.sections)
        result.sections.extend(
            self._govern_sections(
                raw_sections,
                provenance,
                active_budget,
                result,
                enable_ocr=ocr_active,
                enable_transcription=transcription_active,
            )
        )
        return result

    def _resolve_path(
        self,
        path: str | Path,
    ) -> tuple[Path | None, str, ExtractionDiagnostic | None]:
        requested = Path(path)
        candidate = requested if requested.is_absolute() else self.root / requested
        display_path = requested.name
        try:
            resolved = candidate.resolve(strict=True)
            relative_path = resolved.relative_to(self.root).as_posix()
        except (FileNotFoundError, OSError, ValueError):
            return (
                None,
                display_path,
                ExtractionDiagnostic(
                    code="artifact_path_rejected",
                    message="Artifact path is missing or outside the configured root.",
                    severity="error",
                    recoverable=False,
                ),
            )
        if not resolved.is_file():
            return (
                None,
                relative_path,
                ExtractionDiagnostic(
                    code="artifact_not_file",
                    message="Artifact path does not identify a regular file.",
                    severity="error",
                    recoverable=False,
                ),
            )
        return resolved, relative_path, None

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _artifact_kind(suffix: str) -> str:
        if suffix in MARKDOWN_SUFFIXES:
            return "markdown"
        if suffix in TEXT_SUFFIXES:
            return "text"
        if suffix in PDF_SUFFIXES:
            return "pdf"
        if suffix in IMAGE_SUFFIXES:
            return "image"
        if suffix in VIDEO_SUFFIXES:
            return "video"
        return "unknown"

    def _adapter_name(self, kind: str) -> str | None:
        adapter = self.adapters.get(kind)
        if adapter is None:
            return None
        return getattr(adapter, "__name__", adapter.__class__.__name__)

    @staticmethod
    def _call_adapter(
        adapter: Any,
        path: Path,
        *,
        enable_ocr: bool,
        enable_transcription: bool,
        budget: ArtifactBudget,
    ) -> AdapterPayload:
        extract = getattr(adapter, "extract", adapter)
        payload = extract(
            path,
            enable_ocr=enable_ocr,
            enable_transcription=enable_transcription,
            budget=budget,
        )
        if isinstance(payload, AdapterPayload):
            return payload
        if isinstance(payload, Mapping):
            return AdapterPayload(
                metadata=dict(payload.get("metadata") or {}),
                sections=list(payload.get("sections") or []),
                diagnostics=list(payload.get("diagnostics") or []),
            )
        raise TypeError("Artifact adapter must return AdapterPayload or a mapping")

    @staticmethod
    def _markdown_sections(text: str) -> list[tuple[str, str, dict[str, Any]]]:
        sections: list[tuple[str, str, dict[str, Any]]] = []
        heading = "document"
        buffer: list[str] = []
        for line in text.splitlines():
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match:
                if buffer:
                    sections.append(("markdown", "\n".join(buffer).strip(), {"heading": heading}))
                heading = match.group(2)
                buffer = []
            else:
                buffer.append(line)
        if buffer or not sections:
            sections.append(("markdown", "\n".join(buffer).strip(), {"heading": heading}))
        return sections

    @staticmethod
    def _coerce_sections(
        sections: Iterable[ArtifactSection | Mapping[str, Any]],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        coerced: list[tuple[str, str, dict[str, Any]]] = []
        for section in sections:
            if isinstance(section, ArtifactSection):
                coerced.append((section.section_kind, section.text, dict(section.metadata)))
            elif isinstance(section, Mapping):
                raw_text = section.get("text") or ""
                if isinstance(raw_text, (bytes, bytearray, memoryview)):
                    text = f"[binary section omitted: {len(raw_text)} bytes]"
                else:
                    text = str(raw_text)
                coerced.append(
                    (
                        str(section.get("section_kind") or section.get("kind") or "text"),
                        text,
                        dict(section.get("metadata") or {}),
                    )
                )
        return coerced

    @staticmethod
    def _coerce_diagnostics(
        diagnostics: Iterable[ExtractionDiagnostic | Mapping[str, Any]],
    ) -> list[ExtractionDiagnostic]:
        coerced: list[ExtractionDiagnostic] = []
        for item in diagnostics:
            if isinstance(item, ExtractionDiagnostic):
                coerced.append(item)
            elif isinstance(item, Mapping):
                coerced.append(
                    ExtractionDiagnostic(
                        code=str(item.get("code") or "adapter_diagnostic"),
                        message=str(item.get("message") or "Artifact adapter diagnostic."),
                        severity=str(item.get("severity") or "warning"),
                        recoverable=bool(item.get("recoverable", True)),
                        fallback=str(item["fallback"]) if item.get("fallback") else None,
                        metadata=dict(item.get("metadata") or {}),
                    )
                )
        return coerced

    def _govern_sections(
        self,
        sections: Iterable[tuple[str, str, dict[str, Any]]],
        provenance: Provenance,
        budget: ArtifactBudget,
        result: ExtractionResult,
        *,
        enable_ocr: bool = False,
        enable_transcription: bool = False,
    ) -> list[ArtifactSection]:
        governed: list[ArtifactSection] = []
        remaining = {
            "ocr": budget.ocr_chars if enable_ocr else 0,
            "transcript": budget.transcription_chars if enable_transcription else 0,
            "transcription": budget.transcription_chars if enable_transcription else 0,
        }
        for section_kind, text, metadata in sections:
            normalized_kind = section_kind.lower()
            if normalized_kind == "ocr" and not enable_ocr:
                result.diagnostics.append(
                    ExtractionDiagnostic(
                        code="ocr_disabled",
                        message="Adapter OCR output was discarded because OCR is disabled or has no budget.",
                        severity="info",
                    )
                )
                continue
            if normalized_kind in {"transcript", "transcription"} and not enable_transcription:
                result.diagnostics.append(
                    ExtractionDiagnostic(
                        code="transcription_disabled",
                        message="Adapter transcript output was discarded because transcription is disabled or has no budget.",
                        severity="info",
                    )
                )
                continue
            if len(governed) >= self.policy.max_sections:
                result.diagnostics.append(
                    ExtractionDiagnostic(
                        code="artifact_section_limit",
                        message="Artifact section limit reached; remaining sections were skipped.",
                        fallback="partial_sections",
                        metadata={"max_sections": self.policy.max_sections},
                    )
                )
                break

            char_limit = self.policy.max_section_chars
            if normalized_kind in remaining:
                char_limit = min(char_limit, remaining[normalized_kind])
                if char_limit <= 0:
                    result.diagnostics.append(
                        ExtractionDiagnostic(
                            code=f"{normalized_kind}_budget_exhausted",
                            message="Artifact extraction budget was exhausted; remaining output was skipped.",
                            severity="info",
                        )
                    )
                    continue
            governed_text = text[:char_limit]
            if normalized_kind in remaining:
                remaining[normalized_kind] -= len(governed_text)
            if len(text) > len(governed_text):
                result.diagnostics.append(
                    ExtractionDiagnostic(
                        code="artifact_section_truncated",
                        message="Artifact section was truncated by the configured character budget.",
                        severity="info",
                        metadata={"section_kind": normalized_kind, "original_chars": len(text)},
                    )
                )
            ordinal = len(governed)
            governed.append(
                ArtifactSection(
                    section_id=stable_id(provenance.source_path, normalized_kind, ordinal, governed_text),
                    section_kind=normalized_kind,
                    text=governed_text,
                    provenance=provenance,
                    ordinal=ordinal,
                    confidence=0.8 if normalized_kind in {"ocr", "transcript", "transcription"} else 1.0,
                    metadata=metadata,
                )
            )
        return governed
