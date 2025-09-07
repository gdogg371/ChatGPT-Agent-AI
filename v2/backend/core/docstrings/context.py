# File: v2/backend/core/docstrings/context.py
"""
Docstring-domain context helpers.

This module is **docstring-specific** and stays entirely inside the `docstrings`
package. It does NOT import from `prompt_pipeline` or any other sibling area.
It provides utilities to:
  - read source windows around a target line
  - analyze a Python file to derive a lightweight signature + context block
  - build per-item context for a batch of generic "items" (id/path/relpath/line)

Inputs are generic dicts produced by upstream stages (e.g., ENRICH), typically
containing:
  - id (str)
  - path | relpath
  - target_lineno | line | lineno
  - signature (optional precomputed)
  - context (optional existing dict to be merged)

All paths are resolved from a provided project_root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ast
import io


# ------------------------------- I/O ---------------------------------------


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _split_lines(src: str) -> List[str]:
    return src.splitlines(keepends=True)


def _clip_lines(lines: List[str], start_idx: int, end_idx: int) -> str:
    start = max(0, start_idx)
    end = max(start, min(len(lines), end_idx))
    return "".join(lines[start:end])


def _resolve_path(project_root: Path, path_or_rel: Optional[str]) -> Optional[Path]:
    if not path_or_rel:
        return None
    p = Path(path_or_rel)
    if not p.is_absolute():
        p = project_root / p
    try:
        return p.resolve()
    except Exception:
        return p


# --------------------------- Python analysis -------------------------------


def _node_signature(node: ast.AST, source: str) -> Optional[str]:
    """
    Best-effort signature extraction for FunctionDef/AsyncFunctionDef/ClassDef.
    Returns the header line up to the first ':'.
    """
    try:
        seg = ast.get_source_segment(source, node)
        if not isinstance(seg, str):
            return None
        # take only the header (first line up to ':')
        buf = io.StringIO(seg)
        header = buf.readline().rstrip("\r\n")
        if ":" in header:
            header = header.split(":", 1)[0]
        return header.strip()
    except Exception:
        return None


def _lineno(n: ast.AST) -> int:
    try:
        return int(getattr(n, "lineno", 10**9) or 10**9)
    except Exception:
        return 10**9


def _find_target_node(
    tree: ast.AST,
    lineno: int,
    symbol_name: Optional[str],
) -> Tuple[Optional[ast.AST], str]:
    """
    Heuristics:
      1) If symbol_name is provided, prefer a node with that exact name.
      2) Otherwise, pick the closest def/class starting at or after lineno.
      3) Fallback to the module node.

    Returns: (node, resolved_kind: "function"|"class"|"module")
    """
    best: Optional[ast.AST] = None
    best_kind = "module"

    def _is_name_match(n: ast.AST) -> bool:
        if not symbol_name:
            return False
        n_name = getattr(n, "name", None)
        return isinstance(n_name, str) and n_name == symbol_name

    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_name_match(n):
                return n, "function"
            if best is None and _lineno(n) >= max(1, lineno):
                best = n
                best_kind = "function"
        elif isinstance(n, ast.ClassDef):
            if _is_name_match(n):
                return n, "class"
            if best is None and _lineno(n) >= max(1, lineno):
                best = n
                best_kind = "class"

    if best is not None:
        return best, best_kind

    return tree, "module"


@dataclass
class PythonContextOptions:
    """
    Options that control how much context to return for Python targets.
    """
    # Number of extra lines to append after the target node for additional context
    trailing_after_node: int = 10
    # Max lines for module-level context snippet if no specific node chosen
    module_max_lines: int = 80
    # Fallback max body length when node end_lineno is not present
    fallback_node_body_len: int = 40


def analyze_python_context(
    *,
    file_path: Path,
    lineno: int = 0,
    symbol_name: Optional[str] = None,
    options: Optional[PythonContextOptions] = None,
) -> Dict[str, Any]:
    """
    Analyze a Python file and return a minimal, docstring-appropriate context:
      {
        "signature": str | None,
        "context_code": str
      }
    """
    opts = options or PythonContextOptions()
    try:
        src = _read_text(file_path)
    except Exception:
        return {"signature": None, "context_code": ""}

    lines = _split_lines(src)
    try:
        tree = ast.parse(src, filename=str(file_path))
    except Exception:
        # If it doesn't parse, return a safe slice around the requested lineno
        start = max(0, int(lineno or 1) - 1)
        return {
            "signature": None,
            "context_code": _clip_lines(lines, start, min(len(lines), start + opts.module_max_lines)),
        }

    node, kind = _find_target_node(tree, lineno=max(1, int(lineno or 1)), symbol_name=symbol_name)

    # Compute signature and context window
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        sig = _node_signature(node, src)
        start = max(0, (_lineno(node) or 1) - 1)
        end = getattr(node, "end_lineno", None)
        if not isinstance(end, int) or end <= start:
            end = min(len(lines), start + opts.fallback_node_body_len)
        end = min(len(lines), end + max(0, int(opts.trailing_after_node)))
        context = _clip_lines(lines, start, end)
    else:
        # Module-level
        sig = None
        context = _clip_lines(lines, 0, min(len(lines), max(1, int(opts.module_max_lines))))

    return {
        "signature": sig,
        "context_code": context,
    }


# ------------------------------ Windows ------------------------------------


def read_source_window(
    *,
    project_root: Path,
    relpath_or_path: str,
    center_lineno: int,
    before: int = 20,
    after: int = 20,
) -> Dict[str, Any]:
    """
    Return a raw source window around a line number.

    Returns:
      {
        "code": "...",
        "start_line": 1-based start,
        "end_line": 1-based end,
        "path": resolved absolute path (as string)
      }
    """
    p = _resolve_path(project_root, relpath_or_path)
    if not p or not p.exists():
        return {"code": "", "start_line": 0, "end_line": 0, "path": str(p) if p else ""}

    try:
        lines = _split_lines(_read_text(p))
    except Exception:
        return {"code": "", "start_line": 0, "end_line": 0, "path": str(p)}

    idx = max(0, int(center_lineno or 1) - 1)
    start_idx = max(0, idx - max(0, int(before)))
    end_idx = min(len(lines), idx + max(0, int(after)) + 1)
    code = _clip_lines(lines, start_idx, end_idx)

    # Convert to 1-based inclusive lines for reporting
    return {
        "code": code,
        "start_line": start_idx + 1,
        "end_line": end_idx,
        "path": str(p),
    }


# ------------------------------ Batch build --------------------------------


def build_context_for_items(
    *,
    project_root: Path,
    items: Iterable[Dict[str, Any]],
    options: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    For each item, compute/merge a minimal docstring-friendly context.

    Each output item mirrors the input and adds/merges:
      item["context"] = { "signature": ..., "context_code": ... }

    The inputs may contain:
      - "path" or "relpath"
      - "target_lineno" or "line" or "lineno"
      - "context" (dict) to be merged with computed context

    Unknown/unsupported languages are passed through without context_code.
    """
    opts = PythonContextOptions(
        trailing_after_node=int((options or {}).get("trailing_after_node", 10)),
        module_max_lines=int((options or {}).get("module_max_lines", 80)),
        fallback_node_body_len=int((options or {}).get("fallback_node_body_len", 40)),
    )

    out: List[Dict[str, Any]] = []

    for it in items or []:
        if not isinstance(it, dict):
            continue

        rel_or_path = (
            it.get("relpath")
            or it.get("path")
            or (it.get("context") or {}).get("relpath")
        )
        lineno_val = (
            it.get("target_lineno")
            or it.get("line")
            or it.get("lineno")
            or (it.get("context") or {}).get("lineno")
            or 0
        )

        p = _resolve_path(project_root, rel_or_path)
        if p and p.suffix.lower() == ".py":
            ctx = analyze_python_context(
                file_path=p,
                lineno=int(lineno_val or 0),
                symbol_name=(it.get("context") or {}).get("name"),
                options=opts,
            )
        else:
            # Non-Python target: pass-through with empty context (extensible later)
            ctx = {"signature": None, "context_code": ""}

        merged_ctx = {**(it.get("context") or {}), **ctx}
        out.append({**it, "context": merged_ctx})

    return out
