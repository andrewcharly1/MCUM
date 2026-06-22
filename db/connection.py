"""
MCUM — Motor Cerebral Ultra Multiversal
db/connection.py — Modulo de Conexion a PostgreSQL

Maneja la conexion al PostgreSQL local usando psycopg3 con connection pooling.
Carga credenciales desde .env automaticamente.
Poka-Yoke: falla rapido si la conexion no esta disponible.
"""

import atexit
import os
import platform
import socket
import struct
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from urllib.parse import quote, urlsplit, urlunsplit

# Carga .env desde el directorio MCUM/ (parent del modulo db/)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=_env_path)
except ImportError:
    # dotenv no instalado — continuar con variables de entorno del sistema
    pass

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    print("ERROR MCUM: psycopg3 no instalado.")
    print("Instalar con: pip install psycopg[binary]")
    sys.exit(1)

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None  # type: ignore[assignment,misc]


# -----------------------------------------
# CONFIGURACION (desde .env o variables de entorno)
# -----------------------------------------
def _running_in_wsl() -> bool:
    if os.name == "nt":
        return False
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    release = platform.release().lower()
    return "microsoft" in release or "wsl" in release


def _detect_wsl_host_gateway() -> str | None:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                destination = fields[1]
                gateway = fields[2]
                if destination != "00000000":
                    continue
                return socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
    except (OSError, ValueError, StopIteration):
        return None
    return None


def _resolve_runtime_db_host(raw_host: str) -> str:
    host = str(raw_host or "").strip() or "localhost"
    if not _running_in_wsl():
        return host
    explicit_host = str(os.getenv("DB_HOST_WSL", "")).strip()
    if explicit_host:
        return explicit_host
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return _detect_wsl_host_gateway() or host
    return host


def _rewrite_database_url_host(raw_url: str, resolved_host: str) -> str:
    conn_url = str(raw_url or "").strip()
    if not conn_url:
        return conn_url
    try:
        parsed = urlsplit(conn_url)
    except ValueError:
        return conn_url
    if not parsed.scheme or not parsed.hostname:
        return conn_url
    original_host = str(parsed.hostname or "").strip()
    if original_host.lower() not in {"localhost", "127.0.0.1", "::1"}:
        return conn_url

    auth = ""
    if parsed.username:
        auth = quote(parsed.username, safe="")
        if parsed.password:
            auth = f"{auth}:{quote(parsed.password, safe='')}"
        auth = f"{auth}@"

    host_render = resolved_host
    if ":" in host_render and not host_render.startswith("["):
        host_render = f"[{host_render}]"

    netloc = f"{auth}{host_render}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _database_identity(raw_url: str) -> tuple[str, int, str]:
    """Return a credential-free identity suitable for safety comparisons."""
    parsed = urlsplit(str(raw_url or "").strip())
    return (
        str(parsed.hostname or "").lower(),
        int(parsed.port or 5432),
        str(parsed.path or "").lstrip("/").lower(),
    )


def _resolve_database_runtime(
    operational_url: str,
    test_url: str,
    *,
    pytest_active: bool,
) -> tuple[str, bool]:
    """Select the test database or force accidental operational access read-only."""
    operational = str(operational_url or "").strip()
    isolated = str(test_url or "").strip()
    if not pytest_active:
        return operational, False
    if not isolated:
        return operational, True
    if _database_identity(operational) == _database_identity(isolated):
        raise RuntimeError(
            "MCUM_TEST_DATABASE_URL must not point to the operational database"
        )
    return isolated, False


_RAW_DB_HOST = os.getenv("DB_HOST", "localhost")
DB_HOST     = _resolve_runtime_db_host(_RAW_DB_HOST)
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME", "postgres")
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

def _build_local_database_url() -> str:
    auth = quote(DB_USER, safe="")
    if DB_PASSWORD:
        auth = f"{auth}:{quote(DB_PASSWORD, safe='')}"
    return f"postgresql://{auth}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# String de conexion completo. Las credenciales reales viven solo en .env local.
