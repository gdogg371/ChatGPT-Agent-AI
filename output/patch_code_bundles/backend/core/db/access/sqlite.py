# File: v2/backend/core/db/access/sqlite.py
from __future__ import annotations
"""
SQLite helpers (no env lookups, no external context dependencies).

Design
------
- Single source of truth for DB location comes from `db_init.DB_PATH`.
- Shared connection lifecycle is delegated to `db_init.get_connection()` /
  `db_init.close_connection()` to avoid duplicate globals.
- Optional helpers to open an ad-hoc connection to a *different* path (e.g. tests).
- Safe pragmas (WAL/NORMAL) and row factory configured on every connection.
- Extension loading utility guarded behind feature detection.

Why this exists
---------------
Historically this module tried to read variables from `backend.core.context`
and/or environment variables. Those are removed. Import from `db_init` instead.
"""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Any

# --- minimal logger fallback (don’t explode if project logger isn’t present) ---
try:
    from v1.backend.utils.logger import get_logger  # type: ignore
    _logger = get_logger("sqlite")
    def _log_info(msg: str) -> None: _logger.info(msg)
    def _log_warn(msg: str) -> None: _logger.warning(msg)
    def _log_err(msg: str) -> None: _logger.error(msg)
except Exception:  # stdlib fallback
    import logging
    logging.basicConfig(level=logging.INFO)
    _lg = logging.getLogger("sqlite")
    def _log_info(msg: str) -> None: _lg.info(msg)
    def _log_warn(msg: str) -> None: _lg.warning(msg)
    def _log_err(msg: str) -> None: _lg.error(msg)

# Import the canonical DB path and shared lifecycle from db_init.
from v2.backend.core.db.access.db_init import (
    DB_PATH,
    get_connection as _get_shared_connection,
    close_connection as _close_shared_connection,
)

# ------------------------------------------------------------------------------
# Shared connection (delegates to db_init)
# ------------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """
    Return the **shared** project SQLite connection.
    Delegates to db_init.get_connection() and ensures basic pragmas/row_factory.
    """
    conn = _get_shared_connection()
    _ensure_connection_config(conn)
    return conn


def close_connection() -> None:
    """
    Safely close the **shared** SQLite connection.

    AG Coverage:
    - AG-6: CLI shutdown
    - AG-12: Diagnostics restart cleanup
    - AG-21: Daemon thread disposal
    - AG-35: Safe lifecycle finalization
    """
    try:
        _close_shared_connection()
        _log_info("[sqlite] 🔌 Shared SQLite connection closed.")
    except Exception as e:
        _log_warn(f"[sqlite] ⚠ Failed to close shared SQLite connection cleanly: {e}")

# ------------------------------------------------------------------------------
# Ad-hoc (non-shared) connection helpers
# ------------------------------------------------------------------------------

def connect_to(path: str | Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """
    Open a **new, caller-owned** sqlite3 connection to the given path.
    The caller must close() it when done.

    - Ensures parent directory exists.
    - Applies WAL/NORMAL pragmas and Row factory.
    """
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    _ensure_connection_config(conn)
    _log_info(f"[sqlite] ✅ Opened ad-hoc connection at {p}")
    return conn


def close(conn: Optional[sqlite3.Connection]) -> None:
    """Close a caller-owned sqlite3 connection safely."""
    if conn is None:
        return
    try:
        conn.close()
        _log_info("[sqlite] 🔌 Closed ad-hoc SQLite connection.")
    except Exception as e:
        _log_warn(f"[sqlite] ⚠ Failed to close ad-hoc SQLite connection: {e}")

# ------------------------------------------------------------------------------
# Pragmas / configuration
# ------------------------------------------------------------------------------

def _ensure_connection_config(conn: sqlite3.Connection) -> None:
    """Apply standard configuration to a connection (idempotent)."""
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        # Pragmas may fail depending on SQLite build; not fatal.
        pass
    try:
        conn.row_factory = sqlite3.Row
    except Exception:
        pass


def set_pragma(name: str, value: Any, *, connection: Optional[sqlite3.Connection] = None) -> None:
    """
    Set a pragma on the given connection (or the shared connection if None).
    Example: set_pragma("cache_size", -20000)
    """
    conn = connection or get_connection()
    try:
        conn.execute(f"PRAGMA {name}={value};")
    except Exception as e:
        _log_warn(f"[sqlite] ⚠ Failed to set PRAGMA {name}={value!r}: {e}")

# ------------------------------------------------------------------------------
# Convenience execution helpers (shared connection by default)
# ------------------------------------------------------------------------------

def execute(sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
    """Execute a statement on the **shared** connection and return the cursor."""
    cur = get_connection().cursor()
    cur.execute(sql, params or [])
    return cur


def executemany(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
    """Execute many on the **shared** connection and return the cursor."""
    cur = get_connection().cursor()
    cur.executemany(sql, seq_of_params)
    return cur


def query_all(sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
    """Run a SELECT and return all rows (sqlite3.Row mapping)."""
    cur = execute(sql, params or [])
    return cur.fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> Optional[sqlite3.Row]:
    """Run a SELECT and return a single row or None."""
    cur = execute(sql, params or [])
    return cur.fetchone()

# ------------------------------------------------------------------------------
# Extension loading
# ------------------------------------------------------------------------------

def load_extension(lib_path: str | Path, *, connection: Optional[sqlite3.Connection] = None) -> bool:
    """
    Load a SQLite extension library (.dll/.so/.dylib).

    Returns True on success, False if loading is unsupported or failed.
    """
    conn = connection or get_connection()
    try:
        # Some Python builds expose enable_load_extension; guard via hasattr
        if hasattr(conn, "enable_load_extension"):
            conn.enable_load_extension(True)  # type: ignore[attr-defined]
        conn.load_extension(str(lib_path))
        _log_info(f"[sqlite] 🧩 Loaded extension: {lib_path}")
        return True
    except Exception as e:
        _log_warn(f"[sqlite] ⚠ Failed to load extension {lib_path}: {e}")
        return False

# ------------------------------------------------------------------------------
# Module exports
# ------------------------------------------------------------------------------

__all__ = [
    "DB_PATH",
    "get_connection",
    "close_connection",
    "connect_to",
    "close",
    "execute",
    "executemany",
    "query_all",
    "query_one",
    "set_pragma",
    "load_extension",
]
