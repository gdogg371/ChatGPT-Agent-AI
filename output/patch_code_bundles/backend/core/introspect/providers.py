# File: v2/backend/core/introspec/providers.py
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional
from v2.backend.core.spine.contracts import Artifact, Task

# ---- helpers ---------------------------------------------------------

def _bool(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)

def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

def _problem(uri: str, code: str, message: str, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={"problem": {
        "code": code, "message": message, "retryable": retryable, "details": dict(details or {})
    }})]

def _open_sqlite(url: str) -> sqlite3.Connection:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"Only sqlite URLs supported, got: {url}")
    path = url[len("sqlite:///") :]
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con

def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    cols: List[str] = []
    for r in con.execute(f'PRAGMA table_info("{table}")'):
        cols.append(r["name"])
    return cols

# ---- fetch -----------------------------------------------------------

def fetch_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: introspec.fetch.v1
    Payload:
      run: bool
      url: "sqlite:///path/to.db"
      table: str = "introspection_index"
      where: {status: "active"}  (optional)
      limit: int (optional)
      order_by: "id ASC" (optional)
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/introspec.fetch.v1", {"result": []})

    url = str(p.get("url") or "")
    table = str(p.get("table") or "introspection_index")
    where = p.get("where") or {}
    order_by = str(p.get("order_by") or "id ASC")
    limit = p.get("limit")

    try:
        con = _open_sqlite(url)
    except Exception as e:
        return _problem("spine://problem/introspec.fetch.v1", "ConfigError", str(e))

    sql = [f'SELECT id, filepath, symbol_type, name, lineno, description, unique_key_hash FROM "{table}"']
    params: List[Any] = []
    if isinstance(where, dict) and where.get("status"):
        sql.append("WHERE status = ?"); params.append(where["status"])
    if order_by:
        sql.append("ORDER BY " + order_by)
    if isinstance(limit, int) and limit > 0:
        sql.append("LIMIT ?"); params.append(limit)
    q = " ".join(sql)

    rows: List[Dict[str, Any]] = []
    try:
        with con:
            for row in con.execute(q, params):
                rows.append({
                    "id": row["id"],
                    "filepath": row["filepath"],
                    "lineno": row["lineno"],
                    "name": row["name"],
                    "symbol_type": row["symbol_type"],
                    "description": row["description"],
                    "unique_key_hash": row["unique_key_hash"],
                })
    except Exception as e:
        return _problem("spine://problem/introspec.fetch.v1", "DbError", f"fetch failed: {e!r}")
    finally:
        try: con.close()
        except Exception: pass

    return _result("spine://result/introspec.fetch.v1", {"result": rows})

# ---- write (upsert-ish, column-flexible) -----------------------------

def write_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: introspec.write.v1
    Payload:
      run: bool
      url: "sqlite:///path/to.db"
      table: str = "introspection_index"
      rows: [ {filepath, symbol_type, name, lineno, description, unique_key_hash, status?}, ... ]
    Behavior:
      - Inserts available fields; ignores unknown fields
      - If table has a unique/PK, INSERT OR IGNORE will avoid duplicates
    """
    p = task.payload or {}
    if not _bool(p.get("run", True)):
        return _result("spine://result/introspec.write.v1", {"result": {"written": 0}})

    url = str(p.get("url") or "")
    table = str(p.get("table") or "introspection_index")
    rows_in = p.get("rows") or []
    if not isinstance(rows_in, list):
        return _problem("spine://problem/introspec.write.v1", "InvalidPayload", "rows must be a list")

    try:
        con = _open_sqlite(url)
    except Exception as e:
        return _problem("spine://problem/introspec.write.v1", "ConfigError", str(e))

    try:
        cols = _table_columns(con, table)
        # permissible fields we know about (will intersect with actual table cols)
        preferred = ["filepath", "symbol_type", "name", "lineno", "description", "unique_key_hash", "status"]
        insert_cols = [c for c in preferred if c in cols]
        if not insert_cols:
            return _problem("spine://problem/introspec.write.v1", "SchemaError", f'no writable columns in "{table}"')

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
                        try: v = int(v) if v is not None else None
                        except Exception: v = None
                    vals.append(v)
                con.execute(sql, vals)
                written += 1

        return _result("spine://result/introspec.write.v1", {"result": {"written": written}})
    except Exception as e:
        return _problem("spine://problem/introspec.write.v1", "DbError", f"write failed: {e!r}")
    finally:
        try: con.close()
        except Exception: pass
