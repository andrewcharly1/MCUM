"""
Local embedding helpers for MCUM.

The first model load may download weights from Hugging Face if they are not
already cached locally. After that, embeddings run from the local cache.

Once the model is cached, loads run fully offline: MCUM forces
``HF_HUB_OFFLINE``/``TRANSFORMERS_OFFLINE`` and ``local_files_only=True`` so the
loader never makes an (unauthenticated, latency-variable) network round-trip to
the Hugging Face Hub to check for updates. That network check was the root cause
of multi-minute cold loads that timed out ``mcum_search_memory``. Set
``MCUM_EMBEDDING_ALLOW_DOWNLOAD=1`` to permit the one-time download on a machine
where the model is not yet cached.
"""

from __future__ import annotations

import json
import math
import os
import re
from hashlib import blake2b

from ..logging_utils import get_logger

SENTENCE_MODEL_NAME = "all-MiniLM-L6-v2"
ONNX_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
FALLBACK_MODEL_NAME = "mcum-hash-embedding-v1"
EMBEDDING_BACKEND = os.getenv("MCUM_EMBEDDING_BACKEND", "onnx").strip().lower()
EMBEDDING_DIM = 384

# Backend aliases. All semantic backends use the SAME all-MiniLM-L6-v2 weights,
# so their 384-dim vectors are interchangeable (cosine ~1.0): switching backends
# never requires re-embedding stored rows. They differ only in runtime cost.
_ST_BACKENDS = {"sentence", "sentence-transformers", "st"}
_ONNX_BACKENDS = {"onnx", "fastembed"}
_HASH_BACKENDS = {"hash", "fallback", "none"}

MODEL_NAME = (
    SENTENCE_MODEL_NAME
    if EMBEDDING_BACKEND in (_ST_BACKENDS | _ONNX_BACKENDS)
    else FALLBACK_MODEL_NAME
)

_model = None
_onnx_model = None
LOGGER = get_logger("db.embedder")
_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _use_sentence_transformers() -> bool:
    return EMBEDDING_BACKEND in _ST_BACKENDS


def _use_onnx() -> bool:
    return EMBEDDING_BACKEND in _ONNX_BACKENDS


def _fastembed_cache_dir() -> str:
    """Stable on-disk cache for the ONNX model (overridable, never Temp)."""
    explicit = str(os.getenv("MCUM_EMBEDDING_CACHE_DIR", "")).strip()
    if explicit:
        return explicit
    return os.path.join(os.path.expanduser("~"), ".cache", "fastembed")


