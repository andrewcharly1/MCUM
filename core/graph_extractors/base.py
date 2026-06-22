"""Common contracts and helpers for governed graph extractors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def stable_id(*parts: object) -> str:
    """Return a deterministic compact identifier for extractor entities."""
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:24]


def sanitize_value(value: Any) -> JSONValue:
    """Convert untrusted adapter output to JSON-safe data without binary payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[binary omitted: {len(value)} bytes]"
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_value(item) for item in value]
    return str(value)


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, JSONValue]:
    return {
        str(key): sanitize_value(value)
        for key, value in dict(metadata or {}).items()
    }


@dataclass
class Provenance:
    source_path: str
    extractor: str
    extractor_version: str
    content_hash: str | None = None
    adapter: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = sanitize_metadata(self.metadata)


@dataclass
class GraphNode:
    node_id: str
    node_kind: str
    name: str
    qualified_name: str
    provenance: Provenance
    line_start: int | None = None
    line_end: int | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    confidence: float = 1.0
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.metadata = sanitize_metadata(self.metadata)


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    edge_kind: str
    provenance: Provenance
    confidence: float = 1.0
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.metadata = sanitize_metadata(self.metadata)


@dataclass
class ExtractionDiagnostic:
    code: str
    message: str
    severity: str = "warning"
    recoverable: bool = True
    fallback: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = sanitize_metadata(self.metadata)


@dataclass
class ArtifactSection:
    section_id: str
    section_kind: str
    text: str
    provenance: Provenance
    ordinal: int = 0
    confidence: float = 1.0
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.text, (bytes, bytearray, memoryview)):
            self.text = f"[binary section omitted: {len(self.text)} bytes]"
        elif not isinstance(self.text, str):
            self.text = str(self.text)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.metadata = sanitize_metadata(self.metadata)


@dataclass
class ExtractionResult:
    source_path: str
    content_hash: str | None = None
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    sections: list[ArtifactSection] = field(default_factory=list)
    diagnostics: list[ExtractionDiagnostic] = field(default_factory=list)
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = sanitize_metadata(self.metadata)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" and not item.recoverable for item in self.diagnostics)

    def to_dict(self) -> dict[str, JSONValue]:
        return sanitize_value(asdict(self))  # type: ignore[return-value]


@dataclass
class ArtifactBudget:
    ocr_chars: int = 0
    transcription_chars: int = 0

    def __post_init__(self) -> None:
        self.ocr_chars = max(0, int(self.ocr_chars))
        self.transcription_chars = max(0, int(self.transcription_chars))


@dataclass
class AdapterPayload:
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    sections: list[ArtifactSection | Mapping[str, Any]] = field(default_factory=list)
    diagnostics: list[ExtractionDiagnostic | Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.metadata = sanitize_metadata(self.metadata)


@runtime_checkable
class ArtifactAdapter(Protocol):
    def extract(
        self,
        path: Path,
        *,
        enable_ocr: bool,
        enable_transcription: bool,
        budget: ArtifactBudget,
    ) -> AdapterPayload | Mapping[str, Any]:
        """Extract governed metadata and text sections from an artifact."""
