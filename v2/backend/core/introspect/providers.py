# File: v2/backend/core/introspect/providers.py
from __future__ import annotations

"""
Providers for the Introspection layer.

This version makes `introspect.fetch.v1` return a *clean* Result artifact with
`meta.items` at the top level (no nested `meta.result`). That prevents the
engine/runner from seeing a stringified Artifact and missing the rows.

Inputs (payload keys):
  - sqlalchemy_url: str (e.g., sqlite:///databases/bot_dev.db or absolute sqlite:///C:/...)
  - sqlalchemy_table: str (default: "introspection_index")
  - status: str | list[str]  (e.g., "todo" or ["todo"])
  - status_any: optional list[str]  (alternative filter)
  - max_rows: int (default 50)
  - exclude_globs: list[str] (applied to filepath)
  - segment_excludes: list[str] (reserved; ignored by fetch)

Return:
  [
    {
      "kind": "Result",
      "uri": "spine://result/introspect.fetch.v1",
      "sha256": "",
      "meta": {
        "items": [ {id, file, filetype, line, name, description, status}, ... ],
        "diagnostics": {...}
      }
    }
  ]
"""

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


# ------------------------------- helpers -------------------------------------

def _cwd() -> str:
    try:
        return str(Path.cwd())
    except Exception:
        return os.getcwd()


def _resolve_sqlite_url(url: str) -> Tuple[str, str]:
    """
    Returns (engine_url, resolved_path).

    Accepts both:
      - sqlite:///databases/bot_dev.db         (relative)
      - sqlite:///C:/path/to/bot_dev.db        (absolute on Windows)
      - sqlite:////C:/path/to/bot_dev.db       (canonical absolute some callers use)

    Normalizes to sqlite:///C:/... (THREE slashes) for absolute Windows paths.
    Uses the real filesystem path (resolved_path) for existence checks.
    """
    if not url or not url.startswith("sqlite:"):
        return url, ""

    # Strip the scheme, tolerate three or four slashes
    if url.startswith("sqlite:////"):
        path_part = url[len("sqlite:////"):]  # e.g., "C:/Users/.../bot_dev.db"
    elif url.startswith("sqlite:///"):
        path_part = url[len("sqlite:///"):]   # e.g., "databases/bot_dev.db" or "C:/.../bot_dev.db"
    else:
        # Unexpected variant; best effort: drop "sqlite:" and leading slashes
        path_part = url[len("sqlite:"):].lstrip("/")

    p = Path(path_part)

    # Absolute path (Windows drive or POSIX) → keep absolute, normalize to three slashes form
    if p.drive or p.is_absolute():
        return f"sqlite:///{p.as_posix()}", str(p)

    # Relative → resolve against CWD
    abs_p = (Path(_cwd()) / p).resolve()
    return f"sqlite:///{abs_p.as_posix()}", str(abs_p)


def _normalize_statuses(s: Any) -> List[str]:
    """
    Normalize like diagnostics: lower -> remove '_' and '-'.
    Accepts str or list[str].
    """
    if s is None:
        return []
    vals = s if isinstance(s, (list, tuple)) else [s]
    out: List[str] = []
    for v in vals:
        if not isinstance(v, str):
            continue
        out.append(v.replace("_", "").replace("-", "").lower())
    # de-dupe preserve order
    dedup: List[str] = []
    for v in out:
        if v not in dedup:
            dedup.append(v)
    return dedup


def _globs_exclude(path: str, patterns: Iterable[str]) -> bool:
    """Return True if 'path' matches any glob in patterns."""
    if not path:
        return False
    posix_path = Path(path).as_posix()
    for pat in patterns or []:
        if fnmatch.fnmatch(posix_path, pat):
            return True
    return False


