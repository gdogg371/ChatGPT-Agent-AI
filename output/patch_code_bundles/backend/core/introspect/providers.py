# File: v2/backend/core/introspect/providers.py
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from v2.backend.core.spine.contracts import Artifact, Task


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _problem(
    uri: str,
    code: str,
    message: str,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri=uri,
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": dict(details or {}),
                }
            },
        )
    ]


# ---------------------------------------------------------------------------
# SQLite helpers (Windows/Unix friendly)
# ---------------------------------------------------------------------------

def _open_sqlite(url_or_path: str) -> sqlite3.Connection:
    """
    Accept either:
      - sqlite:///C:/path/to.db
      - sqlite:///:memory:
      - plain file path (C:\\path\\to.db or /path/to.db)
    """
    s = (url_or_path or "").strip()
    if not s:
        raise ValueError("empty database URL/path")

    if s.startswith("sqlite:///:memory:"):
        con = sqlite3.connect(":memory:")
    elif s.startswith("sqlite:///"):
        con = sqlite3.connect(s[len("sqlite:///"):])
    else:
        # treat as a plain filesystem path
        con = sqlite3.connect(s)

    con.row_factory = sqlite3.Row
    return con


def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    cols: List[str] = []
    for r in con.execute(f'PRAGMA table_info("{table}")'):
        cols.append(r["name"])
    return cols


def _ensure_min_schema(con: sqlite3.Connection, table: str) -> None:
    """
    Create a minimal, compatible 'introspection_index' table if it doesn't exist.
    Safe on Windows/macOS/Linux. No effect if table already exists.
    """
    ddl = f"""
    CREATE TABLE IF NOT EXISTS "{table}" (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        filepath         TEXT NOT NULL,
        symbol_type      TEXT,
        name             TEXT,
        lineno           INTEGER,
        description      TEXT,
        unique_key_hash  TEXT,
        status           TEXT
    );
    """
    with con:
        con.executescript(ddl)


# ---------------------------------------------------------------------------
# fetch_v1
# ---------------------------------------------------------------------------

def fetch_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: introspect.fetch.v1

    Payload (compatible keys):
      url | sqlalchemy_url:   "sqlite:///path/to.db"  (or plain path)
      table | sqlalchemy_table: str = "introspection_index"
      where: { status: "todo" }  OR  status_filter: "todo"
      limit | max_rows: int
      order_by: "id ASC"
      run: bool
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/introspect.fetch.v1", {"result": []})

    url = str(p.get("url") or p.get("sqlalchemy_url") or "")
    table = str(p.get("table") or p.get("sqlalchemy_table") or "introspection_index")
    order_by = str(p.get("order_by") or "id ASC")
    limit = p.get("limit", p.get("max_rows"))

    # unify where/status
    where = p.get("where") or {}
    status_filter = p.get("status_filter")
    if status_filter and isinstance(where, dict):
        where = dict(where)
        where["status"] = status_filter
    elif not isinstance(where, dict):
        where = {}

    if not url:
        return _problem("spine://problem/introspect.fetch.v1", "ConfigError", "database URL/path is required")

    try:
        con = _open_sqlite(url)
    except Exception as e:
        return _problem("spine://problem/introspect.fetch.v1", "ConfigError", str(e))

    # Ensure table exists (no-op if already present)
    try:
        if not _table_columns(con, table):
            _ensure_min_schema(con, table)
    except Exception as e:
        return _problem("spine://problem/introspect.fetch.v1", "SchemaError", f"{e!r}")

    sql = [
        f'SELECT id, filepath, symbol_type, name, lineno, description, unique_key_hash FROM "{table}"'
    ]
    params: List[Any] = []
    if isinstance(where, dict) and where.get("status"):
        sql.append("WHERE status = ?")
        params.append(where["status"])
    if order_by:
        sql.append("ORDER BY " + order_by)
    if isinstance(limit, int) and limit > 0:
        sql.append("LIMIT ?")
        params.append(limit)
    q = " ".join(sql)

    rows: List[Dict[str, Any]] = []
    try:
        with con:
            for row in con.execute(q, params):
                rows.append(
                    {
                        "id": row["id"],
                        "filepath": row["filepath"],
                        "lineno": row["lineno"],
                        "name": row["name"],
                        "symbol_type": row["symbol_type"],
                        "description": row["description"],
                        "unique_key_hash": row["unique_key_hash"],
                    }
                )
    except Exception as e:
        return _problem("spine://problem/introspect.fetch.v1", "DbError", f"fetch failed: {e!r}")
    finally:
        try:
            con.close()
        except Exception:
            pass

    return _result("spine://result/introspect.fetch.v1", {"result": rows})


# ---------------------------------------------------------------------------
# write_v1 (column-flexible, upsert-by-ignore)
# ---------------------------------------------------------------------------

