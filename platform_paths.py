"""
Cross-platform path & launcher resolution for MCUM (Windows / Linux / macOS).

Single source of truth so the installer, the embedded-PostgreSQL layer, and the
agent registrar never hardcode OS-specific paths. Everything calls `detect()`.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def detect() -> dict:
    """Resolve OS, arch, Python launcher, data/cache dirs, and pgvector ext."""
    system = platform.system()  # 'Windows' | 'Linux' | 'Darwin'
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"

    if system == "Windows":
        base = Path(os.getenv("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / "mcum"
        cache = base / "cache"
        py_launcher = "py"
        pgvector_ext = "vector.dll"
        embedded_pg_pkg = "@embedded-postgres/windows-x64"
    elif system == "Darwin":
        base = _home() / "Library" / "Application Support" / "mcum"
        cache = _home() / "Library" / "Caches" / "mcum"
        py_launcher = "python3"
        pgvector_ext = "vector.dylib"
        embedded_pg_pkg = f"@embedded-postgres/darwin-{arch}"
    else:  # Linux and other POSIX
        base = Path(os.getenv("XDG_DATA_HOME") or (_home() / ".local" / "share")) / "mcum"
        cache = Path(os.getenv("XDG_CACHE_HOME") or (_home() / ".cache")) / "mcum"
        py_launcher = "python3"
        pgvector_ext = "vector.so"
        embedded_pg_pkg = f"@embedded-postgres/linux-{arch}"

    return {
        "os": system,
        "arch": arch,
        "py_launcher": os.getenv("MCUM_PYTHON") or py_launcher,
        "data_dir": base,
        "pg_data_dir": base / "pgdata",
        "model_cache_dir": os.getenv("MCUM_EMBEDDING_CACHE_DIR") or str(cache / "fastembed"),
        "pgvector_ext": pgvector_ext,
        "embedded_pg_pkg": embedded_pg_pkg,
    }


if __name__ == "__main__":
    import json

    info = detect()
    print(json.dumps({k: str(v) for k, v in info.items()}, indent=2))
