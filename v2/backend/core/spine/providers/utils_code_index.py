# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/utils_code_index.py
from __future__ import annotations

"""
Capability: utils.code.index.v1
--------------------------------
Wrap the local code indexer at:
  backend/core/utils/scanners/code_indexer.py
as a Spine provider (no CLI, no subprocesses).

Design
------
- Import the indexer module and call a clear callable:
    preferred: index_code(...)
    fallback:  run(...), main(...)
- We *never* guess arguments: inspect the callable's signature and pass only
  payload keys that match.
- If the callable returns in-memory rows, we persist them to `out_file`
  (CSV or JSON/JSONL depending on suffix). If the callable writes directly to
  `out_file`, we simply summarize the artifact.
- If the module exposes neither of the expected callables, we fail clearly.

Payload
-------
- root:            str        (REQUIRED)  Directory to scan
- out_file:        str        (REQUIRED)  Path to write results (.csv | .json | .jsonl)
- include_globs:   list[str]  (optional)  Include patterns (posix globs relative to root)
- exclude_globs:   list[str]  (optional)  Exclude patterns (posix globs)
- exts:            list[str]  (optional)  File extensions to include (e.g., [".py",".sql"])
- max_files:       int        (optional)  Hard cap on files scanned
- follow_symlinks: bool       (optional)  Default False
- recurse:         bool       (optional)  Default True
- format:          str        (optional)  "auto"|"csv"|"json"|"jsonl" (default "auto")
- extra:           dict       (optional)  Extra kwargs to pass if the target supports them

Return
------
{
  "root": "<abs>",
  "out_file": "<abs>",
  "call": {"module":"...code_indexer","function":"index_code|run|main","kwargs_used":{...}},
  "written": {"bytes": <int>, "rows": <int>},
  "format": "csv|json|jsonl"
}
"""

from dataclasses import is_dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Sequence
import importlib
import inspect
import io
import json
import csv
import os


# ------------------------------ helpers ---------------------------------------
def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _detect_format(out_file: Path, fmt_opt: str | None) -> str:
    if fmt_opt and fmt_opt.lower() in {"csv", "json", "jsonl"}:
        return fmt_opt.lower()
    suf = out_file.suffix.lower()
    if suf == ".csv":
        return "csv"
    if suf == ".jsonl":
        return "jsonl"
    if suf == ".json":
        return "json"
    # default
    return "csv"


