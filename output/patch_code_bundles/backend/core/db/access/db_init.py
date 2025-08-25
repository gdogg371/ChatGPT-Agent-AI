# File: v2/backend/core/db/access/db_init.py
from __future__ import annotations

import os
import sqlite3
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

# Import ORM models (each defines its own DeclarativeBase)
from v2.models.introspection_index import Base as IntrospectBase
from v2.models.agent_insights import Base as InsightsBase


# ----------------------------- logging (ASCII-safe) ----------------------------

_logger = logging.getLogger("database")

def _safe_ascii(msg: str) -> str:
    try:
        enc = getattr(getattr(__import__("sys"), "stdout", None), "encoding", None) or "ascii"
        return str(msg).encode(enc, errors="ignore").decode(enc, errors="ignore")
    except Exception:
        return str(msg).encode("ascii", errors="ignore").decode("ascii", errors="ignore")

def _log_info(msg: str) -> None: _logger.info(_safe_ascii(msg))
def _log_warn(msg: str) -> None: _logger.warning(_safe_ascii(msg))
def _log_error(msg: str) -> None: _logger.error(_safe_ascii(msg))


# ----------------------------- repo root + DB URL -----------------------------

def _find_repo_root(start: Optional[Path] = None) -> Path:
    p = (start or Path(__file__).resolve()).parent
    for _ in range(10):
        if (p / "databases").exists() or (p / ".git").exists() or (p / "backend").exists() or (p / "v2").exists():
            return p
        p = p.parent
    return Path.cwd()

_REPO_ROOT = _find_repo_root()
DB_PATH = (_REPO_ROOT / "databases" / "bot_dev.db").resolve()

# Accept ALL common env names (scanner used SQLITE_DB_URL)
_env_db = (
    os.getenv("DB_URL")
    or os.getenv("INTROSPECTION_DB_URL")
    or os.getenv("SQLITE_DB_URL")    # <-- added
)
DB_URL = _env_db or f"sqlite:///{DB_PATH}"


# --------------------------- SQLAlchemy engine/session -------------------------

_engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite:///") else {},
)
_SessionFactory = sessionmaker(bind=_engine)
_ScopedSession = scoped_session(_SessionFactory)
_initialized = False  # guard to run create_all once

# Log exactly where we will write
_log_info(f"[database] Using URL: {DB_URL}")

def _ensure_db_dir():
    if DB_URL.startswith("sqlite:///"):
        path = DB_URL[len("sqlite:///") :]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception as e:
            _log_warn(f"[database] could not create databases dir: {e!r}")


def _create_all_if_needed() -> None:
    global _initialized
    if _initialized:
        return
    _ensure_db_dir()
    try:
        # Each Base carries its own metadata; calling both is idempotent.
        IntrospectBase.metadata.create_all(_engine)
        InsightsBase.metadata.create_all(_engine)
        _initialized = True
        _log_info("[database] Schema ensured (introspection_index, agent_insights).")
    except Exception as e:
        _log_error(f"[database] failed to ensure schema: {e!r}")
        raise


def get_sqlalchemy_session():
    """Return a scoped SQLAlchemy session (ensures schema on first use)."""
    _create_all_if_needed()
    return _ScopedSession()


# --------------------------- Raw sqlite3 connection ----------------------------

_sqlite_conn: Optional[sqlite3.Connection] = None

def get_connection() -> sqlite3.Connection:
    """
    Return a process-global sqlite3 connection. Only valid when using sqlite:///
    """
    global _sqlite_conn
    if not DB_URL.startswith("sqlite:///"):
        raise RuntimeError("get_connection() is only valid for SQLite URLs")

    _ensure_db_dir()
    path = DB_URL[len("sqlite:///") :]

    if _sqlite_conn is None:
        _sqlite_conn = sqlite3.connect(path, check_same_thread=False)
        try:
            _sqlite_conn.execute("PRAGMA journal_mode=WAL;")
            _sqlite_conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        _sqlite_conn.row_factory = sqlite3.Row
        _log_info(f"[database] Connected to SQLite at {Path(path).resolve()}")
    return _sqlite_conn


def close_connection() -> None:
    """Close the process-global sqlite3 connection if open."""
    global _sqlite_conn
    if _sqlite_conn is not None:
        try:
            _sqlite_conn.close()
            _log_info("[database] SQLite connection closed.")
        finally:
            _sqlite_conn = None
