# File: v2/backend/core/prompt_pipeline/executor/sources.py
"""
Generic data sources for the prompt pipeline.

Capability implemented here:
  - introspect.fetch.v1  -> fetch_v1

This provider MUST be domain-agnostic. It simply pulls rows from a DB table
according to the payload parameters and returns a normalized shape:

  {"records": [ {<row>...}, ... ], "count": <int>}

On error, it still returns {"records": []} and includes an "error" message.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import contextlib

try:
    # SQLAlchemy is expected by the project
    from sqlalchemy import create_engine, text, inspect  # type: ignore
except Exception as e:  # pragma: no cover
    create_engine = None  # type: ignore
    text = None  # type: ignore
    inspect = None  # type: ignore


# ----------------------------- helpers ---------------------------------------


def _payload_of(task_like: Any, **overrides) -> Dict[str, Any]:
    """Extract a dict payload from either a Task-like (.payload) or a dict."""
    if hasattr(task_like, "payload") and isinstance(getattr(task_like, "payload"), dict):
        base = dict(task_like.payload)
    elif isinstance(task_like, dict):
        base = dict(task_like)
    else:
        base = {}
    base.update(overrides or {})
    return base


def _as_list(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return list(val)
    return [val]


def _discover_columns(engine, table: str) -> List[str]:
    """
    Return the actual column names for `table`, best-effort.
    Uses SQLAlchemy inspector; if unavailable or fails, tries SQLite PRAGMA.
    """
    cols: List[str] = []
    # Try SQLAlchemy inspector first
    if inspect is not None:
        try:
            insp = inspect(engine)
            info = insp.get_columns(table)
            if isinstance(info, list):
                for c in info:
                    name = c.get("name")
                    if isinstance(name, str):
                        cols.append(name)
                if cols:
                    return cols
        except Exception:
            pass

    # Fallback: SQLite PRAGMA (works only for SQLite)
    try:
        with engine.connect() as conn:
            res = conn.execute(text(f"PRAGMA table_info({table})"))
            for row in res:
                # row: (cid, name, type, notnull, dflt_value, pk)
                name = row[1] if len(row) > 1 else None
                if isinstance(name, str):
                    cols.append(name)
    except Exception:
        pass
    return cols


def _make_select_columns(requested: List[str], actual: List[str]) -> Tuple[str, List[str]]:
    """
    Build the SELECT columns clause from requested vs actual.

    - Keep only intersection of requested∩actual
    - If intersection is empty, return "*" (SELECT all)
    - If 'filepath' is not present but 'file' is, alias it as "file AS filepath"
    Returns (select_clause, output_columns) where output_columns are the keys that will
    appear in each record dict (post-normalization).
    """
    actual_set = set(actual)
    keep = [c for c in requested if c in actual_set]

    select_parts: List[str] = []
    output_cols: List[str] = []

    if not keep:
        # SELECT * fallback; we'll normalize 'filepath' if a 'file' column exists later
        return "*", actual

    # Handle filepath/file aliasing
    if "filepath" in keep:
        select_parts.append("filepath")
        output_cols.append("filepath")
    elif "file" in actual_set:
        select_parts.append("file AS filepath")
        output_cols.append("filepath")  # we alias it to filepath downstream

    # Add the rest, skipping duplicates and the alias target
    seen = set(output_cols)
    for c in keep:
        if c == "filepath":
            continue  # already added
        if c not in seen:
            select_parts.append(c)
            output_cols.append(c)
            seen.add(c)

    return ", ".join(select_parts), output_cols


# ----------------------------- capability ------------------------------------


def fetch_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Fetch rows from a SQL table.

    Expected payload keys (all optional except sqlalchemy_url/sqlalchemy_table):
      sqlalchemy_url: "sqlite:///path/to/db.sqlite"
      sqlalchemy_table: "introspection_index"
      status: "todo"                        # optional single status filter
      status_any: ["todo", "active"]        # optional multi-status filter
      max_rows: 50                          # limit
      columns: ["id","filepath","name",...] # optional explicit column list

    Returns:
      {"records":[{...},{...}], "count": N}
    """
    payload: Dict[str, Any] = _payload_of(task_like, **kwargs)
    url: Optional[str] = payload.get("sqlalchemy_url")
    table: Optional[str] = payload.get("sqlalchemy_table")
    status: Optional[str] = payload.get("status")
    status_any: Sequence[str] = _as_list(payload.get("status_any"))
    limit: int = int(payload.get("max_rows") or 50)

    # A safe default request set. Note: DO NOT include 'file' here; we alias dynamically.
    default_cols = [
        "id",
        "filepath",
        "symbol_type",
        "name",
        "lineno",
        "status",
        "description",
        "route_method",
        "route_path",
        "ag_tag",
        "unique_key_hash",
        "discovered_at",
        "last_seen_at",
        "resolved_at",
        "occurrences",
        "recurrence_count",
        "created_at",
        "updated_at",
        "mdata",
    ]
    columns: List[str] = _as_list(payload.get("columns")) or default_cols

    if not url or not table:
        return {
            "records": [],
            "count": 0,
            "error": "Missing required payload keys: sqlalchemy_url and sqlalchemy_table",
        }

    if create_engine is None or text is None:
        return {
            "records": [],
            "count": 0,
            "error": "SQLAlchemy is required for introspect.fetch.v1 (pip install sqlalchemy)",
        }

    records: List[Dict[str, Any]] = []
    with contextlib.ExitStack() as stack:
        engine = create_engine(url, future=True)
        conn = stack.enter_context(engine.connect())

        # Discover actual columns to avoid selecting non-existent ones
        actual_cols = _discover_columns(engine, table)
        select_clause, out_cols = _make_select_columns(columns, actual_cols)

        # Build WHERE
        where_clauses: List[str] = []
        params: Dict[str, Any] = {}

        if status:
            where_clauses.append("status = :status")
            params["status"] = status

        if status_any:
            keys: List[str] = []
            for i, val in enumerate(status_any):
                key = f"s_any_{i}"
                keys.append(f":{key}")
                params[key] = val
            where_clauses.append(f"status IN ({', '.join(keys)})")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        order_clause = "ORDER BY id DESC" if "id" in actual_cols else ""

        sql = f"SELECT {select_clause} FROM {table} {where_sql} {order_clause} LIMIT :limit"
        params["limit"] = limit

        try:
            res = conn.execute(text(sql), params)
            # mappings() gives dict-like rows when available; else fallback to raw rows
            try:
                iterator = res.mappings()
                use_mappings = True
            except Exception:
                iterator = res
                use_mappings = False

            for row in iterator:
                if use_mappings:
                    rec = dict(row)
                else:
                    # Build a dict from selected output columns if possible, else list indices
                    if out_cols and isinstance(row, (tuple, list)):
                        rec = {col: row[i] for i, col in enumerate(out_cols) if i < len(row)}
                    else:
                        rec = {"_row": list(row) if isinstance(row, (tuple, list)) else row}
                # Normalize filepath alias (if we aliased 'file AS filepath', it already exists)
                if "filepath" not in rec and "file" in rec:
                    rec["filepath"] = rec.get("file")
                # Ensure id exists to help ordering/reporting
                if "id" not in rec:
                    rec["id"] = rec.get("unique_key_hash") or rec.get("name") or rec.get("filepath") or ""
                records.append(rec)
        except Exception as e:
            # Do NOT raise; return a structured, non-string error
            return {
                "records": [],
                "count": 0,
                "error": f"SQL execution failed: {e}",
                "query": sql,
                "params": params,
            }

    return {"records": records, "count": len(records)}





