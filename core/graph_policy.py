"""Typed, fail-safe policy loading for governed graph capabilities."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


GRAPH_POLICY_FILE = Path(__file__).resolve().parents[1] / "directives" / "graph_policy.json"


@dataclass(frozen=True)
class GraphFeatures:
    tree_sitter: bool = False
    multimedia: bool = False
    analytics: bool = False
    impact: bool = False
    exports: bool = False
    comparison: bool = False
    cross_project: bool = False
    dashboard: bool = False


@dataclass(frozen=True)
class GraphQueryLimits:
    default_depth: int = 1
    max_depth: int = 3
    default_page_size: int = 25
    max_page_size: int = 100
    max_nodes: int = 250
    max_edges_per_node: int = 100
    max_evidence_items: int = 100


@dataclass(frozen=True)
class MultimediaBudget:
    max_file_bytes: int = 25 * 1024 * 1024
    max_ocr_pages: int = 50
    max_video_seconds: int = 600


@dataclass(frozen=True)
class AnalyticsBudget:
    max_nodes: int = 10000
    max_runtime_seconds: int = 60


@dataclass(frozen=True)
class ImpactBudget:
    max_depth: int = 4
    max_nodes: int = 1000


@dataclass(frozen=True)
class ExportBudget:
    max_nodes: int = 5000
    max_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True)
class CrossProjectBudget:
    max_projects: int = 1
    max_nodes_per_project: int = 500


@dataclass(frozen=True)
class DashboardBudget:
    max_rows: int = 1000
    min_refresh_seconds: int = 10


@dataclass(frozen=True)
class GraphBudgets:
    multimedia: MultimediaBudget = field(default_factory=MultimediaBudget)
    analytics: AnalyticsBudget = field(default_factory=AnalyticsBudget)
    impact: ImpactBudget = field(default_factory=ImpactBudget)
    exports: ExportBudget = field(default_factory=ExportBudget)
    cross_project: CrossProjectBudget = field(default_factory=CrossProjectBudget)
    dashboard: DashboardBudget = field(default_factory=DashboardBudget)


@dataclass(frozen=True)
class GraphPolicy:
    version: str = "1.0.0"
    features: GraphFeatures = field(default_factory=GraphFeatures)
    priority_languages: tuple[str, ...] = (
        "python",
        "javascript",
        "typescript",
        "go",
        "sql",
        "html",
        "css",
    )
    query: GraphQueryLimits = field(default_factory=GraphQueryLimits)
    budgets: GraphBudgets = field(default_factory=GraphBudgets)
    warnings: tuple[str, ...] = field(default=(), compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("warnings", None)
        payload["priority_languages"] = list(self.priority_languages)
        return payload


DEFAULT_GRAPH_POLICY = GraphPolicy()

_QUERY_BOUNDS = {
    "default_depth": (1, 8),
    "max_depth": (1, 8),
    "default_page_size": (1, 250),
    "max_page_size": (1, 500),
    "max_nodes": (1, 5000),
    "max_edges_per_node": (1, 1000),
    "max_evidence_items": (1, 1000),
}

_BUDGET_BOUNDS = {
    "multimedia": {
        "max_file_bytes": (1, 1024 * 1024 * 1024),
        "max_ocr_pages": (1, 1000),
        "max_video_seconds": (1, 24 * 60 * 60),
    },
    "analytics": {
        "max_nodes": (1, 1000000),
        "max_runtime_seconds": (1, 3600),
    },
    "impact": {"max_depth": (1, 16), "max_nodes": (1, 100000)},
    "exports": {
        "max_nodes": (1, 1000000),
        "max_bytes": (1, 2 * 1024 * 1024 * 1024),
    },
    "cross_project": {"max_projects": (1, 20), "max_nodes_per_project": (1, 100000)},
    "dashboard": {"max_rows": (1, 100000), "min_refresh_seconds": (1, 3600)},
}


def _mapping(value: Any, name: str, warnings: list[str]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if value is not None:
        warnings.append(f"{name} must be an object; safe defaults applied")
    return {}


def _safe_bool(value: Any, default: bool, name: str, warnings: list[str]) -> bool:
    if isinstance(value, bool):
        return value
    if value is not None:
        warnings.append(f"{name} must be a boolean; safe default applied")
    return default


def _safe_int(
    value: Any,
    default: int,
    bounds: tuple[int, int],
    name: str,
    warnings: list[str],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            warnings.append(f"{name} must be an integer; safe default applied")
        return default
    minimum, maximum = bounds
    if value < minimum or value > maximum:
        warnings.append(f"{name} outside safe bounds; value clamped")
    return max(minimum, min(value, maximum))


def _int_section(
    raw: Mapping[str, Any],
    defaults: Any,
    bounds: Mapping[str, tuple[int, int]],
    section_name: str,
    cls: type[Any],
    warnings: list[str],
) -> Any:
    values = {
        key: _safe_int(raw.get(key), getattr(defaults, key), limit, f"{section_name}.{key}", warnings)
        for key, limit in bounds.items()
    }
    return cls(**values)


def validate_graph_policy(raw: Any) -> GraphPolicy:
    """Normalize untrusted policy data without allowing unsafe implicit values."""
    warnings: list[str] = []
    root = _mapping(raw, "graph_policy", warnings)
    feature_raw = _mapping(root.get("features"), "features", warnings)
    query_raw = _mapping(root.get("query"), "query", warnings)
    budgets_raw = _mapping(root.get("budgets"), "budgets", warnings)

    features = GraphFeatures(
        **{
            name: _safe_bool(feature_raw.get(name), False, f"features.{name}", warnings)
            for name in GraphFeatures.__dataclass_fields__
        }
    )
    query = _int_section(
        query_raw,
        DEFAULT_GRAPH_POLICY.query,
        _QUERY_BOUNDS,
        "query",
        GraphQueryLimits,
        warnings,
    )
    if query.default_depth > query.max_depth:
        warnings.append("query.default_depth exceeded query.max_depth; max_depth raised")
        query = GraphQueryLimits(**{**asdict(query), "max_depth": query.default_depth})
    if query.default_page_size > query.max_page_size:
        warnings.append("query.default_page_size exceeded query.max_page_size; max_page_size raised")
        query = GraphQueryLimits(**{**asdict(query), "max_page_size": query.default_page_size})

    budget_values: dict[str, Any] = {}
    budget_classes = {
        "multimedia": MultimediaBudget,
        "analytics": AnalyticsBudget,
        "impact": ImpactBudget,
        "exports": ExportBudget,
        "cross_project": CrossProjectBudget,
        "dashboard": DashboardBudget,
    }
    for name, cls in budget_classes.items():
        section = _mapping(budgets_raw.get(name), f"budgets.{name}", warnings)
        budget_values[name] = _int_section(
            section,
            getattr(DEFAULT_GRAPH_POLICY.budgets, name),
            _BUDGET_BOUNDS[name],
            f"budgets.{name}",
            cls,
            warnings,
        )

    language_value = root.get("priority_languages", DEFAULT_GRAPH_POLICY.priority_languages)
    if not isinstance(language_value, (list, tuple)):
        warnings.append("priority_languages must be a list; safe default applied")
        languages = DEFAULT_GRAPH_POLICY.priority_languages
    else:
        languages = tuple(
            dict.fromkeys(str(item).strip().lower() for item in language_value if str(item).strip())
        )
        if not languages:
            warnings.append("priority_languages was empty; safe default applied")
            languages = DEFAULT_GRAPH_POLICY.priority_languages

    version = str(root.get("version") or _mapping(root.get("_meta"), "_meta", warnings).get("version") or "1.0.0")
    return GraphPolicy(
        version=version,
        features=features,
        priority_languages=languages,
        query=query,
        budgets=GraphBudgets(**budget_values),
        warnings=tuple(warnings),
    )


def load_graph_policy(path: str | Path | None = None) -> GraphPolicy:
    """Load graph policy from disk, falling back to safe typed defaults."""
    policy_path = Path(path) if path is not None else GRAPH_POLICY_FILE
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return GraphPolicy(warnings=(f"policy load failed: {exc.__class__.__name__}; safe defaults applied",))
    return validate_graph_policy(raw)
