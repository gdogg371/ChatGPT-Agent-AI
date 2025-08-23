from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

# --- minimal logger fallback (don’t explode if project logger isn’t present) ---
try:  # project logger (if available)
    from v1.backend.utils.logger import get_logger  # type: ignore
    _logger = get_logger("database")
    def _log_info(msg: str) -> None: _logger.info(msg)
    def _log_warn(msg: str) -> None: _logger.warning(msg)
    def _log_err(msg: str) -> None: _logger.error(msg)
except Exception:  # stdlib fallback
    import logging
    logging.basicConfig(level=logging.INFO)
    _lg = logging.getLogger("database")
    def _log_info(msg: str) -> None: _lg.info(msg)
    def _log_warn(msg: str) -> None: _lg.warning(msg)
    def _log_err(msg: str) -> None: _lg.error(msg)

# ------------------------------------------------------------------------------
# Repo-aware pathing and URL handling
# ------------------------------------------------------------------------------

def _repo_root() -> Path:
    p = Path(__file__).resolve().parent
    # Walk up a few levels looking for markers
    for _ in range(8):
        if (p / ".git").exists() or (p / "databases").exists() or (p / "software").exists():
            return p
        p = p.parent
    # Fallback: reasonably high up
    return Path(__file__).resolve().parents[4]

_REPO_ROOT = _repo_root()

def _from_sqlite_url(url: str) -> Optional[str]:
    """Return filesystem path from sqlite URL; supports file and :memory:."""
    s = (url or "").strip()
    if not s:
        return None
    if s.startswith("sqlite:///:memory:"):
        return ":memory:"
    if s.startswith("sqlite:///"):
        return s[len("sqlite:///"):]
    return None

def _ensure_dir_for(pathlike: str) -> None:
    p = Path(pathlike)
    if p != Path(":memory:"):
        p.parent.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------------
# Resolve DB location
# Priority:
#   1) SQLITE_DB_URL (sqlalchemy-style, e.g. sqlite:///C:/.../bot_dev.db)
#   2) DB_URL (same as above)
#   3) DB_PATH (plain filesystem path)
#   4) default: <repo>/databases/bot_dev.db
# ------------------------------------------------------------------------------

_env_sql_url = os.getenv("SQLITE_DB_URL") or os.getenv("DB_URL") or ""
_env_path     = os.getenv("DB_PATH") or ""

_url_path = _from_sqlite_url(_env_sql_url) if _env_sql_url else None
if _url_path:
    _DB_PATH = _url_path
elif _env_path:
    _DB_PATH = _env_path
else:
    _DB_PATH = str((_REPO_ROOT / "databases" / "bot_dev.db").resolve())

# Exported constant used by other modules
DB_PATH: str = _DB_PATH

# Build the SQLAlchemy URL from DB_PATH (unless it's :memory:)
if DB_PATH == ":memory:":
    _SQLA_URL = "sqlite:///:memory:"
else:
    _ensure_dir_for(DB_PATH)
    # Normalize Windows backslashes to forward slashes for SQLAlchemy URL
    _SQLA_URL = "sqlite:///" + Path(DB_PATH).as_posix()

# ------------------------------------------------------------------------------
# SQLAlchemy session factory
# ------------------------------------------------------------------------------

_engine = create_engine(_SQLA_URL, connect_args={"check_same_thread": False})
SessionFactory = sessionmaker(bind=_engine)
ScopedSession = scoped_session(SessionFactory)

def get_sqlalchemy_session():
    """
    Returns a new SQLAlchemy session object.
    Use as a context manager or close() manually.
    """
    return ScopedSession()

# ------------------------------------------------------------------------------
# Raw sqlite3 connection (shared)
# ------------------------------------------------------------------------------

_connection: Optional[sqlite3.Connection] = None

def get_connection() -> sqlite3.Connection:
    """
    Return a thread-safe, globally shared SQLite connection.
    - Ensures DB directory exists
    - Enables WAL + NORMAL sync
    - Row factory = sqlite3.Row
    """
    global _connection
    if _connection is not None:
        return _connection

    try:
        if DB_PATH != ":memory:":
            _ensure_dir_for(DB_PATH)
        _connection = sqlite3.connect(DB_PATH if DB_PATH else ":memory:", check_same_thread=False)
        try:
            _connection.execute("PRAGMA journal_mode=WAL;")
            _connection.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        _connection.row_factory = sqlite3.Row
        _log_info(f"[database] ✅ Connected to SQLite at {DB_PATH or ':memory:'}")
    except Exception as e:
        _log_err(f"[database] ❌ Failed to connect: {e}")
        raise RuntimeError("Critical: Unable to initialize SQLite DB") from e

    return _connection

def close_connection() -> None:
    """Safely close the active SQLite connection, if it exists."""
    global _connection
    if _connection is not None:
        try:
            _connection.close()
            _log_info("[database] SQLite connection closed.")
        except Exception as e:
            _log_warn(f"[database] ⚠ Failed to close DB connection cleanly: {e}")
        finally:
            _connection = None
