# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/utils_db_load_sqlite_vec.py
from __future__ import annotations

"""
Capability: utils.db.load_sqlite_vec.v1
---------------------------------------
Safely load a SQLite vector (or any) extension into a target SQLite database,
entirely within the Spine pipeline (no external CLI).

This provider does **not** guess extension-specific SQL; it only loads the
shared library and optionally executes a caller-provided `test_sql`.

Payload
-------
- db_path:        str   (REQUIRED)  Path to SQLite database file (created if missing)
- plugin_path:    str   (REQUIRED)  Path to the extension library (.dll/.so/.dylib)
- test_sql:       str   (optional)  SQL to run after loading (e.g., 'select 1')
- create_if_missing: bool (optional, default True) Create DB file if it does not exist
- timeout_secs:   float (optional, default 30.0)  sqlite3 connect timeout
- disable_load:   bool  (optional, default False) If True, skip actual loading (dry-run)

Return
------
{
  "db_path": "<abs>",
  "plugin_path": "<abs>",
  "created_db": true|false,
  "loaded": true|false,
  "sqlite_version": "3.x.x",
  "page_count": <int>,
  "page_size": <int>,
  "total_bytes": <int>,
  "test_sql": {"ran": true|false, "rows": <int>, "error": "<str or ''>"},
}
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import os
import sqlite3
import sys
import time


# ------------------------------- helpers --------------------------------------
def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _summarize_db(con: sqlite3.Connection, db_path: Path) -> Dict[str, int | str]:
    cur = con.cursor()
    try:
        cur.execute("PRAGMA page_count")
        page_count = int(cur.fetchone()[0])
    except Exception:
        page_count = 0
    try:
        cur.execute("PRAGMA page_size")
        page_size = int(cur.fetchone()[0])
    except Exception:
        page_size = 0
    try:
        total_bytes = page_count * page_size if page_count and page_size else db_path.stat().st_size
    except Exception:
        total_bytes = 0
    return {
        "page_count": page_count,
        "page_size": page_size,
        "total_bytes": total_bytes,
    }


def _sqlite_version(con: sqlite3.Connection) -> str:
    cur = con.cursor()
    try:
        cur.execute("select sqlite_version()")
        row = cur.fetchone()
        return str(row[0]) if row else ""
    except Exception:
        return ""


def _validate_plugin_file(p: Path) -> None:
    if not p.is_file():
        raise FileNotFoundError(f"plugin_path does not exist or is not a file: {p}")
    # Very light sanity on extension suffix
    allowed = {".dll", ".so", ".dylib"}
    if p.suffix.lower() not in allowed:
        # We allow unusual suffixes, but warn via exception message detail.
        # Not raising a strict error to support custom-named shared libs.
        pass  # no-op: informational only


# -------------------------------- provider ------------------------------------
def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    db_raw = payload.get("db_path")
    plugin_raw = payload.get("plugin_path")
    if not db_raw or not plugin_raw:
        raise ValueError("payload must include 'db_path' and 'plugin_path'")

    db_path = Path(str(db_raw)).expanduser().resolve()
    plugin_path = Path(str(plugin_raw)).expanduser().resolve()

    create_if_missing = _as_bool(payload.get("create_if_missing"), True)
    disable_load = _as_bool(payload.get("disable_load"), False)
    timeout_secs = float(payload.get("timeout_secs") or 30.0)
    test_sql = payload.get("test_sql")
    if test_sql is not None and not isinstance(test_sql, str):
        raise ValueError("'test_sql' must be a string when provided")

    _validate_plugin_file(plugin_path)

    # Ensure DB location
    if not db_path.exists():
        if not create_if_missing:
            raise FileNotFoundError(f"db_path does not exist and create_if_missing=False: {db_path}")
        _ensure_parent(db_path)
        # touching the file ensures a deterministic 'created_db' flag
        db_path.touch(exist_ok=True)
        created_db = True
    else:
        created_db = False

    # Connect
    con = None
    try:
        con = sqlite3.connect(str(db_path), timeout=timeout_secs)
        # Enable extension loading if supported
        try:
            con.enable_load_extension(True)  # type: ignore[attr-defined]
        except AttributeError as e:
            raise RuntimeError(
                "This Python sqlite3 build does not support enable_load_extension()."
            ) from e

        loaded = False
        if not disable_load:
            try:
                con.load_extension(str(plugin_path))
                loaded = True
            except sqlite3.OperationalError as e:
                # Typical errors: "not authorized", "file is not a dll/so"
                raise RuntimeError(f"Failed to load extension: {plugin_path} ({e})") from e

        # Optional test SQL
        test_result = {"ran": False, "rows": 0, "error": ""}
        if test_sql:
            cur = con.cursor()
            try:
                cur.execute(test_sql)
                rows = cur.fetchall()
                test_result["ran"] = True
                test_result["rows"] = len(rows)
            except Exception as e:
                test_result["ran"] = True
                test_result["error"] = f"{type(e).__name__}: {e}"

        # Summaries
        ver = _sqlite_version(con)
        s = _summarize_db(con, db_path)

        return {
            "db_path": str(db_path),
            "plugin_path": str(plugin_path),
            "created_db": created_db,
            "loaded": loaded,
            "sqlite_version": ver,
            **s,
            "test_sql": test_result,
        }
    finally:
        if con is not None:
            try:
                # Return to safe default
                con.enable_load_extension(False)  # type: ignore[attr-defined]
            except Exception:
                pass
            con.close()
