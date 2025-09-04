# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/utils_db_init_sqlite_dev.py
from __future__ import annotations

"""
Capability: utils.db.init_sqlite_dev.v1
---------------------------------------
Wrap the local SQLite dev DB initializer
  backend/core/utils/db/init_sqlite_dev.py
as a Spine provider (no subprocess/CLI).

Design
------
- Import the module and invoke a callable if present (prefer kwargs that match).
- Never guess arguments: we introspect the target callable’s signature and only
  pass payload keys that match parameter names.
- If the module exposes none of (init_dev_db, init_db, main), we raise a clear error.
- After execution, we summarize the resulting DB (table count, approx size).

Payload
-------
- db_path:            str   (REQUIRED)  Path to the SQLite DB file to create/init
- reset:              bool  (optional)  If supported by target module, request reset
- schema_sql:         str   (optional)  Path to .sql file (if module supports it)
- pragmas:            list[str] (opt)   Pragmas to apply (if module supports it)
- extra:              dict  (optional)  Extra kwargs to pass if supported by module

Return
------
{
  "db_path": "<abs>",
  "called": {
     "module": "backend.core.utils.db.init_sqlite_dev",
     "function": "init_dev_db|init_db|main",
     "kwargs_used": {"...": "..."}
  },
  "summary": {
     "tables": <int>,
     "views": <int>,
     "indices": <int>,
     "total_bytes": <int>,
     "page_count": <int>,
     "page_size": <int>
  }
}
"""

from pathlib import Path
from typing import Any, Dict, Optional
import importlib
import inspect
import os
import sqlite3


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _choose_callable(mod) -> Optional[tuple[str, Any]]:
    """Prefer a clearly-named initializer; fall back to 'main'."""
    for name in ("init_dev_db", "init_db", "create_dev_db", "main"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return name, fn
    return None


def _filter_kwargs(fn, candidate_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Return only kwargs accepted by fn’s signature."""
    sig = inspect.signature(fn)
    accepted = set(sig.parameters.keys())
    return {k: v for k, v in candidate_kwargs.items() if k in accepted}


def _summarize_sqlite(db_path: Path) -> Dict[str, int]:
    if not db_path.exists():
        return {"tables": 0, "views": 0, "indices": 0, "total_bytes": 0, "page_count": 0, "page_size": 0}
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='view'")
        views = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='index'")
        indices = int(cur.fetchone()[0])
        try:
            cur.execute("PRAGMA page_count")
            page_count = int(cur.fetchone()[0])
            cur.execute("PRAGMA page_size")
            page_size = int(cur.fetchone()[0])
            total_bytes = page_count * page_size
        except Exception:
            page_count = 0
            page_size = 0
            try:
                total_bytes = db_path.stat().st_size
            except Exception:
                total_bytes = 0
        return {
            "tables": tables,
            "views": views,
            "indices": indices,
            "total_bytes": total_bytes,
            "page_count": page_count,
            "page_size": page_size,
        }
    finally:
        con.close()


def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    db_raw = payload.get("db_path")
    if not db_raw:
        raise ValueError("payload must include 'db_path'")
    db_path = Path(str(db_raw)).expanduser().resolve()
    _ensure_parent(db_path)

    reset = _as_bool(payload.get("reset"), False)
    schema_sql_raw = payload.get("schema_sql")
    schema_sql = Path(str(schema_sql_raw)).expanduser().resolve() if schema_sql_raw else None
    pragmas = payload.get("pragmas")
    if pragmas is not None and not isinstance(pragmas, (list, tuple)):
        raise ValueError("'pragmas' must be a list of strings if provided")
    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("'extra' must be a dict if provided")

    # Import target module
    try:
        mod = importlib.import_module("backend.core.utils.db.init_sqlite_dev")
    except Exception as e:
        raise ImportError("Unable to import backend.core.utils.db.init_sqlite_dev") from e

    chosen = _choose_callable(mod)
    if not chosen:
        raise RuntimeError(
            "init_sqlite_dev module exposes no callable among "
            "('init_dev_db', 'init_db', 'create_dev_db', 'main')"
        )
    fn_name, fn = chosen

    # Build candidate kwargs; only pass what the function accepts
    candidate_kwargs: Dict[str, Any] = {
        "db_path": str(db_path),
        "reset": reset,
        "schema_sql": str(schema_sql) if schema_sql else None,
        "pragmas": list(pragmas) if isinstance(pragmas, (list, tuple)) else None,
        **extra,
    }
    # Drop None values pre-filter to avoid accidental override of defaults
    candidate_kwargs = {k: v for k, v in candidate_kwargs.items() if v is not None}
    kwargs_used = _filter_kwargs(fn, candidate_kwargs)

    # Invoke
    result = fn(**kwargs_used)  # type: ignore[misc]

    # Summarize DB
    summary = _summarize_sqlite(db_path)

    return {
        "db_path": str(db_path),
        "called": {
            "module": "backend.core.utils.db.init_sqlite_dev",
            "function": fn_name,
            "kwargs_used": kwargs_used,
        },
        "summary": summary,
        "result": result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else None,
    }
