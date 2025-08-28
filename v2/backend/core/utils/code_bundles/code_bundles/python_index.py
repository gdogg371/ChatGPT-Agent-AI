# File: v2/backend/core/utils/code_bundles/code_bundles/python_index.py
"""
Python source indexer.

Produces:
- a `python.module` record (via contracts.build_python_module)
- zero or more `graph.edge` records (via contracts.build_graph_edge) for imports

Design goals:
- Robust to parse errors (we'll skip module record and edges if AST fails).
- Conservative, portable implementation (no third-party deps).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_python_module,
    build_graph_edge,
)


def _first_line(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    return s.splitlines()[0][:400]  # cap length to keep manifest compact


def _package_root_for(file_path: Path, repo_root: Path) -> Tuple[str, bool]:
    """
    Walk upward from file_path's directory until __init__.py stops appearing.
    Return (package_root_repo_rel_posix, has_init_in_module_dir).
    """
    file_dir = file_path.parent
    has_init_here = (file_dir / "__init__.py").exists()
    pkg_dir = file_dir
    last_pkg = None
    while True:
        if (pkg_dir / "__init__.py").exists():
            last_pkg = pkg_dir
            if pkg_dir == repo_root:
                break
            pkg_dir = pkg_dir.parent
            # stop if we went above repo root
            if repo_root not in pkg_dir.parents and pkg_dir != repo_root:
                break
            continue
        break
    if last_pkg is None:
        return (file_dir.relative_to(repo_root).as_posix(), has_init_here)
    return (last_pkg.relative_to(repo_root).as_posix(), has_init_here)


def _module_dotted_for(repo_rel: str) -> str:
    """
    Convert repo-relative posix path to dotted module name.
    - .../__init__.py -> drop the filename
    - .../name.py -> ... . name
    """
    if not repo_rel.endswith(".py"):
        # non-.py caller shouldn't use this; still provide dotted-ish path
        return repo_rel.replace("/", ".")
    parts = repo_rel.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]  # strip .py
    return ".".join(p for p in parts if p)


def _collect_defs(mod: ast.AST) -> Dict[str, List[Dict[str, Any]]]:
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []

    for node in ast.walk(mod):
        if isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))  # py3.9+
                except Exception:
                    bases.append(getattr(getattr(b, "id", None), "id", None) or type(b).__name__)
            classes.append({
                "name": node.name,
                "lineno": int(getattr(node, "lineno", 0) or 0),
                "end_lineno": int(getattr(node, "end_lineno", 0) or 0),
                "bases": bases,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "lineno": int(getattr(node, "lineno", 0) or 0),
                "end_lineno": int(getattr(node, "end_lineno", 0) or 0),
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })

    classes.sort(key=lambda d: (d["lineno"], d["name"]))
    functions.sort(key=lambda d: (d["lineno"], d["name"]))
    return {"classes": classes, "functions": functions}


def _collect_imports(mod: ast.AST) -> List[Dict[str, Any]]:
    imports: List[Dict[str, Any]] = []
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "import",
                    "name": alias.name,
                    "alias": alias.asname or None,
                    "level": 0,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            lvl = int(getattr(node, "level", 0) or 0)
            for alias in node.names:
                imports.append({
                    "type": "from",
                    "module": module,
                    "name": alias.name,
                    "alias": alias.asname or None,
                    "level": lvl,
                })
    return imports


def _edges_from_imports(
    *,
    imports: List[Dict[str, Any]],
    current_module: str,
) -> List[Dict[str, Any]]:
    """
    Convert import dicts into graph edges.

    - For 'import x.y as z' -> edge dst=x.y
    - For 'from a.b import c' -> edge dst=a.b (module); if module is empty and level==0, dst=c
    - For relative imports (level>0), we resolve against the current_module package:
      - e.g., current 'pkg.sub.mod', 'from . import x' (level=1, module='')
        -> dst='pkg.sub'
      - 'from ..util import y' (level=2, module='util') -> dst='pkg.util'
    """
    edges: List[Dict[str, Any]] = []

    def resolve_relative(level: int, mod: str) -> str:
        if level <= 0:
            return mod or ""
        base_parts = current_module.split(".")
        # if current is a module (not package), drop last component
        # e.g. v2.backend.foo.bar -> package v2.backend.foo
        if base_parts:
            base_parts = base_parts[:-1]
        # climb 'level-1' further (since one up already by dropping module)
        up = max(0, len(base_parts) - (level - 1))
        new_parts = base_parts[:up]
        if mod:
            new_parts.extend(mod.split("."))
        return ".".join(p for p in new_parts if p)

    for imp in imports:
        if imp.get("type") == "import":
            dst = imp.get("name") or ""
            if dst:
                edges.append(build_graph_edge(src_path="<PYMODULE>", dst_module=dst, edge_type="import"))
        elif imp.get("type") == "from":
            lvl = int(imp.get("level") or 0)
            mod = imp.get("module") or ""
            if lvl > 0:
                resolved = resolve_relative(lvl, mod)
                if resolved:
                    edges.append(build_graph_edge(src_path="<PYMODULE>", dst_module=resolved, edge_type="from"))
            else:
                if mod:
                    edges.append(build_graph_edge(src_path="<PYMODULE>", dst_module=mod, edge_type="from"))
                else:
                    # 'from  import name' (rare) -> fall back to the imported symbol as module
                    name = imp.get("name") or ""
                    if name:
                        edges.append(build_graph_edge(src_path="<PYMODULE>", dst_module=name, edge_type="from"))
    return edges


def index_python_file(
    *,
    repo_root: Path,
    local_path: Path,
    repo_rel_posix: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse a Python file and return:
      (python.module record or None if parse failed, list of graph.edge records)

    The returned edges have a temporary src_path "<PYMODULE>" and the caller
    should replace it with the true repo-relative path before writing.
    """
    text: str
    try:
        text = local_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary or funky encoding: skip
        return None, []
    except Exception:
        return None, []

    try:
        mod = ast.parse(text, filename=repo_rel_posix)
    except Exception:
        # Parse error: no module record; no edges
        return None, []

    imports = _collect_imports(mod)
    defs = _collect_defs(mod)
    doc = _first_line(ast.get_docstring(mod))
    package_root, has_init = _package_root_for(local_path, repo_root)
    dotted = _module_dotted_for(repo_rel_posix)

    module_rec = build_python_module(
        path=repo_rel_posix,
        module=dotted,
        package_root=package_root,
        has_init=has_init,
        imports=imports,
        classes=defs["classes"],
        functions=defs["functions"],
        doc=doc,
    )

    edges = _edges_from_imports(imports=imports, current_module=dotted)
    # Caller replaces src_path placeholder
    for e in edges:
        e["src_path"] = repo_rel_posix

    return module_rec, edges
