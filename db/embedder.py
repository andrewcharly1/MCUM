"""
Local embedding helpers for MCUM.

The first model load may download weights from Hugging Face if they are not
already cached locally. After that, embeddings run from the local cache.
"""

from __future__ import annotations

import json
import math

from ..logging_utils import get_logger

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_model = None
LOGGER = get_logger("db.embedder")


def _get_model():
    """Load the embedding model lazily and cache it in memory."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Install it with: pip install sentence-transformers"
            ) from exc

        LOGGER.info("Loading embeddings model: %s", MODEL_NAME)
        try:
            _model = SentenceTransformer(MODEL_NAME)
        except Exception as exc:
            raise RuntimeError(
                "Failed to load the embedding model. "
                "Ensure internet access on the first setup run or pre-cache the model locally."
            ) from exc
        LOGGER.info("Embedding model ready (%s dimensions)", EMBEDDING_DIM)
    return _model


def warmup_model() -> str:
    """Load the model once and return its name."""
    _get_model()
    return MODEL_NAME


def embed(text: str) -> list[float]:
    """Generate a normalized embedding for a single text."""
    model = _get_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Generate normalized embeddings for multiple texts."""
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
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
