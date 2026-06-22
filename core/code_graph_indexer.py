"""
Native deterministic code graph indexer for MCUM.

This first pass intentionally avoids LLM calls. It extracts files, symbols and
lightweight dependency edges so MCUM can ask PostgreSQL for the small set of
code locations that matter before spending model context.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any

from .graph_extractors import TreeSitterExtractor
from ..db.project_registry import estimate_tokens


EXTRACTOR_VERSION = "mcum-code-graph-v2"

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".agent/runtime",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".dart_tool",
    ".gradle",
    ".idea",
    ".plugin_symlinks",
    "ephemeral",
    "target",
    "bin",
    "obj",
}

DEFAULT_EXCLUDED_FILES = {
    "GeneratedPluginRegistrant.java",
    "generated_plugin_registrant.dart",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".sql": "sql",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".m": "powerquery",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".php": "php",
    ".dart": "dart",
}


@dataclass
class IndexedFile:
    relative_path: str
    absolute_path: str
    language: str
    file_hash: str
    bytes_size: int
    line_count: int
    token_estimate: int
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphNode:
    relative_path: str
    node_kind: str
    name: str
    qualified_name: str
    signature: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    doc_excerpt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def search_text(self) -> str:
        return " ".join(
            _compact_text(value)
            for value in (
                self.node_kind,
                self.name,
                self.qualified_name,
                self.signature,
                self.relative_path,
                self.doc_excerpt,
            )
        )


def _compact_text(value: Any, limit: int = 280) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


@dataclass
class GraphEdge:
    edge_kind: str
    target_ref: str
    source_ref: str | None = None
    source_path: str | None = None
    target_path: str | None = None
    confidence: float = 0.70
    metadata: dict[str, Any] = field(default_factory=dict)


def _normalize_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _language_for_path(path: Path) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text(data: bytes) -> str:
    # Null bytes are never valid source content (UTF-16 files, binaries with a
    # source extension, or corrupted files). ast.parse() raises ValueError on
    # them, which would otherwise abort the entire scan, so strip them at read
    # time. The file hash is computed on raw bytes, so dedup is unaffected.
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def _line_end_for_ast(node: ast.AST) -> int | None:
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", None)


def _signature_from_python_node(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args: list[str] = []
        for arg in list(node.args.posonlyargs) + list(node.args.args):
            args.append(arg.arg)
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        for arg in node.args.kwonlyargs:
            args.append(arg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        return f"{node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [getattr(base, "id", "") or getattr(base, "attr", "") for base in node.bases]
        bases = [base for base in bases if base]
        return f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    return ""


def _python_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _python_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _module_qualified_name(relative_path: str) -> str:
    cleaned = relative_path.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
    for prefix in ("src.", "app."):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _parse_python(text: str, relative_path: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    module_name = _module_qualified_name(relative_path)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        # SyntaxError: malformed Python. ValueError: e.g. "source code string
        # cannot contain null bytes". Either way, degrade to a parse_error node
        # for this one file instead of aborting the whole project scan.
        return [
            GraphNode(
                relative_path=relative_path,
                node_kind="parse_error",
                name=Path(relative_path).name,
                qualified_name=f"{module_name}:parse_error",
                line_start=getattr(exc, "lineno", None),
                doc_excerpt=str(exc),
                metadata={"parser": "python_ast"},
            )
        ], []

    module_node = GraphNode(
        relative_path=relative_path,
        node_kind="module",
        name=Path(relative_path).stem,
        qualified_name=module_name,
        line_start=1,
        line_end=max(1, text.count("\n") + 1),
        doc_excerpt=ast.get_docstring(tree),
        metadata={"parser": "python_ast"},
    )
    nodes.append(module_node)

    parent_stack: list[str] = [module_name]
    parent_by_ast: dict[int, str] = {id(tree): module_name}

    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parent = module_name
            for candidate in ast.walk(tree):
                if candidate is node:
                    break
            # Recompute parent cheaply by line containment among already indexed scopes.
            containing = [
                existing
                for existing in nodes
                if existing.relative_path == relative_path
                and existing.line_start
                and existing.line_end
                and getattr(node, "lineno", 0) > existing.line_start
                and getattr(node, "lineno", 0) <= existing.line_end
                and existing.node_kind in {"class", "function", "async_function"}
            ]
            if containing:
                parent = sorted(containing, key=lambda item: (item.line_end or 0) - (item.line_start or 0))[0].qualified_name
            kind = "class" if isinstance(node, ast.ClassDef) else "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            qualified = f"{parent}.{node.name}" if parent else node.name
            parent_by_ast[id(node)] = qualified
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind=kind,
                    name=node.name,
                    qualified_name=qualified,
                    signature=_signature_from_python_node(node),
                    line_start=getattr(node, "lineno", None),
                    line_end=_line_end_for_ast(node),
                    doc_excerpt=ast.get_docstring(node),
                    metadata={"parser": "python_ast"},
                )
            )

    scope_nodes = [
        node for node in nodes if node.node_kind in {"function", "async_function", "class"} and node.line_start and node.line_end
    ]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            source_ref = module_name
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                base = node.module or ""
                targets = [f"{base}.{alias.name}".strip(".") for alias in node.names]
            for target in targets:
                edges.append(
                    GraphEdge(
                        edge_kind="imports",
                        source_ref=source_ref,
                        source_path=relative_path,
                        target_ref=target,
                        confidence=0.95,
                        metadata={"line": getattr(node, "lineno", None)},
                    )
                )
        if isinstance(node, ast.Call):
            call = _python_call_name(node.func)
            if not call:
                continue
            line = getattr(node, "lineno", 0)
            owner = module_name
            containing = [scope for scope in scope_nodes if (scope.line_start or 0) <= line <= (scope.line_end or 0)]
            if containing:
                owner = sorted(containing, key=lambda item: (item.line_end or 0) - (item.line_start or 0))[0].qualified_name
            edges.append(
                GraphEdge(
                    edge_kind="calls",
                    source_ref=owner,
                    source_path=relative_path,
                    target_ref=call,
                    confidence=0.65,
                    metadata={"line": line},
                )
            )
    return nodes, edges


_IMPORT_RE = re.compile(r"(?:import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))")
_JS_FUNCTION_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>|class\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_GO_DECL_RE = re.compile(r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)|type\s+([A-Za-z_]\w*)\s+(?:struct|interface)")
_SQL_DECL_RE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE)\s+([\w.\"\[\]]+)", re.IGNORECASE)
_SQL_REF_RE = re.compile(r"\b(?:FROM|JOIN|REFERENCES|UPDATE|INTO)\s+([\w.\"\[\]]+)", re.IGNORECASE)
_PS_FUNCTION_RE = re.compile(r"\bfunction\s+([A-Za-z_][\w-]*)", re.IGNORECASE)
_DART_IMPORT_RE = re.compile(r"^\s*(?:import|export|part)\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_DART_TYPE_RE = re.compile(
    r"^\s*(?:(?:abstract|base|final|sealed|interface)\s+)?(?:class|mixin|enum|extension)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_DART_FUNCTION_RE = re.compile(
    r"^\s*(?:static\s+)?(?:Future(?:<[^>\n]+>)?|Stream(?:<[^>\n]+>)?|void|bool|int|double|String|Widget|"
    r"[A-Za-z_]\w*(?:<[^>\n]+>)?\??)\s+([A-Za-z_]\w*)\s*\(([^;{}\n]*)\)\s*(?:async\*?|sync\*?)?\s*\{",
    re.MULTILINE,
)


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _parse_regex_language(text: str, relative_path: str, language: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    module_name = _module_qualified_name(relative_path)
    nodes = [
        GraphNode(
            relative_path=relative_path,
            node_kind="module" if language not in {"sql"} else "sql_file",
            name=Path(relative_path).stem,
            qualified_name=module_name,
            line_start=1,
            line_end=max(1, text.count("\n") + 1),
            metadata={"parser": f"{language}_regex"},
        )
    ]
    edges: list[GraphEdge] = []

    if language in {"javascript", "typescript"}:
        for match in _IMPORT_RE.finditer(text):
            target = match.group(1) or match.group(2)
            if target:
                edges.append(GraphEdge("imports", target_ref=target, source_ref=module_name, source_path=relative_path, confidence=0.90))
        for match in _JS_FUNCTION_RE.finditer(text):
            name = match.group(1) or match.group(3) or match.group(5)
            if not name:
                continue
            kind = "class" if match.group(5) else "function"
            params = match.group(2) or match.group(4) or ""
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind=kind,
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    signature=f"{name}({params})" if kind == "function" else f"class {name}",
                    line_start=line,
                    line_end=line,
                    metadata={"parser": f"{language}_regex"},
                )
            )
        return nodes, edges

    if language == "go":
        package = re.search(r"^\s*package\s+(\w+)", text, re.MULTILINE)
        if package:
            nodes[0].qualified_name = package.group(1)
        for match in re.finditer(r'import\s+(?:\((.*?)\)|"([^"]+)")', text, re.DOTALL):
            block = match.group(1) or match.group(2) or ""
            targets = re.findall(r'"([^"]+)"', block) or ([block] if block else [])
            for target in targets:
                edges.append(GraphEdge("imports", target_ref=target, source_ref=nodes[0].qualified_name, source_path=relative_path, confidence=0.90))
        for match in _GO_DECL_RE.finditer(text):
            name = match.group(1) or match.group(3)
            if not name:
                continue
            kind = "type" if match.group(3) else "function"
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind=kind,
                    name=name,
                    qualified_name=f"{nodes[0].qualified_name}.{name}",
                    signature=match.group(0).split("{", 1)[0].strip(),
                    line_start=line,
                    line_end=line,
                    metadata={"parser": "go_regex"},
                )
            )
        return nodes, edges

    if language == "sql":
        declarations: list[str] = []
        for match in _SQL_DECL_RE.finditer(text):
            name = match.group(1).strip('"[]')
            declarations.append(name)
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind="sql_object",
                    name=name.split(".")[-1],
                    qualified_name=name,
                    signature=match.group(0),
                    line_start=line,
                    line_end=line,
                    metadata={"parser": "sql_regex"},
                )
            )
        source = declarations[0] if declarations else module_name
        for match in _SQL_REF_RE.finditer(text):
            target = match.group(1).strip('"[]')
            edges.append(GraphEdge("uses_sql_object", target_ref=target, source_ref=source, source_path=relative_path, confidence=0.80))
        return nodes, edges

    if language == "powershell":
        for match in _PS_FUNCTION_RE.finditer(text):
            name = match.group(1)
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind="function",
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    signature=f"function {name}",
                    line_start=line,
                    line_end=line,
                    metadata={"parser": "powershell_regex"},
                )
            )
        return nodes, edges

    if language == "dart":
        for match in _DART_IMPORT_RE.finditer(text):
            edges.append(
                GraphEdge(
                    "imports",
                    target_ref=match.group(1),
                    source_ref=module_name,
                    source_path=relative_path,
                    confidence=0.90,
                )
            )
        for match in _DART_TYPE_RE.finditer(text):
            name = match.group(1)
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind="type",
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    signature=_compact_text(match.group(0)),
                    line_start=line,
                    line_end=line,
                    metadata={"parser": "dart_regex"},
                )
            )
        for match in _DART_FUNCTION_RE.finditer(text):
            name = match.group(1)
            line = _line_number(text, match.start())
            nodes.append(
                GraphNode(
                    relative_path=relative_path,
                    node_kind="function",
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    signature=f"{name}({_compact_text(match.group(2), limit=180)})",
                    line_start=line,
                    line_end=line,
                    metadata={"parser": "dart_regex"},
                )
            )
        return nodes, edges

    return nodes, edges


def _enrich_with_tree_sitter(
    *,
    data: bytes,
    relative_path: str,
    language: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    max_nodes: int,
) -> tuple[list[GraphNode], list[GraphEdge], list[dict[str, Any]]]:
    """Add Tree-sitter containment evidence without replacing legacy extraction."""
    result = TreeSitterExtractor(language, max_nodes=max_nodes).extract(data, relative_path)
    diagnostics = [item.__dict__ for item in result.diagnostics]
    if result.metadata.get("fallback"):
        return nodes, edges, diagnostics

    existing = {
        (item.node_kind, item.name, int(item.line_start or 0))
        for item in nodes
    }
    module_name = nodes[0].qualified_name if nodes else _module_qualified_name(relative_path)
    by_id = {item.node_id: item for item in result.nodes}
    parent_by_id = {item.target_id: item.source_id for item in result.edges if item.edge_kind == "contains"}
    qualified_by_id = {result.nodes[0].node_id: module_name} if result.nodes else {}

    def qualified(node_id: str) -> str:
        if node_id in qualified_by_id:
            return qualified_by_id[node_id]
        item = by_id[node_id]
        parent_id = parent_by_id.get(node_id)
        parent = qualified(parent_id) if parent_id in by_id else module_name
        resolved = f"{parent}.{item.name}" if parent else item.name
        qualified_by_id[node_id] = resolved
        return resolved

    for item in result.nodes[1:]:
        key = (item.node_kind, item.name, int(item.line_start or 0))
        qname = qualified(item.node_id)
        if key in existing:
            for current in nodes:
                if (current.node_kind, current.name, int(current.line_start or 0)) == key:
                    current.metadata = {
                        **current.metadata,
                        "tree_sitter": True,
                        "tree_sitter_type": item.metadata.get("tree_sitter_type"),
                        "tree_sitter_confidence": item.confidence,
                    }
                    break
            continue
        nodes.append(
            GraphNode(
                relative_path=relative_path,
                node_kind=item.node_kind,
                name=item.name,
                qualified_name=qname,
                line_start=item.line_start,
                line_end=item.line_end,
                metadata={
                    "parser": "tree_sitter",
                    "tree_sitter_type": item.metadata.get("tree_sitter_type"),
                    "confidence": item.confidence,
                },
            )
        )
        existing.add(key)

    edge_keys = {(item.edge_kind, item.source_ref, item.target_ref) for item in edges}
    for item in result.edges:
        source_ref = qualified_by_id.get(item.source_id)
        target_ref = qualified_by_id.get(item.target_id)
        if not source_ref or not target_ref:
            continue
        key = ("contains", source_ref, target_ref)
        if key in edge_keys:
            continue
        edges.append(
            GraphEdge(
                edge_kind="contains",
                source_ref=source_ref,
                source_path=relative_path,
                target_ref=target_ref,
                target_path=relative_path,
                confidence=item.confidence,
                metadata={"parser": "tree_sitter"},
            )
        )
        edge_keys.add(key)
    return nodes, edges, diagnostics


def _should_skip_dir(path: Path, root: Path, excluded_dirs: set[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    parts = set(relative.split("/"))
    return any(excluded in parts or relative.startswith(excluded.rstrip("/") + "/") for excluded in excluded_dirs)


def _iter_source_files(
    root: Path,
    *,
    excluded_dirs: set[str],
    max_file_bytes: int,
    max_files: int | None = None,
    max_seconds: float | None = None,
) -> tuple[list[Path], int, int, bool]:
    """Walk source files under root, bounded by an optional file/time budget.

    Returns (files, skipped, directories_pruned, budget_exhausted). The budget
    prevents a runaway os.walk over very large trees (e.g. an entire OneDrive
    workspace of unrelated projects) from blocking session-begin for minutes.
    """
    files: list[Path] = []
    skipped = 0
    directories_pruned = 0
    budget_exhausted = False
    file_cap = max_files if (max_files and max_files > 0) else None
    started = time.perf_counter()
    deadline = (started + max_seconds) if (max_seconds and max_seconds > 0) else None

    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            candidate = current / dirname
            if _should_skip_dir(candidate, root, excluded_dirs):
                directories_pruned += 1
            else:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            path = current / filename
            language = _language_for_path(path)
            if not language:
                continue
            if path.name in DEFAULT_EXCLUDED_FILES:
                skipped += 1
                continue
            try:
                if path.stat().st_size > max_file_bytes:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            files.append(path)
            if file_cap is not None and len(files) >= file_cap:
                budget_exhausted = True
                break

        if budget_exhausted:
            break
        if deadline is not None and time.perf_counter() >= deadline:
            budget_exhausted = True
            break

    return sorted(files), skipped, directories_pruned, budget_exhausted


def _serialize_node(node: GraphNode) -> dict[str, Any]:
    return {
        "relative_path": node.relative_path,
        "node_kind": node.node_kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "signature": _compact_text(node.signature) or None,
        "line_start": node.line_start,
        "line_end": node.line_end,
        "doc_excerpt": _compact_text(node.doc_excerpt) or None,
        "search_text": node.search_text,
        "metadata": node.metadata,
    }


def _serialize_edge(edge: GraphEdge) -> dict[str, Any]:
    return {
        "edge_kind": edge.edge_kind,
        "source_ref": edge.source_ref,
        "source_path": edge.source_path,
        "target_ref": edge.target_ref,
        "target_path": edge.target_path,
        "confidence": edge.confidence,
        "metadata": edge.metadata,
    }


def scan_project_code_graph(
    project_path: str,
    *,
    excluded_dirs: list[str] | None = None,
    max_file_bytes: int = 1_000_000,
    previous_manifest: dict[str, dict[str, Any]] | None = None,
    tree_sitter_enabled: bool = False,
    tree_sitter_languages: list[str] | None = None,
    tree_sitter_max_nodes: int = 10_000,
    max_files: int | None = None,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    root = Path(project_path).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Project path not found or not a directory: {project_path}")

    excludes = set(DEFAULT_EXCLUDED_DIRS)
    excludes.update(str(item).replace("\\", "/").strip("/") for item in (excluded_dirs or []) if str(item).strip())
    source_files, skipped, directories_pruned, budget_exhausted = _iter_source_files(
        root,
        excluded_dirs=excludes,
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        max_seconds=max_seconds,
    )
    indexed_files: list[dict[str, Any]] = []
    all_nodes: list[dict[str, Any]] = []
    all_edges: list[dict[str, Any]] = []
    token_total = 0
    indexed_token_total = 0
    previous = {
        str(path).replace("\\", "/"): dict(item or {})
        for path, item in (previous_manifest or {}).items()
    }
    current_paths: set[str] = set()
    new_paths: list[str] = []
    modified_paths: list[str] = []
    unchanged_paths: list[str] = []
    tree_sitter_diagnostics: list[dict[str, Any]] = []
    enabled_languages = {
        str(item).strip().lower()
        for item in (tree_sitter_languages or [])
        if str(item).strip()
    }

    for path in source_files:
        relative = _normalize_relative(path, root)
        current_paths.add(relative)
        language = _language_for_path(path) or "text"
        try:
            data = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        file_hash = _sha256_bytes(data)
        previous_item = previous.get(relative)
        is_new = previous_item is None
        is_modified = bool(previous_item) and str(previous_item.get("file_hash") or "") != file_hash
        if is_new:
            new_paths.append(relative)
        elif is_modified:
            modified_paths.append(relative)
        else:
            unchanged_paths.append(relative)

        text: str | None = None
        if is_new or is_modified or previous_manifest is None:
            text = _read_text(data)
            line_count = max(1, text.count("\n") + 1)
            token_estimate = estimate_tokens(text)
        else:
            line_count = int(previous_item.get("line_count") or 0)
            token_estimate = int(previous_item.get("token_estimate") or 0)
        token_total += token_estimate
        file_payload = IndexedFile(
            relative_path=relative,
            absolute_path=str(path),
            language=language,
            file_hash=file_hash,
            bytes_size=len(data),
            line_count=line_count,
            token_estimate=token_estimate,
            metadata={"extractor_version": EXTRACTOR_VERSION},
        ).__dict__
        if not (is_new or is_modified or previous_manifest is None):
            continue
        indexed_token_total += token_estimate
        indexed_files.append(file_payload)
        assert text is not None
        if language == "python":
            nodes, edges = _parse_python(text, relative)
        else:
            nodes, edges = _parse_regex_language(text, relative, language)
        if tree_sitter_enabled and (not enabled_languages or language in enabled_languages):
            nodes, edges, diagnostics = _enrich_with_tree_sitter(
                data=data,
                relative_path=relative,
                language=language,
                nodes=nodes,
                edges=edges,
                max_nodes=tree_sitter_max_nodes,
            )
            tree_sitter_diagnostics.extend(
                {"relative_path": relative, **item}
                for item in diagnostics
            )
        all_nodes.extend(_serialize_node(node) for node in nodes)
        all_edges.extend(_serialize_edge(edge) for edge in edges)

    deleted_paths = sorted(set(previous) - current_paths)
    changed_paths = sorted(set(new_paths + modified_paths))
    incremental = previous_manifest is not None
    return {
        "files": indexed_files,
        "nodes": all_nodes,
        "edges": all_edges,
        "delta": {
            "incremental": incremental,
            "changed_paths": changed_paths,
            "new_paths": sorted(new_paths),
            "modified_paths": sorted(modified_paths),
            "deleted_paths": deleted_paths,
            "unchanged_paths": sorted(unchanged_paths),
            "has_changes": bool(changed_paths or deleted_paths),
        },
        "stats": {
            "files_scanned": len(source_files),
            "files_indexed": len(indexed_files),
            "files_skipped": skipped,
            "directories_pruned": directories_pruned,
            "files_new": len(new_paths),
            "files_modified": len(modified_paths),
            "files_deleted": len(deleted_paths),
            "files_unchanged": len(unchanged_paths),
            "nodes_indexed": len(all_nodes),
            "edges_indexed": len(all_edges),
            "tokens_indexed_estimate": indexed_token_total,
            "tokens_project_estimate": token_total,
            "tokens_saved_estimate": max(0, token_total - min(token_total, 1400)),
            "budget_exhausted": budget_exhausted,
        },
        "metadata": {
            "extractor_version": EXTRACTOR_VERSION,
            "excluded_dirs": sorted(excludes),
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "max_seconds": max_seconds,
            "budget_exhausted": budget_exhausted,
            "root": str(root),
            "incremental": incremental,
            "tree_sitter_enabled": bool(tree_sitter_enabled),
            "tree_sitter_languages": sorted(enabled_languages),
            "tree_sitter_diagnostics": tree_sitter_diagnostics[:100],
        },
    }