def _artifact(kind: str, uri: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": kind, "uri": uri, "sha256": "", "meta": meta}


def _problem(code: str, message: str, retryable: bool = False, details: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return _artifact(
        "Problem",
        "spine://capability/introspect.fetch.v1",
        {"problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}}
    )


# ------------------------------- provider ------------------------------------

def fetch_v1(payload: Dict[str, Any], context: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """
    Fetch rows from the introspection_index table and return them as clean items.
    """
    sqlalchemy_url: str | None = payload.get("sqlalchemy_url")
    table_name: str = payload.get("sqlalchemy_table") or "introspection_index"
    status = payload.get("status")
    status_any = payload.get("status_any")
    max_rows: int = int(payload.get("max_rows", 50))
    exclude_globs: List[str] = list(payload.get("exclude_globs") or [])

    # Always include safe defaults so pipeline never fails interpolation
    if "**/__pycache__/**" not in exclude_globs:
        exclude_globs.append("**/__pycache__/**")
    if "output/**" not in exclude_globs:
        exclude_globs.append("output/**")

    if not sqlalchemy_url:
        return [_problem("ConfigError", "Missing sqlalchemy_url in payload.", details={"payload_keys": list(payload.keys())})]

    engine_url, resolved_path = _resolve_sqlite_url(sqlalchemy_url)

    # Basic file existence hint for sqlite (use resolved_path, not URL slicing)
    if engine_url.startswith("sqlite"):
        if resolved_path and not Path(resolved_path).exists():
            return [_problem(
                "SQLiteFileNotFound",
                f"SQLite database file not found at absolute path: {resolved_path}",
                retryable=False,
                details={"sqlalchemy_url": sqlalchemy_url, "resolved_path": resolved_path}
            )]

    # Connect & reflect
    try:
        engine: Engine = create_engine(engine_url, future=True)
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=engine)
    except SQLAlchemyError as e:
        return [_problem("DBConnectError", f"Failed to initialize DB/table: {e}", details={"sqlalchemy_url": engine_url, "table": table_name})]

    # Normalize requested statuses
    req_norm = _normalize_statuses(status_any or status or [])
    if not req_norm:
        # Default to 'todo' if unspecified
        req_norm = ["todo"]

    # Build query (no raw SQL)
    s_col = table.c.status
    status_norm_expr = func.lower(func.replace(func.replace(s_col, "_", ""), "-", ""))

    # Oversample factor so we can drop via globs and still keep max_rows
    oversample = max(1, min(10, 5 if max_rows <= 50 else max_rows // 10))
    fetch_limit = max_rows * oversample

    stmt = (
        select(
            table.c.id,
            table.c.filepath,
            table.c.symbol_type,
            table.c.name,
            table.c.lineno,
            table.c.route_method,
            table.c.route_path,
            table.c.ag_tag,
            table.c.description,
            table.c.target_symbol,
            table.c.relation_type,
            table.c.unique_key_hash,
            table.c.status,
            table.c.discovered_at,
            table.c.last_seen_at,
            table.c.resolved_at,
            table.c.occurrences,
            table.c.recurrence_count,
            table.c.created_at,
            table.c.updated_at,
            table.c.mdata,
        )
        .where(status_norm_expr.in_(req_norm))
        .order_by(table.c.id.desc())
        .limit(fetch_limit)
    )

    # Prepare diagnostics
    table_cols = [c.name for c in table.columns]
    sql_preview = ""
    try:
        sql_preview = str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
    except Exception:
        # Fallback minimal preview
        sql_preview = f"SELECT ... FROM {table_name} WHERE normalized(status) IN {req_norm} ORDER BY id DESC LIMIT {fetch_limit}"

    diagnostics: Dict[str, Any] = {
        "engine_url": engine_url,
        "resolved_db_path": resolved_path or "",
        "table": table_name,
        "cwd": _cwd(),
        "requested_status": status if isinstance(status, list) else ([status] if isinstance(status, str) else status_any or []),
        "requested_status_norm": req_norm,
        "max_rows": max_rows,
        "sql_preview": sql_preview,
        "where_sql_preview": f"normalized status IN {req_norm}",
        "table_columns": table_cols,
        "glob_excludes": exclude_globs,
        "segment_excludes": payload.get("segment_excludes") or [],
    }

    # Execute & shape
    try:
        with engine.connect() as conn:
            # total rows (for context) and counts per status
            try:
                total = conn.execute(select(func.count()).select_from(table)).scalar_one()
                diagnostics["table_total_rows"] = int(total)
            except Exception:
                pass

            try:
                sc = conn.execute(select(s_col, func.count().label("count")).group_by(s_col)).all()
                diagnostics["table_status_counts"] = [{"status": r[0], "count": int(r[1])} for r in sc]
            except Exception:
                pass

            rows = conn.execute(stmt).mappings().all()
    except SQLAlchemyError as e:
        return [_problem("DBQueryError", f"Query failed: {e}", details={"sql": sql_preview})]

    diagnostics["fetched_rows_before_filters"] = len(rows)

    # Apply glob excludes
    filtered: List[Dict[str, Any]] = []
    for r in rows:
        fp = r.get("filepath") or ""
        if _globs_exclude(fp, exclude_globs):
            continue
        filtered.append(dict(r))

    diagnostics["fetched_rows_after_filters"] = len(filtered)

    # Map to items (match your sample keys)
    items: List[Dict[str, Any]] = []
    for r in filtered[:max_rows]:
        items.append({
            "id": r.get("id"),
            "file": r.get("filepath"),
            "filetype": r.get("symbol_type"),
            "line": r.get("lineno"),
            "name": r.get("name"),
            "description": r.get("description"),
            "status": r.get("status"),
        })

    # Add a few human-friendly samples for forensics
    if filtered:
        diagnostics["samples"] = {
            "first_5_raw": filtered[:5],
            "first_5_items": items[:5],
        }

    # --- IMPORTANT: return a *clean* Result artifact with meta.items ---
    return [_artifact("Result", "spine://result/introspect.fetch.v1", {"items": items, "diagnostics": diagnostics})]