_RAW_DATABASE_URL = os.getenv("DATABASE_URL", "")
_OPERATIONAL_DATABASE_URL = (
    _rewrite_database_url_host(_RAW_DATABASE_URL, DB_HOST)
    if _RAW_DATABASE_URL
    else _build_local_database_url()
)
_PYTEST_ACTIVE = str(os.getenv("MCUM_PYTEST_ACTIVE") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_RAW_TEST_DATABASE_URL = os.getenv("MCUM_TEST_DATABASE_URL", "")
DATABASE_URL, PYTEST_OPERATIONAL_READ_ONLY = _resolve_database_runtime(
    _OPERATIONAL_DATABASE_URL,
    _rewrite_database_url_host(_RAW_TEST_DATABASE_URL, DB_HOST)
    if _RAW_TEST_DATABASE_URL
    else "",
    pytest_active=_PYTEST_ACTIVE,
)

# Pool sizing
POOL_MIN = int(os.getenv("MCUM_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("MCUM_POOL_MAX", "10"))

# Opciones de conexion (Poka-Yoke: encoding UTF-8 siempre)
_CONNECTION_OPTIONS = "-c client_encoding=UTF8"
if PYTEST_OPERATIONAL_READ_ONLY:
    _CONNECTION_OPTIONS += " -c default_transaction_read_only=on"

_CONNECTION_KWARGS = {
    "conninfo"   : DATABASE_URL,
    "row_factory": dict_row,
    "options"    : _CONNECTION_OPTIONS,
}


# -----------------------------------------
# PGVECTOR TYPE REGISTRATION
# -----------------------------------------
def _configure_connection(conn: psycopg.Connection) -> None:
    """Register pgvector types on a connection if available."""
    try:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    except (ImportError, Exception):
        pass


# -----------------------------------------
# CONNECTION POOL (singleton, lazy init)
# -----------------------------------------
_pool: "ConnectionPool | None" = None
_pool_lock = threading.Lock()


def _get_pool() -> "ConnectionPool":
    """Return the singleton ConnectionPool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        if ConnectionPool is None:
            raise ImportError(
                "psycopg_pool no instalado. Instalar con: pip install psycopg_pool"
            )
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            kwargs={"row_factory": dict_row, "options": _CONNECTION_OPTIONS},
            configure=_configure_connection,
            open=True,
        )
        return _pool


def shutdown_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(shutdown_pool)


# -----------------------------------------
# CONEXION DIRECTA (sin pool, caller maneja lifecycle)
# -----------------------------------------
def get_connection() -> psycopg.Connection:
    """
    Retorna una conexion activa a PostgreSQL.
    El caller es responsable de cerrarla con .close().
    Prefiere usar get_db() como context manager.

    Raises:
        ConnectionError: Si no puede conectar a PostgreSQL.
    """
    try:
        conn = psycopg.connect(**_CONNECTION_KWARGS)
        _configure_connection(conn)
        return conn
    except psycopg.OperationalError as e:
        raise ConnectionError(
            f"MCUM no puede conectar a PostgreSQL ({DB_HOST}:{DB_PORT}/{DB_NAME}). "
            f"Verificar que el servicio esta activo.\n"
            f"Error: {e}"
        ) from e


# -----------------------------------------
# CONTEXT MANAGERS
# -----------------------------------------
@contextmanager
def get_db() -> Generator[psycopg.Connection, None, None]:
    """
    Context manager para conexiones seguras a PostgreSQL.
    Usa el connection pool si esta disponible, sino cae a conexion directa.
    Auto-commit en success, auto-rollback en excepcion.

    Uso:
        with get_db() as conn:
            conn.execute("SELECT 1")
    """
    if ConnectionPool is not None:
        pool = _get_pool()
        with pool.connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    else:
        conn = None
        try:
            conn = psycopg.connect(**_CONNECTION_KWARGS)
            _configure_connection(conn)
            yield conn
            conn.commit()
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()


@contextmanager
def get_cursor(conn: psycopg.Connection):
    """
    Context manager para cursores con dict_row.

    Uso:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute("SELECT * FROM ...")
    """
    cur = conn.cursor(row_factory=dict_row)
    try:
        yield cur
    finally:
        cur.close()


# -----------------------------------------
# VERIFICACION DE SALUD DEL SISTEMA
# -----------------------------------------
def health_check() -> dict:
    """
    Verifica el estado de la conexion y los schemas del MCUM.
    Retorna un dict con el estado de cada componente.

    Returns:
        {
            "connected": bool,
            "schemas": {"core_brain": bool, "project_registry": bool},
            "pgvector": bool,
            "pgvector_installed": bool,
            "postgres_version": str | None,
            "pool_active": bool,
            "error": str | None
        }
    """
    result = {
        "connected"         : False,
        "schemas"           : {"core_brain": False, "project_registry": False},
        "pgvector"          : False,
        "pgvector_installed": False,
        "postgres_version"  : None,
        "pool_active"       : ConnectionPool is not None and _pool is not None,
        "error"             : None,
    }

    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:

                # Verificar conexion y version
                cur.execute("SELECT version()")
                row = cur.fetchone()
                result["connected"]        = True
                result["postgres_version"] = row["version"] if row else "unknown"

                # Verificar schemas
                cur.execute("""
                    SELECT schema_name
                    FROM information_schema.schemata
                    WHERE schema_name IN ('core_brain', 'project_registry')
                """)
                schemas_found = {row["schema_name"] for row in cur.fetchall()}
                result["schemas"]["core_brain"]       = "core_brain" in schemas_found
                result["schemas"]["project_registry"] = "project_registry" in schemas_found

                # Verificar pgvector disponible
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
                    ) AS available
                """)
                row = cur.fetchone()
                result["pgvector"] = bool(row["available"]) if row else False

                # Verificar pgvector instalado activamente
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_extension WHERE extname = 'vector'
                    ) AS installed
                """)
                row = cur.fetchone()
                result["pgvector_installed"] = bool(row["installed"]) if row else False

    except ConnectionError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"Error inesperado: {e}"

    # Update pool status after potential pool creation
    result["pool_active"] = ConnectionPool is not None and _pool is not None

    return result


# -----------------------------------------
# CLI DE DIAGNOSTICO
# -----------------------------------------
if __name__ == "__main__":
    print("MCUM — Verificacion de conexion PostgreSQL")
    print("-" * 50)

    status = health_check()

    if status["connected"]:
        print(f"  OK Conectado: {status['postgres_version'][:50]}...")
        print(f"   Schema core_brain:       {'OK' if status['schemas']['core_brain'] else 'NO instalado'}")
        print(f"   Schema project_registry: {'OK' if status['schemas']['project_registry'] else 'NO instalado'}")
        print(f"   pgvector disponible:     {'OK' if status['pgvector'] else 'No disponible'}")
        print(f"   pgvector instalado:      {'OK' if status['pgvector_installed'] else 'No instalado'}")
        print(f"   Connection pool:         {'OK' if status['pool_active'] else 'No activo (psycopg_pool no instalado)'}")

        if not status["schemas"]["core_brain"]:
            print("\n   Para instalar el schema MCUM:")
            print("   psql -U postgres -d postgres -f db/schema.sql")
    else:
        print(f"  ERROR No se puede conectar a PostgreSQL")
        print(f"   Error: {status['error']}")
        print(f"\n   Verificar:")
        print(f"   1. PostgreSQL esta corriendo (services.msc)")
        print(f"   2. Credenciales en .env son correctas")
        print(f"   3. Host: {DB_HOST}, Puerto: {DB_PORT}, DB: {DB_NAME}")