def _allow_download() -> bool:
    return str(os.getenv("MCUM_EMBEDDING_ALLOW_DOWNLOAD", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _force_offline_env() -> None:
    """Block the Hugging Face Hub network round-trip on cached model loads.

    With the model already cached, the loader still contacts the Hub to check
    for updates unless these flags are set. That unauthenticated request is
    latency-variable and was the cause of cold loads ballooning past the
    ``mcum_search_memory`` timeout. We only force offline when downloads are not
    explicitly allowed, so first-run setup can still fetch the weights.
    """
    if _allow_download():
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _hash_embedding(text: str) -> list[float]:
    """Fast deterministic local embedding that never downloads a model."""
    vector = [0.0] * EMBEDDING_DIM
    tokens = _TOKEN_RE.findall(str(text or "").lower())
    if not tokens:
        return vector

    for token in tokens:
        digest = blake2b(token.encode("utf-8", errors="ignore"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % EMBEDDING_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm < 1e-10:
        return vector
    return [value / norm for value in vector]


def _get_model():
    """Load the embedding model lazily and cache it in memory."""
    global _model
    if not _use_sentence_transformers():
        return None
    if _model is None:
        _force_offline_env()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            LOGGER.warning(
                "sentence-transformers is not installed; falling back to %s. "
                "Install it or set MCUM_EMBEDDING_BACKEND=hash to silence this warning.",
                FALLBACK_MODEL_NAME,
            )
            return None

        LOGGER.info("Loading embeddings model: %s", SENTENCE_MODEL_NAME)
        # Cached, offline load: no Hub network round-trip (the timeout cause).
        try:
            _model = SentenceTransformer(SENTENCE_MODEL_NAME, local_files_only=True)
        except Exception as offline_exc:
            if _allow_download():
                # Model not cached yet and downloads permitted: one-time fetch.
                LOGGER.info(
                    "Model not in local cache; downloading %s (one-time).",
                    SENTENCE_MODEL_NAME,
                )
                try:
                    _model = SentenceTransformer(SENTENCE_MODEL_NAME)
                except Exception as download_exc:
                    LOGGER.warning(
                        "Failed to load embedding model; falling back to %s: %s",
                        FALLBACK_MODEL_NAME,
                        download_exc,
                    )
                    return None
            else:
                LOGGER.warning(
                    "Embedding model not found in local cache and downloads are "
                    "disabled; falling back to %s. Set MCUM_EMBEDDING_ALLOW_DOWNLOAD=1 "
                    "for the one-time download. Cause: %s",
                    FALLBACK_MODEL_NAME,
                    offline_exc,
                )
                return None
        LOGGER.info("Embedding model ready (%s dimensions)", EMBEDDING_DIM)
    return _model


def _repair_fastembed_snapshot(cache_dir: str) -> None:
    """Heal a Windows snapshot left incomplete by a failed symlink.

    On Windows without Developer Mode, ``huggingface_hub`` cannot create the
    symlinks it uses to materialize small files (``config.json``,
    ``tokenizer_config.json``) into the snapshot directory, so an offline reuse
    raises ``Could not find config.json``. The blobs are present; we copy them
    into the snapshot by content type. No-op on platforms where symlinks work.
    """
    import glob
    import shutil

    pattern = os.path.join(cache_dir, "models--*all-MiniLM*onnx*", "snapshots", "*")
    for snapshot in glob.glob(pattern):
        if not os.path.isdir(snapshot):
            continue
        if os.path.exists(os.path.join(snapshot, "config.json")):
            continue
        blobs_dir = os.path.join(os.path.dirname(os.path.dirname(snapshot)), "blobs")
        if not os.path.isdir(blobs_dir):
            continue
        for blob in os.listdir(blobs_dir):
            blob_path = os.path.join(blobs_dir, blob)
            try:
                with open(blob_path, encoding="utf-8") as handle:
                    parsed = json.load(handle)
            except (ValueError, OSError, UnicodeDecodeError):
                continue
            name = "config.json" if "model_type" in parsed else "tokenizer_config.json"
            target = os.path.join(snapshot, name)
            if not os.path.exists(target):
                shutil.copyfile(blob_path, target)


def _get_onnx_model():
    """Load the fastembed ONNX model lazily and cache it in memory.

    Same all-MiniLM-L6-v2 weights as the sentence-transformers backend but on
    ONNX Runtime: ~10x faster cold load and ~half the RAM, with numerically
    identical vectors. Falls back (returns None -> hash) if fastembed is missing
    or the model is not cached and downloads are disabled.
    """
    global _onnx_model
    if not _use_onnx():
        return None
    if _onnx_model is None:
        _force_offline_env()
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            from fastembed import TextEmbedding
        except ImportError:
            LOGGER.warning(
                "fastembed is not installed; falling back to %s. Install it "
                "(pip install fastembed) or switch MCUM_EMBEDDING_BACKEND.",
                FALLBACK_MODEL_NAME,
            )
            return None

        cache_dir = _fastembed_cache_dir()
        LOGGER.info("Loading ONNX embeddings model: %s", ONNX_MODEL_NAME)

        def _load():
            return TextEmbedding(model_name=ONNX_MODEL_NAME, cache_dir=cache_dir)

        try:
            _onnx_model = _load()
        except ValueError as missing_exc:
            # Windows symlink gap: blobs exist, snapshot incomplete. Heal + retry.
            _repair_fastembed_snapshot(cache_dir)
            try:
                _onnx_model = _load()
            except Exception as retry_exc:
                if _allow_download():
                    return _download_onnx_model(cache_dir)
                LOGGER.warning(
                    "ONNX model not usable from cache; falling back to %s. Set "
                    "MCUM_EMBEDDING_ALLOW_DOWNLOAD=1 for the one-time download. "
                    "Cause: %s",
                    FALLBACK_MODEL_NAME,
                    retry_exc,
                )
                return None
        except Exception as load_exc:
            if _allow_download():
                return _download_onnx_model(cache_dir)
            LOGGER.warning(
                "Failed to load ONNX model from cache; falling back to %s. Set "
                "MCUM_EMBEDDING_ALLOW_DOWNLOAD=1 for the one-time download. Cause: %s",
                FALLBACK_MODEL_NAME,
                load_exc,
            )
            return None
        LOGGER.info("ONNX embedding model ready (%s dimensions)", EMBEDDING_DIM)
    return _onnx_model


def _download_onnx_model(cache_dir: str):
    """One-time ONNX model download when MCUM_EMBEDDING_ALLOW_DOWNLOAD=1."""
    global _onnx_model
    # Allow the network round-trip for the initial fetch only.
    for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(flag, None)
    try:
        from fastembed import TextEmbedding

        LOGGER.info("Downloading ONNX model %s (one-time).", ONNX_MODEL_NAME)
        _onnx_model = TextEmbedding(model_name=ONNX_MODEL_NAME, cache_dir=cache_dir)
        _repair_fastembed_snapshot(cache_dir)
    except Exception as exc:
        LOGGER.warning(
            "Failed to download ONNX model; falling back to %s: %s",
            FALLBACK_MODEL_NAME,
            exc,
        )
        return None
    return _onnx_model


def warmup_model() -> str:
    """Load the active embedding model once and return its name."""
    if _use_onnx():
        return ONNX_MODEL_NAME if _get_onnx_model() is not None else FALLBACK_MODEL_NAME
    model = _get_model()
    return SENTENCE_MODEL_NAME if model is not None else FALLBACK_MODEL_NAME


def embed(text: str) -> list[float]:
    """Generate a normalized embedding for a single text."""
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Generate normalized embeddings for multiple texts."""
    if _use_onnx():
        onnx = _get_onnx_model()
        if onnx is not None:
            # fastembed yields already-normalized 384-dim vectors.
            return [list(map(float, vec)) for vec in onnx.embed(list(texts))]
        return [_hash_embedding(text) for text in texts]

    model = _get_model()
    if model is None:
        return [_hash_embedding(text) for text in texts]
    embeddings = model.encode(
        texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
    )
    return [embedding.tolist() for embedding in embeddings]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(vec_a) != len(vec_b):
        raise ValueError(f"Dimension mismatch: {len(vec_a)} vs {len(vec_b)}")

    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0

    return dot / (norm_a * norm_b)


def rank_by_similarity(
    query_text: str,
    candidates: list[dict],
    text_field: str = "title",
    embedding_field: str = "embedding",
    min_score: float = 0.3,
    top_k: int = 5,
) -> list[dict]:
    """Rank candidate dictionaries by semantic similarity to a query."""
    if not candidates:
        return []

    query_embedding = embed(query_text)
    results: list[dict] = []

    for candidate in candidates:
        cand_embedding = candidate.get(embedding_field)

        if cand_embedding is None:
            fallback_text = candidate.get(text_field, "")
            if not fallback_text:
                continue
            cand_embedding = embed(fallback_text)
        elif isinstance(cand_embedding, str):
            cand_embedding = json.loads(cand_embedding)

        similarity = cosine_similarity(query_embedding, cand_embedding)
        if similarity < min_score:
            continue

        enriched = dict(candidate)
        enriched["_similarity"] = round(similarity, 4)
        results.append(enriched)

    results.sort(key=lambda item: item["_similarity"], reverse=True)
    return results[:top_k]


def build_experience_text(experience: dict) -> str:
    """Build a representative text block for an experience embedding."""
    parts: list[str] = []

    title = experience.get("title")
    if title:
        parts.append(title)

    content = experience.get("content")
    if content:
        if isinstance(content, str):
            content = json.loads(content)
        if isinstance(content, dict):
            conclusion = content.get("conclusion")
            reasoning = content.get("reasoning")
            if conclusion:
                parts.append(conclusion)
            if reasoning:
                parts.append(reasoning[:200])

    task_description = experience.get("task_description")
    if task_description:
        parts.append(task_description)

    return " | ".join(parts)


if __name__ == "__main__":
    print("MCUM Embedder - Semantic Similarity Test")
    print("-" * 50)

    texts = [
        "Conexion PostgreSQL con psycopg3 en Windows",
        "Como conectar base de datos Python",
        "Flutter widget con estado",
        "Dashboard HTML con KPIs de logistica",
    ]

    query = "conectar python a PostgreSQL"
    print(f"\nQuery: '{query}'")
    print("\nSimilarities:")

    query_emb = embed(query)
    for text in texts:
        text_emb = embed(text)
        similarity = cosine_similarity(query_emb, text_emb)
        bar = "#" * int(similarity * 20)
        print(f"  {similarity:.3f} {bar} '{text[:50]}'")

    print("\nEmbedder is working correctly")
    print(f"   Model: {MODEL_NAME} ({EMBEDDING_DIM} dimensions)")
    print("   No external API, 100% offline")
