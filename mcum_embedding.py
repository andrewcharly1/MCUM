"""
MCUM embedding backend switcher.

Choose the embedding runtime the way you switch models in a CLI. All semantic
backends use the SAME all-MiniLM-L6-v2 weights, so their 384-dim vectors are
interchangeable (cosine ~1.0) -- switching never requires re-embedding stored
rows. They differ only in runtime cost.

Usage:
    py mcum_embedding.py status            # show current backend + availability
    py mcum_embedding.py list              # list backends and their tradeoffs
    py mcum_embedding.py use onnx          # switch (writes .env and .mcp.json)
    py mcum_embedding.py use sentence-transformers
    py mcum_embedding.py use hash
    py mcum_embedding.py bench [backend]   # measure cold load + RAM of a backend
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

MCUM_ROOT = Path(__file__).resolve().parent
ENV_FILE = MCUM_ROOT / ".env"

# backend -> (canonical_key, import_module_to_probe, blurb)
BACKENDS = {
    "onnx": ("onnx", "fastembed", "ONNX Runtime (fastembed). ~2-3s cold, ~220MB RAM. Recommended."),
    "sentence-transformers": (
        "sentence-transformers",
        "sentence_transformers",
        "PyTorch. ~15-50s cold, ~440MB RAM. Same vectors, heavier.",
    ),
    "hash": ("hash", None, "Deterministic, no semantics, 0 RAM. Tests / ultralight."),
}
ALIASES = {"st": "sentence-transformers", "fastembed": "onnx", "fallback": "hash", "none": "hash"}


def _resolve(name: str) -> str:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in BACKENDS:
        raise SystemExit(
            f"Unknown backend '{name}'. Choose: {', '.join(BACKENDS)} (aliases: {', '.join(ALIASES)})."
        )
    return key


def _installed(backend_key: str) -> bool:
    module = BACKENDS[backend_key][1]
    if module is None:
        return True  # hash is always available
    return importlib.util.find_spec(module) is not None


def _current_backend() -> str:
    # Honor a live env override first, then the persisted .env value.
    env_override = os.getenv("MCUM_EMBEDDING_BACKEND")
    if env_override:
        return _resolve(env_override)
    if ENV_FILE.exists():
        match = re.search(r"^MCUM_EMBEDDING_BACKEND=(.+)$", ENV_FILE.read_text(encoding="utf-8"), re.M)
        if match:
            return _resolve(match.group(1))
    return "onnx"


def _find_mcp_json() -> Path | None:
    base = os.getenv("MCUM_PROJECT_PATH") or ""
    candidates = []
    if base:
        candidates.append(Path(base) / ".mcp.json")
    # Walk up from MCUM root looking for a workspace .mcp.json.
    for parent in MCUM_ROOT.parents:
        candidates.append(parent / ".mcp.json")
    for path in candidates:
        if path.exists():
            return path
    return None


def _write_env(backend_key: str) -> None:
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    if re.search(r"^MCUM_EMBEDDING_BACKEND=.*$", text, re.M):
        text = re.sub(r"^MCUM_EMBEDDING_BACKEND=.*$", f"MCUM_EMBEDDING_BACKEND={backend_key}", text, flags=re.M)
    else:
        text += f"\nMCUM_EMBEDDING_BACKEND={backend_key}\n"
    ENV_FILE.write_text(text, encoding="utf-8")


def _write_mcp_json(backend_key: str) -> Path | None:
    path = _find_mcp_json()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data.get("mcpServers", {}).get("mcum")
        if not isinstance(server, dict):
            return None
        server.setdefault("env", {})["MCUM_EMBEDDING_BACKEND"] = backend_key
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path
    except (ValueError, OSError):
        return None


def cmd_list() -> None:
    print("MCUM embedding backends (all semantic backends share all-MiniLM-L6-v2 vectors):\n")
    current = _current_backend()
    for key, (_, _, blurb) in BACKENDS.items():
        mark = "* " if key == current else "  "
        avail = "installed" if _installed(key) else "NOT installed"
        print(f"{mark}{key:22} [{avail:13}] {blurb}")
    print("\n* = current. Switch with: py mcum_embedding.py use <backend>")


def cmd_status() -> None:
    current = _current_backend()
    print(f"current backend : {current}")
    print(f"installed       : {'yes' if _installed(current) else 'NO -> will fall back to hash'}")
    print(f"cache dir       : {os.getenv('MCUM_EMBEDDING_CACHE_DIR', '(default ~/.cache/fastembed)')}")
    print(f".env            : {ENV_FILE}")
    mcp = _find_mcp_json()
    print(f".mcp.json       : {mcp if mcp else '(not found)'}")


def cmd_use(name: str) -> None:
    key = _resolve(name)
    if not _installed(key):
        module = BACKENDS[key][1]
        print(f"WARNING: backend '{key}' requires '{module}', which is not installed.")
        print(f"         Install it (pip install {module}) or MCUM will fall back to hash.")
    _write_env(key)
    mcp_written = _write_mcp_json(key)
    print(f"OK: MCUM_EMBEDDING_BACKEND set to '{key}' in {ENV_FILE.name}")
    if mcp_written:
        print(f"OK: also updated {mcp_written}")
        print("    Restart Claude Code / the MCP server for the change to take effect.")
    else:
        print("    (no .mcp.json found to update; CLI sessions will use the new backend)")


def cmd_bench(name: str | None) -> None:
    key = _resolve(name) if name else _current_backend()
    os.environ["MCUM_EMBEDDING_BACKEND"] = key
    try:
        import psutil  # type: ignore

        proc = psutil.Process()
    except ImportError:
        proc = None
    import importlib
    import time

    from MCUM.db import embedder  # noqa: WPS433

    importlib.reload(embedder)
    t0 = time.time()
    vec = embedder.embed("error conexion postgresql windows")
    cold = time.time() - t0
    t1 = time.time()
    embedder.embed("segunda consulta caliente")
    warm = time.time() - t1
    print(f"backend     : {key}")
    print(f"model       : {embedder.warmup_model()}")
    print(f"dim         : {len(vec)}")
    print(f"cold load   : {cold:.2f}s")
    print(f"warm encode : {warm*1000:.1f}ms")
    if proc is not None:
        print(f"RSS         : {proc.memory_info().rss / 1024 / 1024:.1f} MB")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch the MCUM embedding backend.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List backends and tradeoffs.")
    sub.add_parser("status", help="Show the current backend.")
    use = sub.add_parser("use", help="Switch backend (writes .env and .mcp.json).")
    use.add_argument("backend", help="onnx | sentence-transformers | hash")
    bench = sub.add_parser("bench", help="Measure cold load + RAM of a backend.")
    bench.add_argument("backend", nargs="?", help="Backend to benchmark (default: current).")

    args = parser.parse_args(argv)
    if args.command == "list":
        cmd_list()
    elif args.command == "status":
        cmd_status()
    elif args.command == "use":
        cmd_use(args.backend)
    elif args.command == "bench":
        cmd_bench(args.backend)
    return 0


if __name__ == "__main__":
    # Make `from MCUM.db import embedder` importable when run directly.
    sys.path.insert(0, str(MCUM_ROOT.parent))
    raise SystemExit(main())