def _choose_callable(mod) -> tuple[str, Any]:
    for name in ("index_code", "run", "main"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return name, fn
    raise RuntimeError(
        "code_indexer module exposes no callable among ('index_code', 'run', 'main')"
    )


def _filter_kwargs(fn, candidate_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    sig = inspect.signature(fn)
    accepted = set(sig.parameters.keys())
    return {k: v for k, v in candidate_kwargs.items() if k in accepted}


def _normalize_rows(rows: Any) -> List[Dict[str, Any]]:
    """
    Convert common row shapes to list[dict]. Raise if unsupported.
    Accepts:
      - list[dict]
      - list[dataclass]
      - list[tuple] with 2-4 columns -> coerced to dict with generic keys
    """
    if rows is None:
        return []
    if isinstance(rows, list):
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return rows  # type: ignore[return-value]
        if is_dataclass(rows[0]):
            return [asdict(x) for x in rows]  # type: ignore[arg-type]
        if isinstance(rows[0], tuple):
            out: List[Dict[str, Any]] = []
            for t in rows:  # type: ignore[assignment]
                if not isinstance(t, tuple):
                    raise TypeError("Mixed row types; expected uniform tuples")
                d = {f"col{i}": v for i, v in enumerate(t)}
                out.append(d)
            return out
    raise TypeError(
        "Unsupported result type from indexer callable; expected list[dict|dataclass|tuple]"
    )


def _write_rows(out_file: Path, fmt: str, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Persist rows to out_file. Returns (bytes_written, rows_written).
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        # union all keys to preserve columns
        fieldnames: List[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with out_file.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        size = out_file.stat().st_size
        return size, len(rows)
    elif fmt == "json":
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        size = out_file.stat().st_size
        return size, len(rows)
    elif fmt == "jsonl":
        with out_file.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        size = out_file.stat().st_size
        return size, len(rows)
    else:
        raise ValueError(f"Unsupported output format: {fmt}")


# ------------------------------ provider --------------------------------------
def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    root_raw = payload.get("root")
    out_file_raw = payload.get("out_file")
    if not root_raw or not out_file_raw:
        raise ValueError("payload must include 'root' and 'out_file'")

    root = Path(str(root_raw)).expanduser().resolve()
    out_file = Path(str(out_file_raw)).expanduser().resolve()
    fmt = _detect_format(out_file, payload.get("format"))

    include_globs = list(payload.get("include_globs") or [])
    exclude_globs = list(payload.get("exclude_globs") or [])
    exts = list(payload.get("exts") or [])
    max_files = payload.get("max_files")
    follow_symlinks = _as_bool(payload.get("follow_symlinks"), False)
    recurse = _as_bool(payload.get("recurse"), True)
    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError("'extra' must be a dict if provided")

    if not root.is_dir():
        raise FileNotFoundError(f"root directory not found: {root}")

    # Import target module
    try:
        mod = importlib.import_module("backend.core.utils.scanners.code_indexer")
    except Exception as e:
        raise ImportError("Unable to import backend.core.utils.scanners.code_indexer") from e

    fn_name, fn = _choose_callable(mod)

    # Candidate kwargs—only those accepted by the callable will be passed.
    candidate_kwargs: Dict[str, Any] = {
        "root": str(root),
        "out_file": str(out_file),          # if the indexer supports writing directly
        "include_globs": include_globs,
        "exclude_globs": exclude_globs,
        "exts": exts,
        "max_files": int(max_files) if max_files is not None else None,
        "follow_symlinks": bool(follow_symlinks),
        "recurse": bool(recurse),
        **extra,
    }
    candidate_kwargs = {k: v for k, v in candidate_kwargs.items() if v is not None}
    kwargs_used = _filter_kwargs(fn, candidate_kwargs)

    # Call the indexer
    result = fn(**kwargs_used)  # type: ignore[misc]

    # If the indexer wrote the file, prefer summarizing that artifact.
    if out_file.exists():
        rows_written = None
        try:
            if fmt == "csv":
                with out_file.open("r", encoding="utf-8") as f:
                    rows_written = sum(1 for _ in f) - 1  # minus header
            elif fmt in {"json", "jsonl"}:
                if fmt == "json":
                    data = json.loads(out_file.read_text(encoding="utf-8") or "[]")
                    rows_written = len(data) if isinstance(data, list) else 0
                else:
                    with out_file.open("r", encoding="utf-8") as f:
                        rows_written = sum(1 for _ in f)
        except Exception:
            rows_written = None

        return {
            "root": str(root),
            "out_file": str(out_file),
            "call": {"module": "backend.core.utils.scanners.code_indexer", "function": fn_name, "kwargs_used": kwargs_used},
            "written": {"bytes": out_file.stat().st_size, "rows": rows_written},
            "format": fmt,
        }

    # Otherwise, if it returned rows in-memory, normalize & persist here.
    try:
        rows = _normalize_rows(result)
    except Exception as e:
        raise RuntimeError(
            "Indexer did not produce an output file and returned an unsupported result type. "
            "Either allow it to write to 'out_file' by accepting that kwarg, or return a list of rows."
        ) from e

    bytes_written, rows_written = _write_rows(out_file, fmt, rows)
    return {
        "root": str(root),
        "out_file": str(out_file),
        "call": {"module": "backend.core.utils.scanners.code_indexer", "function": fn_name, "kwargs_used": kwargs_used},
        "written": {"bytes": bytes_written, "rows": rows_written},
        "format": fmt,
    }
