# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/utils_io_zip.py
from __future__ import annotations

"""
Capability: utils.io.zip.v1
---------------------------
Wrap the local recursive zipper at:
  backend/core/utils/io/recursively_zip_directory.py
as a Spine provider (no subprocesses).

Design
------
- Import the zipper module and invoke a clear callable, preferring:
    zip_directory(...), zip_dir(...), make_zip(...), run(...), main(...)
- We never guess arguments: inspect the callable's signature and only pass
  payload keys that match.
- On completion, we summarize the produced ZIP (file count & size).

Payload
-------
- src_dir:         str        (REQUIRED)  Directory to zip
- out_zip:         str        (REQUIRED)  Destination .zip file
- include_globs:   list[str]  (optional)  Include patterns (POSIX, root-relative)
- exclude_globs:   list[str]  (optional)  Exclude patterns
- compression:     str        (optional)  "deflate" (default) | "store"
- prefix_in_zip:   str        (optional)  Path prefix for members inside the ZIP
- follow_symlinks: bool       (optional)  Default False
- deterministic:   bool       (optional)  If supported by target, request stable mtimes

Return
------
{
  "src_dir": "<abs>",
  "out_zip": "<abs>",
  "compression": "deflate|store|unknown",
  "files": <int>,
  "bytes": <int>,
  "call": {"module":"...recursively_zip_directory","function":"...", "kwargs_used": {...}}
}
"""

from pathlib import Path
from typing import Any, Dict, Tuple
import importlib
import inspect
import json
import zipfile


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _choose_callable(mod) -> Tuple[str, Any]:
    for name in ("zip_directory", "zip_dir", "make_zip", "run", "main"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return name, fn
    raise RuntimeError(
        "recursively_zip_directory module exposes no callable among "
        "('zip_directory','zip_dir','make_zip','run','main')"
    )


def _filter_kwargs(fn, candidate_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    sig = inspect.signature(fn)
    accepted = set(sig.parameters.keys())
    return {k: v for k, v in candidate_kwargs.items() if k in accepted}


def _summarize_zip(p: Path) -> Dict[str, int]:
    if not p.exists() or not p.is_file():
        return {"files": 0, "bytes": 0}
    try:
        with zipfile.ZipFile(p, "r") as zf:
            return {"files": len(zf.infolist()), "bytes": p.stat().st_size}
    except Exception:
        return {"files": 0, "bytes": p.stat().st_size if p.exists() else 0}


def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    src_raw = payload.get("src_dir")
    out_raw = payload.get("out_zip")
    if not src_raw or not out_raw:
        raise ValueError("payload must include 'src_dir' and 'out_zip'")

    src_dir = Path(str(src_raw)).expanduser().resolve()
    out_zip = Path(str(out_raw)).expanduser().resolve()
    if not src_dir.is_dir():
        raise FileNotFoundError(f"src_dir not found: {src_dir}")
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    include_globs = list(payload.get("include_globs") or [])
    exclude_globs = list(payload.get("exclude_globs") or [])
    compression = str(payload.get("compression") or "deflate").strip().lower()
    prefix_in_zip = payload.get("prefix_in_zip")
    follow_symlinks = _as_bool(payload.get("follow_symlinks"), False)
    deterministic = _as_bool(payload.get("deterministic"), False)

    try:
        mod = importlib.import_module("backend.core.utils.io.recursively_zip_directory")
    except Exception as e:
        raise ImportError(
            "Unable to import backend.core.utils.io.recursively_zip_directory"
        ) from e

    fn_name, fn = _choose_callable(mod)

    # Build candidate kwargs; only pass what target supports
    candidate_kwargs: Dict[str, Any] = {
        "src_dir": str(src_dir),
        "out_zip": str(out_zip),
        "include_globs": include_globs,
        "exclude_globs": exclude_globs,
        "compression": compression,        # if the util accepts a policy string
        "prefix_in_zip": prefix_in_zip,
        "follow_symlinks": follow_symlinks,
        "deterministic": deterministic,
    }
    candidate_kwargs = {k: v for k, v in candidate_kwargs.items() if v is not None}
    kwargs_used = _filter_kwargs(fn, candidate_kwargs)

    # Invoke zipper
    result = fn(**kwargs_used)  # type: ignore[misc]

    # Summarize artifact
    summ = _summarize_zip(out_zip)
    comp = compression if out_zip.exists() else "unknown"

    # If callable returned a path, prefer that as out_zip (defensive)
    if isinstance(result, (str, Path)):
        rp = Path(result)
        if rp.suffix.lower() == ".zip" and rp.exists():
            out_zip = rp.resolve()
            summ = _summarize_zip(out_zip)

    return {
        "src_dir": str(src_dir),
        "out_zip": str(out_zip),
        "compression": comp,
        **summ,
        "call": {
            "module": "backend.core.utils.io.recursively_zip_directory",
            "function": fn_name,
            "kwargs_used": kwargs_used,
        },
    }
