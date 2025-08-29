# File: v2/backend/core/prompt_pipeline/executor/sources.py
from __future__ import annotations

"""
Generic introspection source + lightweight code context utilities (domain-agnostic).

Part A — IntrospectionDbSource
--------------------------------
Fetch rows via Spine so the executor does not depend on DB client code.
Yields dicts shaped for legacy consumers:

  id, filepath, lineno, name, symbol_type, description, unique_key_hash

Part B — analyze_symbol_context
--------------------------------
Given a file path and (optional) lineno/symbol hint, returns a *generic* context:

  {
    "signature": str | None,
    "context_code": str,
  }

NOTE:
- This module MUST remain domain-neutral. It does not compute or return any
  docstring-specific fields. Domain adapters (e.g., docstrings) should perform
  their own analysis via their Spine capabilities.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Iterable, Optional, Tuple, List
from pathlib import Path
import ast
import io
import os

# Use the orchestrator wrapper to call Spine capabilities without binding to a specific API
try:
    from .orchestrator import capability_run  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError("executor.sources requires executor.orchestrator.capability_run") from e


# =============================================================================
# Part A — DB source via Spine
# =============================================================================

@dataclass
class IntrospectionDbSource:
    """
    Neutral DB source that fetches records through the Spine capability
    'introspect.fetch.v1'.

    Args:
        url: SQLAlchemy URL, e.g. 'sqlite:///.../bot_dev.db'
        table: table/view name (default: 'introspection_index')
        status: optional status filter (preferred key)
        max_rows: optional row limit (int)
        order_by: pass-through ordering hint for providers
        status_filter: deprecated alias for 'status' (kept for compatibility)
    """
    url: str
    table: str = "introspection_index"
    status: Optional[str] = None
    max_rows: Optional[int] = None
    order_by: str = "id ASC"
    # Back-compat alias (deprecated)
    status_filter: Optional[str] = None

    def _fetch_rows_via_spine(self) -> Iterable[Dict[str, Any]]:
        filt = self.status if self.status is not None else self.status_filter
        payload: Dict[str, Any] = {
            "sqlalchemy_url": self.url,
            "sqlalchemy_table": self.table,
            "order_by": self.order_by,
        }
        if filt:
            # Providers commonly accept either 'status' or a 'where' clause.
            payload["status"] = filt
            payload["where"] = {"status": filt}
        if isinstance(self.max_rows, int) and self.max_rows > 0:
            payload["limit"] = int(self.max_rows)

        arts = capability_run(
            "introspect.fetch.v1",
            payload,
            context={"phase": "FETCH", "module": "prompt_pipeline.executor.sources"},
        )

        # If provider returned a Problem-like artifact, try to surface it
        for a in arts:
            meta = getattr(a, "meta", None)
            kind = getattr(a, "kind", "")
            if kind == "Problem" and isinstance(meta, dict):
                prob = (meta or {}).get("problem", {})
                code = prob.get("code", "ProviderError")
                msg = prob.get("message", "unknown error")
                raise RuntimeError(f"introspect.fetch.v1 failed: {code}: {msg}")

        # Normalize to iterable of dict rows (support several provider shapes)
        rows: Iterable[Dict[str, Any]] = []
        if arts:
            m0 = getattr(arts[0], "meta", arts[0])
            if isinstance(m0, dict):
                if isinstance(m0.get("records"), list):
                    rows = [r for r in m0["records"] if isinstance(r, dict)]
                elif isinstance(m0.get("result"), dict) and isinstance(m0["result"].get("records"), list):
                    rows = [r for r in m0["result"]["records"] if isinstance(r, dict)]
                elif isinstance(m0.get("items"), list):
                    rows = [r for r in m0["items"] if isinstance(r, dict)]
        return rows

    def read_rows(self) -> Iterator[Dict[str, Any]]:
        """
        Yield rows shaped like the legacy sqlite prototype:

            {
              "id": int|str,
              "filepath": "path/relative.py",
              "lineno": int,
              "name": str,
              "symbol_type": str,
              "description": str,
              "unique_key_hash": str
            }
        """
        for row in self._fetch_rows_via_spine():
            # Map flexible column names to the expected shape
            filepath = (
                row.get("filepath")
                or row.get("file")
                or row.get("relpath")
                or row.get("path")
                or ""
            )
            lineno = row.get("lineno", row.get("line", 0))
            try:
                lineno = int(lineno or 0)
            except Exception:
                lineno = 0

            yield {
                "id": row.get("id"),
                "filepath": filepath,
                "lineno": lineno,
                "name": row.get("name") or "",
                "symbol_type": row.get("symbol_type") or row.get("type") or "",
                "description": row.get("description") or row.get("summary") or "",
                "unique_key_hash": row.get("unique_key_hash") or row.get("hash") or "",
            }


# =============================================================================
# Part B — Code context utilities (generic)
# =============================================================================

def _read_text(p: Path) -> str:
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _clip_lines(lines: List[str], start: int, end: int) -> str:
    start = max(0, start)
    end = min(len(lines), end)
    return "".join(lines[start:end])


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


def _find_target_node(
    tree: ast.AST,
    lineno: int,
    symbol_name: Optional[str],
    symbol_type: str,
) -> Tuple[Optional[ast.AST], str]:
    """
    Heuristics:
      1) If symbol_name is provided, prefer a node with that name.
      2) Otherwise, pick the closest def/class starting at or after lineno.
      3) Fallback to the module.
    Returns (node, resolved_symbol_type)
    """
    best: Optional[ast.AST] = None
    best_kind = "module"

    def _is_name_match(n: ast.AST) -> bool:
        if not symbol_name:
            return False
        n_name = getattr(n, "name", None)
        return isinstance(n_name, str) and n_name == symbol_name

    def _lineno(n: ast.AST) -> int:
        return int(getattr(n, "lineno", 10**9) or 10**9)

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


def analyze_symbol_context(
    *,
    file_path: str | Path,
    lineno: int = 0,
    symbol_name: Optional[str] = None,
    symbol_type: str = "module",
) -> Dict[str, Any]:
    """
    Analyze a Python file and return a minimal, *generic* context bundle for prompting.

    Returns:
      {
        "signature": str | None,
        "context_code": str,
      }

    NOTE:
    - This function intentionally does NOT return any docstring-specific fields.
      Domain adapters should compute them independently as needed.
    """
    p = Path(file_path)
    src = _read_text(p)
    lines = src.splitlines(keepends=True)

    try:
        tree = ast.parse(src, filename=str(p))
    except Exception:
        return {"signature": None, "context_code": ""}

    node, kind = _find_target_node(tree, lineno=lineno, symbol_name=symbol_name, symbol_type=symbol_type)

    # Compute signature and generic context window
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        sig = _node_signature(node, src)
        start = max(0, (getattr(node, "lineno", 1) or 1) - 1)
        end = getattr(node, "end_lineno", None)
        if not isinstance(end, int) or end <= start:
            end = min(len(lines), start + 40)
        # add a bit of trailing context
        context = _clip_lines(lines, start, min(len(lines), end + 10))
    else:
        # Module-level
        sig = None
        context = _clip_lines(lines, 0, min(len(lines), 80))

    return {
        "signature": sig,
        "context_code": context,
    }



