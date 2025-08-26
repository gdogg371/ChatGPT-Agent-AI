# File: v2/backend/core/prompt_pipeline/executor/sources.py
from __future__ import annotations

"""
Spine-forwarding Introspection source + lightweight code context utilities.

Part A — IntrospectionDbSource
--------------------------------
Replaces direct DB access with a Spine capability call so that the executor
does not depend on DB code. Mirrors the old API and yielded row shape.

Yields dicts with keys:
  id, filepath, lineno, name, symbol_type, description, unique_key_hash

Part B — analyze_symbol_context
--------------------------------
Given a file path and (optional) lineno/symbol hint, returns:
  {
    "signature": str | None,
    "has_docstring": bool,
    "existing_docstring": str,
    "context_code": str,
  }
This is used by the SourceRetriever to enrich rows for prompt building.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Iterable, Optional, Tuple, List
from pathlib import Path
import ast
import io

from v2.backend.core.spine import Spine
from v2.backend.core.configuration.spine_paths import SPINE_CAPS_PATH


# =============================================================================
# Part A — DB source via Spine
# =============================================================================

@dataclass
class IntrospectionDbSource:
    url: str
    table: str = "introspection_index"
    status_filter: Optional[str] = "active"
    max_rows: Optional[int] = None
    order_by: str = "id ASC"

    def _fetch_rows_via_spine(self) -> Iterable[Dict[str, Any]]:
        spine = Spine(caps_path=SPINE_CAPS_PATH)

        payload: Dict[str, Any] = {
            # ✅ pass the DB location; provider accepts either key
            "url": self.url,
            "sqlalchemy_url": self.url,
            "table": self.table,
            "order_by": self.order_by,
        }

        # Preserve legacy filters/limits
        if self.status_filter:
            # provider supports both 'status_filter' and 'where.status'
            payload["status_filter"] = self.status_filter
            payload["where"] = {"status": self.status_filter}
        if isinstance(self.max_rows, int) and self.max_rows > 0:
            payload["limit"] = int(self.max_rows)

        arts = spine.dispatch_capability(
            capability="introspect.fetch.v1",   # 🔧 FIX: correct capability id
            payload=payload,
            intent="discover",
            subject=self.table,
            context={"executor": "prompt_pipeline.executor.sources"},
        )

        # If provider returned a Problem, surface a crisp error
        for a in arts:
            if a.kind == "Problem":
                prob = (a.meta or {}).get("problem", {})
                code = prob.get("code", "ProviderError")
                msg = prob.get("message", "unknown error")
                raise RuntimeError(f"introspect.fetch.v1 failed: {code}: {msg}")

        rows: Iterable[Dict[str, Any]] = []
        if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
            res = arts[0].meta["result"]
            if isinstance(res, list):
                rows = [r for r in res if isinstance(r, dict)]
            elif isinstance(res, dict) and isinstance(res.get("records"), list):
                rows = [r for r in res["records"] if isinstance(r, dict)]
        return rows

    def read_rows(self) -> Iterator[Dict]:
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
# Part B — Code context utilities
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

    # Walk and score candidates
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
    Analyze a Python file and return a minimal context bundle for prompting.

    Returns:
      {
        "signature": str | None,
        "has_docstring": bool,
        "existing_docstring": str,
        "context_code": str,
      }
    """
    p = Path(file_path)
    src = _read_text(p)
    lines = src.splitlines(keepends=True)

    try:
        tree = ast.parse(src, filename=str(p))
    except Exception:
        return {"signature": None, "has_docstring": False, "existing_docstring": "", "context_code": ""}

    node, kind = _find_target_node(tree, lineno=lineno, symbol_name=symbol_name, symbol_type=symbol_type)

    # Compute signature and docstring
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        sig = _node_signature(node, src)
        doc = ast.get_docstring(node) or ""
        has = bool(doc.strip())
        start = max(0, (getattr(node, "lineno", 1) or 1) - 1)
        # `end_lineno` isn't guaranteed; if missing, approximate a small window
        end = getattr(node, "end_lineno", None)
        if not isinstance(end, int) or end <= start:
            end = min(len(lines), start + 40)
        # A little extra trailing context
        context = _clip_lines(lines, start, min(len(lines), end + 10))
    else:
        # Module-level
        sig = None
        doc = ast.get_docstring(tree) or ""
        has = bool(doc.strip())
        context = _clip_lines(lines, 0, min(len(lines), 80))

    return {
        "signature": sig,
        "has_docstring": has,
        "existing_docstring": doc,
        "context_code": context,
    }


