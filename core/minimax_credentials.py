"""
Credential discovery for MiniMax workers.

MCUM never persists provider secrets. This resolver only returns credentials in
memory and exposes a redacted status for logs/artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"


@dataclass(frozen=True)
class MiniMaxCredentials:
    api_key: str
    base_url: str
    protocol: str
    model: str
    source: str

    def redacted_metadata(self) -> dict[str, Any]:
        return {
            "available": bool(self.api_key),
            "source": self.source,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "model": self.model,
        }


def _clean_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    return text.strip()


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char
            continue
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].strip()
    return value.strip()


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return env
    for line in lines:
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = _clean_value(_strip_inline_comment(value))
    return env


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _find_key_recursive(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).upper() in keys:
                cleaned = _clean_value(item)
                if cleaned:
                    return cleaned
        for item in value.values():
            found = _find_key_recursive(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key_recursive(item, keys)
            if found:
                return found
    return ""


def _yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return ""
    return _clean_value(_strip_inline_comment(match.group(1)))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _home() -> Path:
    return Path.home()


def _local_app_data() -> Path | None:
    raw = os.environ.get("LOCALAPPDATA")
    if not raw:
        return None
    return Path(raw)


def _candidate_env_files() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    explicit = os.environ.get("MCUM_MINIMAX_ENV_PATH")
    if explicit:
        candidates.append(("explicit_env", Path(explicit).expanduser()))

    local_app_data = _local_app_data()
    if local_app_data:
        candidates.append(("hermes_appdata_env", local_app_data / "hermes" / ".env"))

    home = _home()
    candidates.extend(
        [
            ("hermes_home_env", home / ".hermes" / ".env"),
            ("opencode_home_env", home / ".opencode" / ".env"),
            ("opencode_config_env", home / ".config" / "opencode" / ".env"),
            ("claude_home_env", home / ".claude" / ".env"),
        ]
    )

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(("opencode_appdata_env", Path(appdata) / "opencode" / ".env"))
    return candidates


def _candidate_claude_settings() -> list[tuple[str, Path]]:
    home = _home()
    return [
        ("claude_settings", home / ".claude" / "settings.json"),
        ("claude_profile", home / ".claude.json"),
    ]


def _candidate_hermes_configs() -> list[tuple[str, Path, Path]]:
    home = _home()
    candidates = [("hermes_home", home / ".hermes" / "config.yaml", home / ".hermes" / ".env")]
    local_app_data = _local_app_data()
    if local_app_data:
        candidates.insert(
            0,
            (
                "hermes_appdata",
                local_app_data / "hermes" / "config.yaml",
                local_app_data / "hermes" / ".env",
            ),
        )
    return candidates


def _protocol_from_base_url(base_url: str, fallback: str = "openai") -> str:
    lowered = str(base_url or "").lower()
    if "anthropic" in lowered:
        return "anthropic"
    return fallback


def _credential_from_direct_values(
    *,
    values: dict[str, str],
    source: str,
    policy: dict[str, Any] | None,
) -> MiniMaxCredentials | None:
    key = _clean_value(values.get("MINIMAX_API_KEY") or values.get("MINIMAX_TOKEN"))
    if not key:
        return None
    base_url = _clean_value(values.get("MINIMAX_BASE_URL") or (policy or {}).get("base_url")) or DEFAULT_MINIMAX_BASE_URL
    protocol = _clean_value((policy or {}).get("protocol")) or _protocol_from_base_url(base_url, "openai")
    if protocol == "auto":
        protocol = _protocol_from_base_url(base_url, "openai")
    model = _clean_value(values.get("MINIMAX_MODEL") or (policy or {}).get("default_model")) or DEFAULT_MINIMAX_MODEL
    return MiniMaxCredentials(api_key=key, base_url=base_url, protocol=protocol, model=model, source=source)


def _credential_from_anthropic_values(
    *,
    token: str,
    base_url: str,
    model: str,
    source: str,
) -> MiniMaxCredentials | None:
    token = _clean_value(token)
    if not token:
        return None
    base_url = _clean_value(base_url) or "http://127.0.0.1:8080/anthropic"
    model = _clean_value(model) or DEFAULT_MINIMAX_MODEL
    if "minimax" not in model.lower() and "minimax" not in base_url.lower() and "anthropic" not in base_url.lower():
        return None
    return MiniMaxCredentials(
        api_key=token,
        base_url=base_url,
        protocol="anthropic",
        model=model,
        source=source,
    )


def resolve_minimax_credentials(policy: dict[str, Any] | None = None) -> MiniMaxCredentials | None:
    env_values = {key: value for key, value in os.environ.items() if key.startswith(("MINIMAX_", "ANTHROPIC_"))}
    direct = _credential_from_direct_values(values=env_values, source="process_env", policy=policy)
    if direct:
        return direct
    anthropic = _credential_from_anthropic_values(
        token=env_values.get("ANTHROPIC_AUTH_TOKEN") or env_values.get("ANTHROPIC_TOKEN") or "",
        base_url=env_values.get("ANTHROPIC_BASE_URL") or "",
        model=env_values.get("ANTHROPIC_MODEL") or env_values.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or "",
        source="process_env:anthropic",
    )
    if anthropic:
        return anthropic

    for label, path in _candidate_env_files():
        values = parse_env_file(path)
        direct = _credential_from_direct_values(values=values, source=f"{label}:{path}", policy=policy)
        if direct:
            return direct
        anthropic = _credential_from_anthropic_values(
            token=values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_TOKEN") or "",
            base_url=values.get("ANTHROPIC_BASE_URL") or "",
            model=values.get("ANTHROPIC_MODEL") or values.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or "",
            source=f"{label}:{path}:anthropic",
        )
        if anthropic:
            return anthropic

    for label, path in _candidate_claude_settings():
        data = _read_json(path)
        token = _find_key_recursive(data, {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"})
        base_url = _find_key_recursive(data, {"ANTHROPIC_BASE_URL"})
        model = _find_key_recursive(data, {"ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"})
        credential = _credential_from_anthropic_values(
            token=token,
            base_url=base_url,
            model=model,
            source=f"{label}:{path}",
        )
        if credential:
            return credential

    for label, config_path, env_path in _candidate_hermes_configs():
        text = _read_text(config_path)
        env_values = parse_env_file(env_path)
        token = env_values.get("ANTHROPIC_TOKEN") or env_values.get("ANTHROPIC_AUTH_TOKEN")
        base_url = _yaml_scalar(text, "base_url") or env_values.get("ANTHROPIC_BASE_URL")
        model = _yaml_scalar(text, "default") or env_values.get("ANTHROPIC_MODEL")
        credential = _credential_from_anthropic_values(
            token=token or "",
            base_url=base_url or "",
            model=model or "",
            source=f"{label}:{config_path}",
        )
        if credential:
            return credential

    return None


def minimax_credential_status(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    credential = resolve_minimax_credentials(policy)
    if not credential:
        return {"available": False, "source": None, "base_url": None, "protocol": None, "model": None}
    return credential.redacted_metadata()
