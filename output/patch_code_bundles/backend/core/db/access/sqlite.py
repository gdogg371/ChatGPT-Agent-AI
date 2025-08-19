import os
import sqlite3
from v1.backend.utils.logger import logger
from backend.core.context.env import get_env_var

DB_PATH = get_env_var("DB_PATH", "/data/agents.db")
_connection = None

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