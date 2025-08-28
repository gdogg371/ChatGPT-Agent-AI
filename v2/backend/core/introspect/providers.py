#v2\backend\core\introspect\providers.py
from __future__ import annotations
r"""
Spine providers for the Introspection subsystem.

Fixes in this version:
- Ensure rows are materialized correctly using SQLAlchemy 2.0 Row._mapping.
- Make status filtering robust by doing it in Python (case/underscore/dash-insensitive).
- Accept both 'status', 'status_filter', and 'status_any' (list).
- Apply exclude_globs and segment_excludes after row normalization.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from pathlib import Path
import fnmatch

from sqlalchemy import create_engine, text  # type: ignore

from v2.backend.core.spine.contracts import Artifact, Task


# ------------------------------ helpers ---------------------------------------

def _problem(uri: str, code: str, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}
    })]


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _as_posix(s: str) -> str:
    return str(s).replace("\\", "/")

def _lower_set(items: Iterable[str]) -> set[str]:
    return {str(x).lower() for x in items}

def _norm_status(s: str) -> str:
    return str(s).strip().lower().replace("_", "").replace("-", "")

def _status_list_from_payload(p: Mapping[str, Any]) -> List[str]:
    if isinstance(p.get("status_any"), list) and p["status_any"]:
        return [str(x) for x in p["status_any"]]
    s = p.get("status")
    if s is None:
        s = p.get("status_filter")
    if isinstance(s, list):
        return [str(x) for x in s]
    if isinstance(s, str) and s.strip():
        return [s]
    return []  # no status filter

def _row_to_item(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize a DB row dict to the engine item shape."""
    # Column name compatibility
    filepath = row.get("filepath") or row.get("file") or ""
    filepath = _as_posix(filepath)
    symbol_type = row.get("symbol_type") or row.get("filetype") or "unknown"
    name = row.get("name") or row.get("function") or row.get("route") or ""
    lineno = int(row.get("lineno") or row.get("line") or 1)
    description = row.get("description") or ""
    status = row.get("status") or "active"
    uid = row.get("id") or row.get("hash") or row.get("unique_key_hash") or f"{filepath}:{lineno}"

    return {
        "id": uid,
        "file": filepath,
        "filetype": symbol_type,
        "line": lineno,
        "name": name,
        "description": description,
        "status": status,
    }

def _path_excluded(path_posix: str, *, exclude_globs: Iterable[str], segment_excludes: Iterable[str]) -> bool:
    # Glob patterns against repo-relative posix path
    for g in exclude_globs or []:
        g1 = _as_posix(g)
        if fnmatch.fnmatch(path_posix, g1):
            return True
    # Basename pruning (case-insensitive)
    segs = path_posix.split("/")
    segset = _lower_set(segs)
    banned = _lower_set(segment_excludes or [])
    return any(seg in banned for seg in segset)


# ------------------------------ providers -------------------------------------

def fetch_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Query introspection_index and return target items.

    Payload (required):
      sqlalchemy_url: str
      sqlalchemy_table: str   (usually 'introspection_index')

    Payload (optional):
      status | status_filter: str | list[str]
      status_any: list[str]
      max_rows: int
      exclude_globs: list[str]
      segment_excludes: list[str]
    """
    uri_ok = "spine://result/introspect.fetch.v1"
    uri_ng = "spine://problem/introspect.fetch.v1"
    p = task.payload or {}

    url = str(p.get("sqlalchemy_url") or "").strip()
    table = str(p.get("sqlalchemy_table") or "").strip()
    if not url or not table:
        return _problem(uri_ng, "InvalidPayload", "Missing sqlalchemy_url/sqlalchemy_table")

    status_any_raw = _status_list_from_payload(p)
    status_any = [_norm_status(x) for x in status_any_raw]
    max_rows = int(p.get("max_rows") or 200)
    exclude_globs = list(p.get("exclude_globs") or [])
    segment_excludes = list(p.get("segment_excludes") or [])

    try:
        eng = create_engine(url, future=True)
    except Exception as e:
        return _problem(uri_ng, "DbError", f"Failed to connect: {e}")

    # Fetch without WHERE to avoid dialect/string normalization pitfalls;
    # filter in Python for reliability.
    sql = f"SELECT * FROM {table} ORDER BY id DESC"
    try:
        with eng.connect() as cx:
            result = cx.execute(text(sql))
            # SQLAlchemy 2.0: use Row._mapping to get a dict
            rows = [dict(r._mapping) for r in result]
    except Exception as e:
        return _problem(uri_ng, "DbError", f"Query failed: {e}")

    items: List[Dict[str, Any]] = []
    for r in rows:
        item = _row_to_item(r)
        if not item["file"]:
            continue
        if status_any:
            if _norm_status(item.get("status", "")) not in status_any:
                continue
        if _path_excluded(item["file"], exclude_globs=exclude_globs, segment_excludes=segment_excludes):
            continue
        items.append(item)
        if len(items) >= max_rows:
            break

    if not items:
        return _problem(uri_ng, "ValidationError", "No valid targets found for docstring patching.")

    return _result(uri_ok, {"items": items})





