#v2/backend/core/utils/db/init_sqlite_dev.py
"""
SQLite development database initializer (YAML-driven, no hardcoded paths).

- Reads DB coordinates and schema directory *strictly* from the centralized loader.
- Supports only SQLite for this tool; errors clearly if another backend is configured.
- Applies SQL files in lexicographic order from the configured schema directory.
- For each SQL file, attempts to parse the primary table name:
    * If a table name is parsed and the table already exists => skip file.
    * If no table can be parsed => apply file (best-effort).
- Strips manual BEGIN/COMMIT in SQL scripts to avoid nested transaction errors.

Usage:
    python -m v2.backend.core.utils.db.init_sqlite_dev
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from v2.backend.core.configuration.loader import ConfigError, get_db


def _is_sqlite_url(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith("sqlite:///")


def _sqlite_path_from_url(url: str) -> Path:
    # Accept both native and POSIX separators in URL tail
    tail = url[len("sqlite:///") :]
    return Path(tail).resolve()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _list_sql_files(schema_dir: Path) -> List[Path]:
    files = sorted(p for p in schema_dir.glob("*.sql") if p.is_file())
    return files


def _parse_primary_table_name(sql_text: str) -> Optional[str]:
    """
    Very tolerant parser for "CREATE TABLE [IF NOT EXISTS] <name>".
    Ignores quoting styles and optional schema qualifiers.
    """
    # Remove comments (naive)
    cleaned = re.sub(r"--.*?$", "", sql_text, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)

    # Look for CREATE TABLE ... <name>
    m = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(?:\w+)\s*\.\s*)?[`\"'\[]?([A-Za-z_][A-Za-z0-9_]*)[`\"'\]]?",
        cleaned,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _strip_manual_tx(sql_script: str) -> str:
    # Avoid nested transaction issues for scripts containing BEGIN/COMMIT.
    s = re.sub(r"\bBEGIN\s*;\s*", "", sql_script, flags=re.IGNORECASE)
    s = re.sub(r"\bCOMMIT\s*;\s*", "", s, flags=re.IGNORECASE)
    return s


def _db_exists(path: Path) -> bool:
    return path.exists()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,)
    )
    return cur.fetchone() is not None


def _apply_sql_file(conn: sqlite3.Connection, sql_path: Path) -> None:
    text = sql_path.read_text(encoding="utf-8")
    cleaned = _strip_manual_tx(text)
    conn.executescript(cleaned)


def init_database() -> None:
    """
    Initialize the SQLite database using SQL scripts from the configured schema_dir.
    """
    db_cfg = get_db()

    if not _is_sqlite_url(db_cfg.url):
        raise ConfigError(
            "init_sqlite_dev supports only SQLite. Configure db.yml with either "
            "'path' or 'url' beginning with 'sqlite:///' for development."
        )

    db_path = _sqlite_path_from_url(db_cfg.url)
    schema_dir = db_cfg.schema_dir
    if schema_dir is None:
        raise ConfigError("db.yml must specify 'schema_dir' for init_sqlite_dev.")

    if not schema_dir.is_dir():
        raise ConfigError(f"schema_dir does not exist: {schema_dir}")

    # Ensure DB directory exists
    _ensure_parent(db_path)

    created = False
    if not _db_exists(db_path):
        print(f"[sqlite:init] Creating DB at: {db_path}")
        created = True
    else:
        print(f"[sqlite:init] Using existing DB: {db_path}")

    # Open connection and apply migrations conditionally
    conn = sqlite3.connect(str(db_path))
    try:
        files = _list_sql_files(schema_dir)
        if not files:
            raise ConfigError(f"No .sql files found in schema_dir: {schema_dir}")

        for sql_file in files:
            sql_text = sql_file.read_text(encoding="utf-8")
            table = _parse_primary_table_name(sql_text)

            if table and _table_exists(conn, table):
                print(f"[sqlite:init] ✔ Table '{table}' exists — skip {sql_file.name}")
                continue

            print(
                f"[sqlite:init] ➕ Applying {sql_file.name}"
                + (f" (parsed table: {table})" if table else " (no table parsed)")
            )
            _apply_sql_file(conn, sql_file)

        # Autocommit is the default for executescript; ensure durability
        conn.commit()
    finally:
        conn.close()

    if created:
        print("[sqlite:init] SQLite database initialized successfully.")
    else:
        print("[sqlite:init] SQLite database checked and up-to-date.")


if __name__ == "__main__":
    init_database()

