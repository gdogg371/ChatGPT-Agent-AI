# File: v2/backend/core/utils/code_bundles/code_bundles/python_index.py
"""
Python source indexing utilities for the packager.

Provided APIs:
  - index_python_file(repo_root, local_path, repo_rel_posix) -> (module_record, edges)
  - build_ldt(repo_root, files=None) -> dict (language descriptor table)

Notes
-----
* This module is imported both by our runner and by the language plugin.
* The plugin expects `build_ldt` to exist and return a serializable mapping
  containing at least "kind", "generated_at", "root", "modules", and "edges".
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            return p.read_text(encoding=sys.getdefaultencoding() or "utf-8", errors="replace")
        except Exception:
            return ""

def _module_name_from_rel(rel: str) -> str:
    """
    Derive a dotted module name from a repo-relative POSIX path.
    Handles __init__.py by using the containing package path.
    """
    if rel.endswith("__init__.py"):
        pkg = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return pkg.replace("/", ".").strip(".")
    if rel.endswith(".py"):
        stem = rel[:-3]
        return stem.replace("/", ".").strip(".")
    # Fallback (non-.py, shouldn't happen here)
    return rel.replace("/", ".").strip(".")

def _collect_defs_and_imports(tree: ast.AST) -> Tuple[Dict[str, List[str]], List[str]]:
    """
    Return ({'classes': [...], 'functions': [...]}, ['imported.name', ...])
    """
    classes: List[str] = []
    functions: List[str] = []
    imports: List[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, ast.FunctionDef):
            functions.append(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            functions.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Construct a dotted import: <module>.<name> if possible
            mod = node.module or ""
            for alias in node.names:
                name = alias.name or ""
                dotted = mod if not name else f"{mod}.{name}" if mod else name
                if dotted:
                    imports.append(dotted)

    return {"classes": classes, "functions": functions}, imports


# ──────────────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PythonModuleRecord:
    kind: str
    path: str                # repo-relative or emitted path depending on caller
    module: str              # dotted module name
    symbols: Dict[str, List[str]]  # classes/functions
    imports: List[str]

def _edge_record(src_rel: str, imported: str) -> Dict[str, Any]:
    return {
        "kind": "graph.edge",
        "edge_type": "import",
        "src_path": src_rel,     # may be rewritten by caller to emitted path
        "dst": imported,         # dotted import name (as written in code)
    }


# ──────────────────────────────────────────────────────────────────────────────
# Public API: index a single file
# ──────────────────────────────────────────────────────────────────────────────

def index_python_file(
    *,
    repo_root: Path,
    local_path: Path,
    repo_rel_posix: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse a single Python file and return:
      - a PythonModuleRecord (as dict) or None if not a .py file
      - a list of graph.edge records for imports
    """
    if not repo_rel_posix.endswith(".py"):
        return None, []

    text = _safe_read_text(local_path)
    try:
        tree = ast.parse(text)
    except Exception:
        # Un-parseable file: still emit a minimal record so it appears in catalog,
        # but without symbols/imports.
        mod_name = _module_name_from_rel(repo_rel_posix)
        rec = PythonModuleRecord(
            kind="python.module",
            path=repo_rel_posix,
            module=mod_name,
            symbols={"classes": [], "functions": []},
            imports=[],
        )
        return asdict(rec), []

    symbols, imports = _collect_defs_and_imports(tree)
    mod_name = _module_name_from_rel(repo_rel_posix)
    rec = PythonModuleRecord(
        kind="python.module",
        path=repo_rel_posix,
        module=mod_name,
        symbols=symbols,
        imports=imports,
    )

    edges = [_edge_record(repo_rel_posix, imp) for imp in imports]
    return asdict(rec), edges


# ──────────────────────────────────────────────────────────────────────────────
# Public API expected by the Python language plugin
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_file_list(
    repo_root: Path,
    files: Optional[Iterable[Union[Path, str, Tuple[Path, str]]]],
) -> List[RepoItem]:
    """
    Accepts flexible input:
      - None -> discover all *.py under repo_root
      - Iterable[Path] -> compute repo-relative posix
      - Iterable[str] -> treat as repo-relative posix (resolve under repo_root)
      - Iterable[(Path, str)] -> pass through
    Returns list of (local_path, repo_rel_posix).
    """
    out: List[RepoItem] = []

    def to_rel(p: Path) -> str:
        return p.relative_to(repo_root).as_posix()

    if files is None:
        for p in repo_root.rglob("*.py"):
            if p.is_file():
                out.append((p, to_rel(p)))
        return out

    for item in files:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], Path) and isinstance(item[1], str):
            out.append((item[0], item[1]))
        elif isinstance(item, Path):
            out.append((item, to_rel(item)))
        elif isinstance(item, str):
            local = (repo_root / item)
            out.append((local, local.relative_to(repo_root).as_posix()))
        else:
            # Skip unknown shapes silently
            continue

    return out


def build_ldt(
    repo_root: Union[str, Path],
    files: Optional[Iterable[Union[Path, str, Tuple[Path, str]]]] = None,
) -> Dict[str, Any]:
    """
    Build a simple "Language Descriptor Table" for Python.

    Parameters
    ----------
    repo_root : str | Path
        Repository root (filesystem path).
    files : Optional iterable
        Optional file list. See _normalize_file_list for accepted shapes.

    Returns
    -------
    dict
        {
          "kind": "python.ldt",
          "generated_at": "<iso8601>",
          "root": "<repo_root posix>",
          "modules": [ {python.module}, ... ],
          "edges":   [ {graph.edge}, ... ]
        }
    """
    root = Path(repo_root).resolve()
    items = _normalize_file_list(root, files)

    modules: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for local, rel in items:
        mod_rec, file_edges = index_python_file(repo_root=root, local_path=local, repo_rel_posix=rel)
        if mod_rec:
            modules.append(mod_rec)
        if file_edges:
            edges.extend(file_edges)

    return {
        "kind": "python.ldt",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": root.as_posix(),
        "modules": modules,
        "edges": edges,
    }


__all__ = [
    "index_python_file",
    "build_ldt",
]

