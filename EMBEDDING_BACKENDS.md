# MCUM Embedding Backends

MCUM stores every embedding in PostgreSQL (`vector(384)` + HNSW via pgvector).
The **backend** is only the runtime that turns text into those 384 numbers. You
pick it the way you switch models in a CLI — and because every semantic backend
uses the **same `all-MiniLM-L6-v2` weights**, their vectors are interchangeable
(cosine ≈ 1.0000, verified). **Switching backends never requires re-embedding
stored rows.**

## The three tiers

| Backend | Runtime | Cold load | RAM (RSS) | Warm encode | Semantics | When |
|---------|---------|-----------|-----------|-------------|-----------|------|
| **`onnx`** ⭐ | ONNX Runtime (`fastembed`) | **~2–4 s** | **~220 MB** | ~20 ms | full | **Default.** Best agility/quality balance. |
| `sentence-transformers` | PyTorch | ~15–50 s | ~440 MB | ~230 ms | full (identical vectors) | Only if already installed / preferred. |
| `hash` | pure Python | ~0 s | ~33 MB | ~0 ms | none (lexical buckets) | Tests, ultralight, or no model available. |

Measured on this machine (Windows 11, Python 3.10). `onnx` and
`sentence-transformers` produce numerically identical vectors, so semantic
search quality is the same — they differ only in cost.

> Why not a persistent "warm sidecar"? With `onnx` cold load at ~3 s, a
> resident model process (which would pin ~220 MB+ of RAM permanently) is not
> worth it. ONNX attacks the root cost (PyTorch import + Hub round-trip) instead
> of papering over it with RAM. Revisit a sidecar only for a dedicated server.

## Switch backend (CLI)

```bash
py .agent/skills/MCUM/mcum_embedding.py status      # current backend + availability
py .agent/skills/MCUM/mcum_embedding.py list        # all backends + tradeoffs
py .agent/skills/MCUM/mcum_embedding.py use onnx    # switch (writes .env AND .mcp.json)
py .agent/skills/MCUM/mcum_embedding.py use sentence-transformers
py .agent/skills/MCUM/mcum_embedding.py use hash
py .agent/skills/MCUM/mcum_embedding.py bench [backend]   # measure cold load + RAM
```

`use` rewrites `MCUM_EMBEDDING_BACKEND` in `MCUM/.env` and, if found, the
workspace `.mcp.json`. **Restart Claude Code / the MCP server** for an MCP-side
change to take effect (the bridge reads the backend from its launch env).

## Configuration (`.env` / `.mcp.json` env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCUM_EMBEDDING_BACKEND` | `onnx` | `onnx` \| `sentence-transformers` \| `hash` (aliases: `st`, `fastembed`). |
| `MCUM_EMBEDDING_CACHE_DIR` | `~/.cache/fastembed` | Stable ONNX model cache (avoids fastembed's volatile Temp dir). |
| `MCUM_EMBEDDING_ALLOW_DOWNLOAD` | unset | Set `=1` for the one-time model download on a fresh machine. |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | Block the Hugging Face Hub network round-trip (the old timeout cause). |
| `HF_HUB_DISABLE_SYMLINKS_WARNING` | `1` | Silence the Windows symlink warning. |

These are set in `MCUM/.env` and injected into MCP child processes by the bridge
(`embeddingEnv()` in `mcum_local_mcp_stdio.mjs`), so every code path — the
embedder, `pattern_discovery`'s direct loader, CLI, and MCP — behaves the same.

## Why offline mode matters (the `search_memory` timeout)

Each MCP call spawns a fresh Python process that cold-loads the model. With the
Hub reachable, the loader made an unauthenticated, latency-variable network
request to check for model updates **even though the model was cached** — that
ballooned cold loads to 67–120 s+ and timed out `mcum_search_memory`. Forcing
offline mode (`HF_HUB_OFFLINE=1` + `local_files_only=True`) made the load
deterministic; moving to `onnx` then cut it to ~3 s. The pgvector query itself
was never the bottleneck (~0.9 s).

## First-time setup / new machine

1. `pip install -r requirements.txt` (installs `fastembed`).
2. One-time model download:
   ```bash
   MCUM_EMBEDDING_ALLOW_DOWNLOAD=1 py .agent/skills/MCUM/mcum_embedding.py bench onnx
   ```
3. Subsequent loads are fully offline from `MCUM_EMBEDDING_CACHE_DIR`.

### Windows note (symlinks)

Without Developer Mode, `huggingface_hub` cannot create the symlinks it uses to
place small files (`config.json`, `tokenizer_config.json`) into the snapshot
dir, so an offline reuse can raise `Could not find config.json`. MCUM
**self-heals** this: `embedder._repair_fastembed_snapshot()` copies the blobs
into the snapshot on load failure and retries once. No manual action needed.

## Packaging (future)

- `onnx` has no PyTorch/CUDA dependency → small, portable wheels, easy to bundle
  in the planned installer alongside PostgreSQL + pgvector.
- Ship the model inside `MCUM_EMBEDDING_CACHE_DIR` so the installer is fully
  offline (no first-run download), with `hash` as the guaranteed fallback.
- Revisit at packaging time (per project decision): confirm cache layout and
  whether to embed the ONNX weights directly in the bundle.