def write_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: introspect.write.v1

    Payload (compatible keys):
      url | sqlalchemy_url:   "sqlite:///path/to.db"  (or plain path)
      table | sqlalchemy_table: str = "introspection_index"
      rows | items: [
        {filepath, symbol_type, name, lineno, description, unique_key_hash, status?},
        ...
      ]
      run:  bool

    Behavior:
      - Creates a minimal table if missing.
      - Inserts available fields; ignores unknown fields.
      - Uses INSERT OR IGNORE; does not error on duplicates.
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/introspect.write.v1", {"result": {"written": 0}})

    url = str(p.get("url") or p.get("sqlalchemy_url") or "")
    table = str(p.get("table") or p.get("sqlalchemy_table") or "introspection_index")
    rows_in = p.get("rows", p.get("items", []))

    if not url:
        return _problem("spine://problem/introspect.write.v1", "ConfigError", "database URL/path is required")
    if not isinstance(rows_in, list):
        return _problem("spine://problem/introspect.write.v1", "InvalidPayload", "rows/items must be a list")

    try:
        con = _open_sqlite(url)
    except Exception as e:
        return _problem("spine://problem/introspect.write.v1", "ConfigError", str(e))

    try:
        # Ensure minimal schema exists so we always have writable columns.
        if not _table_columns(con, table):
            _ensure_min_schema(con, table)

        cols = _table_columns(con, table)

        # permissible fields we know about (will intersect with actual table cols)
        preferred = [
            "filepath",
            "symbol_type",
            "name",
            "lineno",
            "description",
            "unique_key_hash",
            "status",
        ]
        insert_cols = [c for c in preferred if c in cols]
        if not insert_cols:
            return _problem(
                "spine://problem/introspect.write.v1",
                "SchemaError",
                f'no writable columns in "{table}"',
            )

        placeholders = ", ".join(["?"] * len(insert_cols))
        col_list = ", ".join([f'"{c}"' for c in insert_cols])
        sql = f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({placeholders})'

        written = 0
        with con:
            for r in rows_in:
                if not isinstance(r, dict):
                    continue
                vals = []
                for c in insert_cols:
                    v = r.get(c)
                    if c == "lineno":
                        try:
                            v = int(v) if v is not None else None
                        except Exception:
                            v = None
                    vals.append(v)
                con.execute(sql, vals)
                written += 1

        return _result("spine://result/introspect.write.v1", {"result": {"written": written}})
    except Exception as e:
        return _problem("spine://problem/introspect.write.v1", "DbError", f"write failed: {e!r}")
    finally:
        try:
            con.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# read_docstrings_v1 (run analyzer; supports exclude_dirs / excluded_dirs)
# ---------------------------------------------------------------------------

def read_docstrings_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: introspect.read_docstrings.v1

    Walk the repository, summarize existing docstrings via the analyzer,
    and write rows to the introspection_index using DocstringWriter.

    Payload:
      run: bool = true
      scan_root: str   (repo path to scan; defaults to cwd if omitted)
      db_url: str      (sqlite:///... or plain path; optional — uses existing env if omitted)
      exclude_dirs | excluded_dirs: [ "Archive", "output", ... ]  (optional; merged into analyzer.EXCLUDED_DIRS)

    Returns:
      Result meta: { "result": { "files_scanned": N, "rows_written": M,
                                 "skipped": K, "failed": F, "llm": L } }
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/introspect.read_docstrings.v1", {"result": {"files_scanned": 0, "rows_written": 0}})

    scan_root = Path(str(p.get("scan_root") or Path.cwd())).resolve()
    db_url = str(p.get("db_url") or os.getenv("SQLITE_DB_URL") or "")
    # accept either key spelling
    exclude_dirs = p.get("exclude_dirs")
    if exclude_dirs is None:
        exclude_dirs = p.get("excluded_dirs")
    exclude_dirs = exclude_dirs or []

    if not scan_root.exists():
        return _problem("spine://problem/introspect.read_docstrings.v1", "ConfigError", f"scan_root not found: {scan_root}")

    # Point writer at the requested DB (no external env required)
    if db_url:
        os.environ["SQLITE_DB_URL"] = db_url

    # Heavy import only when needed
    try:
        from v2.backend.core.introspect.read_docstrings import DocStringAnalyzer  # type: ignore
    except Exception as e:
        return _problem("spine://problem/introspect.read_docstrings.v1", "ImportError", f"failed to import analyzer: {e}")

    analyzer = DocStringAnalyzer()

    # Override scan root and merge exclusions
    analyzer.ROOT_DIR = str(scan_root)
    analyzer.root_path = scan_root
    if isinstance(exclude_dirs, (list, tuple)):
        analyzer.EXCLUDED_DIRS |= {str(x) for x in exclude_dirs if isinstance(x, str) and x}

    # Run the traversal (prints progress itself)
    stats = analyzer.traverse_and_write()

    return _result(
        "spine://result/introspect.read_docstrings.v1",
        {"result": {
            "files_scanned": stats.get("total_files", 0),
            "rows_written":  stats.get("total_written", 0),
            "skipped":       stats.get("total_skipped", 0),
            "failed":        stats.get("total_failed", 0),
            "llm":           stats.get("total_llm", 0),
        }},
    )



