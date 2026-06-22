"""Safe data normalization for the local MCUM dashboard."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from typing import Any


VALID_STATUSES = (
    "configured",
    "connected",
    "degraded",
    "failed",
    "disabled",
    "unknown",
)
REDACTED = "[redacted]"
_FAILED = {"failed", "failure", "error", "offline", "unavailable"}
_DEGRADED = {"degraded", "warning", "warn", "stale", "partial"}
_DISABLED = {"disabled", "inactive", "off"}
_CONFIGURED = {"configured", "ready", "enabled", "connected", "online", "healthy", "ok"}
_ACTIVITY_FIELDS = (
    "last_heartbeat_at",
    "heartbeat_at",
    "last_invocation_at",
    "invocation_at",
    "last_activity_at",
    "last_seen_at",
    "updated_at",
)
_CONNECTIVITY_FIELDS = (
    "last_heartbeat_at",
    "heartbeat_at",
    "last_invocation_at",
    "invocation_at",
)
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "cookie",
    "credentials",
    "database_url",
    "db_password",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "session_cookie",
    "token",
}
_SENSITIVE_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_auth_token",
    "_client_secret",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_secret_key",
    "_token",
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+=*")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|password|secret|token)=)[^&#\s]+"
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(access_token|api_key|apikey|password|secret|token)\s*[:=]\s*"
    r"[\"']?[^,\s;\"']+"
)


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _redact_text(value: str) -> str:
    value = _URL_CREDENTIALS.sub(lambda match: f"{match.group('scheme')}{REDACTED}@", value)
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}", value)
    return _INLINE_SECRET.sub(lambda match: f"{match.group(1)}={REDACTED}", value)


def redact_secrets(value: Any) -> Any:
    """Return a deep redacted copy suitable for an API response."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_sensitive_key(key) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return deepcopy(value)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_activity(record: Mapping[str, Any]) -> datetime | None:
    values = [_as_datetime(record.get(field)) for field in _ACTIVITY_FIELDS]
    valid = [value for value in values if value is not None]
    return max(valid) if valid else None


def _latest_connectivity_event(record: Mapping[str, Any]) -> datetime | None:
    values = [_as_datetime(record.get(field)) for field in _CONNECTIVITY_FIELDS]
    valid = [value for value in values if value is not None]
    return max(valid) if valid else None


def normalize_status(
    record: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    recent_window: timedelta = timedelta(minutes=5),
) -> str:
    """Normalize status while requiring recent activity for connected."""
    source = record or {}
    raw_status = str(source.get("status") or source.get("health_state") or "").strip().lower()
    if source.get("enabled") is False or raw_status in _DISABLED:
        return "disabled"
    if raw_status in _FAILED or source.get("failed") is True:
        return "failed"
    if raw_status in _DEGRADED or source.get("degraded") is True:
        return "degraded"

    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    activity = _latest_connectivity_event(source)
    age = reference - activity if activity is not None else None
    if age is not None and -recent_window <= age <= recent_window:
        return "connected"
    if source.get("configured") is True or source.get("enabled") is True or raw_status in _CONFIGURED:
        return "configured"
    return "unknown"


class DashboardDataService:
    """Read-only facade over injected dashboard data sources."""

    def __init__(
        self,
        backend: Any = None,
        *,
        now: Callable[[], datetime] | None = None,
        heartbeat_seconds: int = 300,
    ) -> None:
        self._backend = backend if backend is not None else {}
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._recent_window = timedelta(seconds=max(1, int(heartbeat_seconds)))

    def _fetch(self, name: str, default: Any) -> Any:
        if callable(self._backend):
            value = self._backend(name)
        elif isinstance(self._backend, Mapping):
            value = self._backend.get(name, default)
            if callable(value):
                value = value()
        else:
            provider = getattr(self._backend, f"get_{name}", None)
            value = provider() if callable(provider) else default
        return deepcopy(default if value is None else value)

    @staticmethod
    def _items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, Mapping):
            value = value.get("items", [])
        if not isinstance(value, (list, tuple)):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def _normalize_items(self, source: Any) -> list[dict[str, Any]]:
        items = []
        for item in self._items(source):
            normalized = redact_secrets(item)
            normalized["status"] = normalize_status(
                item,
                now=self._now(),
                recent_window=self._recent_window,
            )
            activity = _latest_activity(item)
            normalized["last_activity_at"] = activity.isoformat() if activity else None
            items.append(normalized)
        return items

    def get_health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "mcum-dashboard",
            "mode": "read-only",
            "time": self._now().astimezone(timezone.utc).isoformat(),
        }

    def get_connectors(self) -> dict[str, Any]:
        items = self._normalize_items(self._fetch("connectors", []))
        return {"items": items, "total": len(items)}

    def get_agents(self) -> dict[str, Any]:
        items = self._normalize_items(self._fetch("agents", []))
        return {"items": items, "total": len(items)}

    def get_graph(self) -> dict[str, Any]:
        value = self._fetch("graph", {})
        if not isinstance(value, Mapping):
            value = {}
        graph = redact_secrets(dict(value))
        graph["status"] = normalize_status(
            value,
            now=self._now(),
            recent_window=self._recent_window,
        )
        activity = _latest_activity(value)
        graph["last_activity_at"] = activity.isoformat() if activity else None
        return graph

    def get_summary(self) -> dict[str, Any]:
        value = self._fetch("summary", {})
        if not isinstance(value, Mapping):
            value = {}
        connectors = self.get_connectors()["items"]
        agents = self.get_agents()["items"]
        graph = self.get_graph()
        status_counts = {status: 0 for status in VALID_STATUSES}
        for item in connectors:
            status_counts[item["status"]] += 1
        summary = redact_secrets(dict(value))
        summary.update(
            {
                "connector_statuses": status_counts,
                "connectors_total": len(connectors),
                "agents_total": len(agents),
                "graph_status": graph.get("status", "unknown"),
            }
        )
        return summary
