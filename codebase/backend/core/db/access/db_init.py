# backend/core/database.py

"""
SQLite Database Initialization and Connection Manager

AG Coverage:
- AG-1: Patch loop state recording
- AG-2: Memory CRUD persistence
- AG-5: Goal/task planning I/O
- AG-6: CLI-triggered DB mutations
- AG-8: Patch plan and state metadata
- AG-10: Semantic embedding storage
- AG-12: Diagnostics and recovery storage
- AG-17: Persistent agents state tracking
- AG-18: Forensic audit trail recording
- AG-21: Daemon-threaded DB concurrency
- AG-27: Skill registration and lookup
- AG-33: Immutable audit log storage
- AG-35: Deterministic, WAL-safe connection management

This module provides a shared, global SQLite connection used across all
agents subsystems. It ensures thread-safe WAL mode, proper path setup,
and safe close behavior.

DB path is resolved from `context.get_env_var("DB_PATH", "/data/agents.db")`.
"""

import sqlite3
import os
from v1.backend.utils.logger import get_logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

#def _get_env_var(key: str, default: str = "") -> str:
    #from backend.core.context.context import get_env_var as _get
    #return _get(key, default)

BASE_DIR = "/databases\\"
DB_PATH = os.path.join(BASE_DIR, "bot_dev.db")

#DB_PATH = get_env_var("DB_PATH", "/data/agents.db")
logger = get_logger("database")

_connection = None  # Global SQLite singleton

# 🔧 Replace these with your actual environment or `.env` loader
#DB_HOST = os.getenv("DB_HOST", "localhost")
#DB_PORT = os.getenv("DB_PORT", "5432")
#DB_NAME = os.getenv("DB_NAME", "your_db")
#DB_USER = os.getenv("DB_USER", "your_user")
#DB_PASS = os.getenv("DB_PASS", "your_password")

#DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# ⚙️ SQLAlchemy engine setup (NullPool = safer for CLI tools and scripts)
#engine = create_engine(DATABASE_URL, poolclass=NullPool)

# 🔁 Session factory (non-threaded, scoped per use)
#SessionFactory = sessionmaker(bind=engine)
#ScopedSession = scoped_session(SessionFactory)



engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

SessionFactory = sessionmaker(bind=engine)
ScopedSession = scoped_session(SessionFactory)



# ✅ Main ORM session accessor
def get_sqlalchemy_session():
    """
    Returns a new SQLAlchemy session object.
    Use with `with` or manually call `close()` when done.
    """
    return ScopedSession()


def get_connection() -> sqlite3.Connection:
    """
    Return a thread-safe, globally shared SQLite connection.

    AG Coverage:
    - AG-1, AG-2, AG-5, AG-6, AG-8, AG-10, AG-12, AG-17, AG-18, AG-21, AG-27, AG-33, AG-35

    Returns:
        sqlite3.Connection: Active SQLite connection with WAL and RowFactory enabled.

    Behavior:
        - Initializes connection only once (singleton pattern)
        - Enables WAL mode for concurrent reads/writes
        - Ensures DB directory exists before connecting
        - Configures RowFactory for dict-like cursor access
        - Logs connection events and path resolution

    Raises:
        RuntimeError: If the DB connection fails (prevents agents startup).
    """
    global _connection

    if _connection is not None:
        return _connection

    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.execute("PRAGMA journal_mode=WAL;")
        _connection.execute("PRAGMA synchronous=NORMAL;")
        _connection.row_factory = sqlite3.Row
        logger.info(f"[database] ✅ Connected to SQLite database at {DB_PATH}")
    except Exception as e:
        logger.error(f"[database] ❌ Failed to connect to database: {e}")
        raise RuntimeError("Critical: Unable to initialize SQLite DB")

    return _connection

def close_connection():
    """
    Safely close the active SQLite connection, if it exists.

    AG Coverage:
    - AG-6: CLI shutdown
    - AG-12: Diagnostics restart cleanup
    - AG-21: Daemon thread disposal
    - AG-35: Safe lifecycle finalization

    Notes:
        - Can be called multiple times without error
        - Logs success or failure cleanly
        - Resets global connection state to None
    """
    global _connection

    if _connection is not None:
        try:
            _connection.close()
            logger.info("[database] 🔌 SQLite connection closed.")
        except Exception as e:
            logger.warning(f"[database] ⚠ Failed to close DB connection cleanly: {e}")
        finally:
            _connection = None
