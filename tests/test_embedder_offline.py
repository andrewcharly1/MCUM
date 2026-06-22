"""Offline-load guards for the embedding model.

Regression cover for the mcum_search_memory timeout: the cached model load used
to make an unauthenticated, latency-variable network round-trip to the Hugging
Face Hub, ballooning cold loads past the MCP timeout. The embedder now forces
offline mode (no Hub round-trip) unless a one-time download is explicitly
allowed.
"""

from __future__ import annotations

import importlib

import pytest

from MCUM.db import embedder

_OFFLINE_FLAGS = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "TOKENIZERS_PARALLELISM",
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("", False),
        ("nope", False),
    ],
)
def test_allow_download_parses_truthy_values(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("MCUM_EMBEDDING_ALLOW_DOWNLOAD", raw)
    assert embedder._allow_download() is expected


def test_force_offline_env_sets_hub_flags_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MCUM_EMBEDDING_ALLOW_DOWNLOAD", raising=False)
    for flag in _OFFLINE_FLAGS:
        monkeypatch.delenv(flag, raising=False)

    embedder._force_offline_env()

    assert embedder.os.environ["HF_HUB_OFFLINE"] == "1"
    assert embedder.os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert embedder.os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert embedder.os.environ["TOKENIZERS_PARALLELISM"] == "false"


def test_force_offline_env_respects_explicit_download_optin(monkeypatch) -> None:
    monkeypatch.setenv("MCUM_EMBEDDING_ALLOW_DOWNLOAD", "1")
    for flag in _OFFLINE_FLAGS:
        monkeypatch.delenv(flag, raising=False)

    embedder._force_offline_env()

    # When the operator opts into a one-time download, MCUM must NOT pin offline
    # mode or the download would be blocked.
    for flag in _OFFLINE_FLAGS:
        assert flag not in embedder.os.environ


def test_force_offline_env_does_not_clobber_existing_values(monkeypatch) -> None:
    monkeypatch.delenv("MCUM_EMBEDDING_ALLOW_DOWNLOAD", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")

    embedder._force_offline_env()

    # setdefault semantics: an explicit operator override wins.
    assert embedder.os.environ["HF_HUB_OFFLINE"] == "0"


def test_hash_backend_never_loads_a_model(monkeypatch) -> None:
    # The suite pins MCUM_EMBEDDING_BACKEND=hash; embed must stay fully local and
    # never touch sentence-transformers.
    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "hash")
    monkeypatch.setattr(embedder, "_model", None)
    vector = embedder.embed("conexion postgresql windows")
    assert len(vector) == embedder.EMBEDDING_DIM
