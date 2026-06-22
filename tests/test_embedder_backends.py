"""Backend selection + ONNX integration for the embedder.

The semantic backends (onnx, sentence-transformers) share all-MiniLM-L6-v2
weights, so switching never re-embeds stored rows. These tests cover the routing
and the Windows cache self-repair without loading any model.
"""

from __future__ import annotations

import json

import pytest

from MCUM import mcum_embedding
from MCUM.db import embedder


def test_cli_resolves_aliases() -> None:
    assert mcum_embedding._resolve("st") == "sentence-transformers"
    assert mcum_embedding._resolve("fastembed") == "onnx"
    assert mcum_embedding._resolve("ONNX") == "onnx"
    assert mcum_embedding._resolve("hash") == "hash"


def test_cli_rejects_unknown_backend() -> None:
    with pytest.raises(SystemExit):
        mcum_embedding._resolve("bogus-backend")


def test_hash_backend_is_always_available() -> None:
    assert mcum_embedding._installed("hash") is True


def test_use_onnx_flag_reflects_backend(monkeypatch) -> None:
    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "onnx")
    assert embedder._use_onnx() is True
    assert embedder._use_sentence_transformers() is False

    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "sentence-transformers")
    assert embedder._use_onnx() is False
    assert embedder._use_sentence_transformers() is True


def test_embed_batch_uses_hash_when_backend_is_hash(monkeypatch) -> None:
    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "hash")
    monkeypatch.setattr(embedder, "_model", None)
    monkeypatch.setattr(embedder, "_onnx_model", None)
    out = embedder.embed_batch(["alpha", "beta"])
    assert len(out) == 2
    assert all(len(vec) == embedder.EMBEDDING_DIM for vec in out)


def test_onnx_backend_falls_back_to_hash_when_model_unavailable(monkeypatch) -> None:
    # ONNX selected but fastembed/model not usable: must degrade, never crash.
    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "onnx")
    monkeypatch.setattr(embedder, "_get_onnx_model", lambda: None)
    out = embedder.embed_batch(["solo"])
    assert len(out) == 1
    assert len(out[0]) == embedder.EMBEDDING_DIM


def test_repair_fastembed_snapshot_copies_missing_config(tmp_path) -> None:
    model_dir = tmp_path / "models--qdrant--all-MiniLM-L6-v2-onnx"
    snapshot = model_dir / "snapshots" / "deadbeef"
    blobs = model_dir / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir(parents=True)
    (blobs / "blob_cfg").write_text(json.dumps({"model_type": "bert"}), encoding="utf-8")
    (blobs / "blob_tok").write_text(
        json.dumps({"tokenizer_class": "BertTokenizer"}), encoding="utf-8"
    )
    (snapshot / "model.onnx").write_text("binary", encoding="utf-8")

    embedder._repair_fastembed_snapshot(str(tmp_path))

    assert (snapshot / "config.json").exists()
    assert (snapshot / "tokenizer_config.json").exists()
    assert json.loads((snapshot / "config.json").read_text(encoding="utf-8"))["model_type"] == "bert"


def test_repair_is_noop_when_config_present(tmp_path) -> None:
    snapshot = tmp_path / "models--qdrant--all-MiniLM-L6-v2-onnx" / "snapshots" / "x"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    # No blobs dir, missing-config branch must be skipped without error.
    embedder._repair_fastembed_snapshot(str(tmp_path))
    assert (snapshot / "config.json").read_text(encoding="utf-8") == "{}"
