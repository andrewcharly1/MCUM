"""Optional Tree-sitter graph extraction with a deterministic fallback."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from typing import Any, Iterable, Mapping

from .base import (
    ExtractionDiagnostic,
    ExtractionResult,
    GraphEdge,
    GraphNode,
    Provenance,
    stable_id,
)


EXTRACTOR_VERSION = "tree-sitter-v1"

DEFAULT_SYMBOL_TYPES: dict[str, str] = {
    "class_definition": "class",
    "class_declaration": "class",
    "interface_declaration": "interface",
    "struct_item": "struct",
    "enum_item": "enum",
    "function_definition": "function",
    "function_declaration": "function",
    "function_item": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "constructor_declaration": "constructor",
}


def _value(target: Any, name: str, default: Any = None) -> Any:
    value = getattr(target, name, default)
    return value() if callable(value) else value


def _node_type(node: Any) -> str:
    return str(_value(node, "type", None) or _value(node, "kind", ""))


def _children(node: Any) -> list[Any]:
    children = getattr(node, "children", None)
    if children is not None and not callable(children):
        return list(children or ())
    count = int(_value(node, "child_count", 0) or 0)
    child = getattr(node, "child", None)
    return [child(index) for index in range(count)] if callable(child) else []


class TreeSitterExtractor:
    """Extract symbol containment edges using Tree-sitter when available."""

    def __init__(
        self,
        language: str,
        *,
        parser: Any | None = None,
        grammar: Any | None = None,
        symbol_types: Mapping[str, str] | None = None,
        max_nodes: int = 10_000,
    ) -> None:
        self.language = language
        self._injected_parser = parser
        self.grammar = grammar
        self.symbol_types = dict(symbol_types or DEFAULT_SYMBOL_TYPES)
        self.max_nodes = max(1, int(max_nodes))

    @staticmethod
    def library_available() -> bool:
        try:
            return importlib.util.find_spec("tree_sitter") is not None
        except (ImportError, ValueError):
            return False

    @staticmethod
    def language_pack_available() -> bool:
        try:
            return importlib.util.find_spec("tree_sitter_language_pack") is not None
        except (ImportError, ValueError):
            return False

    def availability(self) -> dict[str, Any]:
        if self._injected_parser is not None:
            return {"available": True, "source": "injected_parser", "reason": None}
        if self.language_pack_available():
            return {"available": True, "source": "tree_sitter_language_pack", "reason": None}
        if not self.library_available():
            return {"available": False, "source": "optional_dependency", "reason": "tree_sitter_missing"}
        if self.grammar is None:
            return {"available": False, "source": "optional_dependency", "reason": "grammar_missing"}
        return {"available": True, "source": "optional_dependency", "reason": None}

    def extract(self, source: str | bytes, source_path: str) -> ExtractionResult:
        data = source if isinstance(source, bytes) else source.encode("utf-8")
        content_hash = hashlib.sha256(data).hexdigest()
        availability = self.availability()
        parser_source = str(availability["source"])
        provenance = Provenance(
            source_path=source_path,
            extractor="tree_sitter",
            extractor_version=EXTRACTOR_VERSION,
            content_hash=content_hash,
            adapter=parser_source,
            metadata={"language": self.language},
        )
        file_id = stable_id(source_path, "file")
        file_node = GraphNode(
            node_id=file_id,
            node_kind="file",
            name=source_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            qualified_name=source_path,
            provenance=provenance,
            byte_start=0,
            byte_end=len(data),
            confidence=1.0 if availability["available"] else 0.45,
            metadata={"language": self.language, "fallback": not availability["available"]},
        )
        result = ExtractionResult(
            source_path=source_path,
            content_hash=content_hash,
            nodes=[file_node],
            metadata={
                "language": self.language,
                "availability": availability,
                "fallback": None if availability["available"] else "file_only",
            },
        )
        if not availability["available"]:
            result.diagnostics.append(
                ExtractionDiagnostic(
                    code=str(availability["reason"]),
                    message="Tree-sitter extraction is unavailable; returned the governed file-only fallback.",
                    fallback="file_only",
                    metadata={"language": self.language},
                )
            )
            return result

        try:
            parser = self._injected_parser or self._build_parser()
            parse_input: str | bytes = (
                data.decode("utf-8", errors="replace")
                if parser_source == "tree_sitter_language_pack"
                else data
            )
            tree = parser.parse(parse_input)
            root = _value(tree, "root_node")
        except Exception as exc:
            result.metadata["fallback"] = "file_only"
            result.nodes[0].confidence = 0.45
            result.nodes[0].metadata["fallback"] = True
            result.diagnostics.append(
                ExtractionDiagnostic(
                    code="tree_sitter_parse_failed",
                    message="Tree-sitter parsing failed; returned the governed file-only fallback.",
                    fallback="file_only",
                    metadata={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            return result

        if bool(_value(root, "has_error", False)):
            result.diagnostics.append(
                ExtractionDiagnostic(
                    code="tree_sitter_syntax_error",
                    message="Tree-sitter returned a tree containing syntax errors.",
                    severity="info",
                    metadata={"language": self.language},
                )
            )

        symbol_parent: dict[int, str] = {}
        for node, nearest_parent_id in self._walk(root, file_id, data, source_path):
            raw_type = _node_type(node)
            node_kind = self.symbol_types.get(raw_type)
            if node_kind is None:
                symbol_parent[id(node)] = nearest_parent_id
                continue
            if len(result.nodes) >= self.max_nodes:
                result.diagnostics.append(
                    ExtractionDiagnostic(
                        code="tree_sitter_node_limit",
                        message="Tree-sitter node limit reached; remaining symbols were skipped.",
                        fallback="partial_tree",
                        metadata={"max_nodes": self.max_nodes},
                    )
                )
                break

            name = self._node_name(node, data) or f"<anonymous:{raw_type}>"
            start_byte = int(_value(node, "start_byte", 0) or 0)
            end_byte = int(_value(node, "end_byte", start_byte) or start_byte)
            node_id = stable_id(source_path, raw_type, start_byte, end_byte, name)
            graph_node = GraphNode(
                node_id=node_id,
                node_kind=node_kind,
                name=name,
                qualified_name=f"{source_path}:{name}",
                provenance=provenance,
                line_start=self._line_number(_value(node, "start_point", None) or _value(node, "start_position", None)),
                line_end=self._line_number(_value(node, "end_point", None) or _value(node, "end_position", None)),
                byte_start=start_byte,
                byte_end=end_byte,
                confidence=0.95,
                metadata={"tree_sitter_type": raw_type, "language": self.language},
            )
            result.nodes.append(graph_node)
            result.edges.append(
                GraphEdge(
                    source_id=nearest_parent_id,
                    target_id=node_id,
                    edge_kind="contains",
                    provenance=provenance,
                    confidence=0.95,
                )
            )
            symbol_parent[id(node)] = node_id

        result.metadata["symbols_extracted"] = len(result.nodes) - 1
        result.metadata["fallback"] = None
        return result

    def _build_parser(self) -> Any:
        if self.language_pack_available():
            language_pack = importlib.import_module("tree_sitter_language_pack")
            return language_pack.get_parser(self.language)
        module = importlib.import_module("tree_sitter")
        parser_type = module.Parser
        try:
            return parser_type(self.grammar)
        except TypeError:
            parser = parser_type()
            if hasattr(parser, "set_language"):
                parser.set_language(self.grammar)
            else:
                parser.language = self.grammar
            return parser

    def _walk(
        self,
        root: Any,
        file_id: str,
        data: bytes,
        source_path: str,
    ) -> Iterable[tuple[Any, str]]:
        stack: list[tuple[Any, str]] = [(root, file_id)]
        while stack:
            node, nearest_parent_id = stack.pop()
            yield node, nearest_parent_id
            child_parent = self._symbol_id_for_walk(node, nearest_parent_id, data, source_path)
            children = _children(node)
            for child in reversed(children):
                stack.append((child, child_parent))

    def _symbol_id_for_walk(
        self,
        node: Any,
        fallback: str,
        data: bytes,
        source_path: str,
    ) -> str:
        raw_type = _node_type(node)
        if raw_type not in self.symbol_types:
            return fallback
        start_byte = int(_value(node, "start_byte", 0) or 0)
        end_byte = int(_value(node, "end_byte", start_byte) or start_byte)
        return stable_id(
            source_path,
            raw_type,
            start_byte,
            end_byte,
            self._node_name(node, data),
        )

    @staticmethod
    def _node_name(node: Any, data: bytes) -> str | None:
        name_node = None
        child_by_field_name = getattr(node, "child_by_field_name", None)
        if callable(child_by_field_name):
            name_node = child_by_field_name("name")
        if name_node is None:
            return None
        text = _value(name_node, "text", None)
        if isinstance(text, bytes):
            return text.decode("utf-8", errors="replace").strip()
        if isinstance(text, str):
            return text.strip()
        if data:
            start = int(_value(name_node, "start_byte", 0) or 0)
            end = int(_value(name_node, "end_byte", start) or start)
            return data[start:end].decode("utf-8", errors="replace").strip()
        return None

    @staticmethod
    def _line_number(point: Any) -> int | None:
        if point is None:
            return None
        try:
            return int(point[0]) + 1
        except (IndexError, TypeError, ValueError):
            row = _value(point, "row", None)
            return int(row) + 1 if row is not None else None
