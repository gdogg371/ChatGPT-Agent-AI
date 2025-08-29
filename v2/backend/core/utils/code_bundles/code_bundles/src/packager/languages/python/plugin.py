# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/languages/python/plugin.py
from __future__ import annotations

"""
Python language plugin (list-tolerant analyze).

This implementation focuses on robustness:
- Accepts either a single file path or a list/tuple of file paths.
- Never passes a list into Path/open/os.* functions.
- Produces a conservative result structure that orchestrators can consume.

Expected usage (orchestrator examples this tolerates):
    analyze(files=[...], root="...")              # common
    analyze(inputs=[...], project_root="...")     # alternate key names
    analyze("file.py", "C:/repo")                 # positional fallback

No external dependencies.
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _as_file_list(x: Any) -> List[str]:
    """
    Normalize a value that could be a string, Path, list/tuple of either, or None
    into a clean list of POSIX-ish path strings.
    """
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        items = list(x)
    else:
        items = [x]

    out: List[str] = []
    for it in items:
        if isinstance(it, Path):
            out.append(it.as_posix())
        elif isinstance(it, str):
            out.append(it)
        else:
            # Best-effort stringification; avoids TypeError on Path(...)
            out.append(str(it))
    return out


def _resolve_rel(root: Optional[str], p: Path) -> str:
    """
    Best-effort relpath under root; falls back to POSIX path if outside root.
    """
    if not root:
        return p.as_posix()
    try:
        return str(p.resolve().relative_to(Path(root).resolve()).as_posix())
    except Exception:
        return p.as_posix()


def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """
    List-tolerant analyzer entrypoint.

    Parameters accepted (any combination):
      - files: List[str|Path] | str | Path
      - inputs: alias for files
      - root: str (preferred)
      - project_root: alias for root

    Positional fallback:
      - args[0] -> files
      - args[1] -> root

    Returns:
      {
        "ok": bool,
        "files_processed": int,
        "items": [
          {"path": "<abs_or_given>", "relpath": "<relative_if_possible>"},
          ...
        ],
        "warnings": [ "...", ... ]   # optional
      }
    """
    # Extract parameters from kwargs first
    files_arg = kwargs.get("files")
    if files_arg is None:
        files_arg = kwargs.get("inputs")

    root = kwargs.get("root") or kwargs.get("project_root")

    # Positional fallbacks if not provided in kwargs
    if files_arg is None and len(args) >= 1:
        files_arg = args[0]
    if root is None and len(args) >= 2:
        root = args[1]

    file_list = _as_file_list(files_arg)
    root_path = Path(root).resolve() if root else None

    items: List[Dict[str, Any]] = []
    warnings: List[str] = []
    processed = 0

    for fp in file_list:
        try:
            p = Path(fp)
            # Do not fail on non-existent paths; just record what we can.
            rel = _resolve_rel(str(root_path) if root_path else None, p)
            items.append({"path": p.as_posix(), "relpath": rel})
            processed += 1
        except Exception as e:
            # Swallow per-file errors to keep the whole batch robust.
            warnings.append(f"{fp}: {e}")

    result: Dict[str, Any] = {"ok": True, "files_processed": processed, "items": items}
    if warnings:
        result["warnings"] = warnings
    return result


__all__ = ["analyze"]
