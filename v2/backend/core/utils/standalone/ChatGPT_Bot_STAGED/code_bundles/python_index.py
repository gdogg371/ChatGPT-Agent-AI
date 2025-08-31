# v2/backend/core/utils/code_bundles/code_bundles/python_index.py

from __future__ import annotations

import ast
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # last-ditch fallback
        return p.read_text(errors="replace")


def _mod_name_from_rel(rel: str) -> str:
    # Convert repo_rel_posix to dotted module name (best-effort)
    # e.g. "pkg/subpkg/mod.py" -> "pkg.subpkg.mod" ; "__init__.py" maps to its package
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join([p for p in parts if p])


@dataclass
class _Scope:
    kind: str                    # "module" | "class" | "function"
    name: str                    # dotted-ish scope name
    lineno: int
    col: int


def _qualify(scope_stack: List[_Scope], leaf: str) -> str:
    if not scope_stack:
        return leaf
    bases = [s.name for s in scope_stack if s.name]
    return ".".join(bases + ([leaf] if leaf else []))


def _expr_to_name(expr: ast.AST) -> str:
    # best-effort string for a callable or attribute chain
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _expr_to_name(expr.value)
        return f"{base}.{expr.attr}" if base else expr.attr
    if isinstance(expr, ast.Call):
        return _expr_to_name(expr.func)
    if isinstance(expr, ast.Subscript):
        return _expr_to_name(expr.value)
    if isinstance(expr, ast.alias):  # pragma: no cover
        return expr.name
    return getattr(expr, "id", "") or getattr(expr, "attr", "") or expr.__class__.__name__


def _decorator_names(node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]) -> List[str]:
    out: List[str] = []
    for dec in getattr(node, "decorator_list", []) or []:
        out.append(_expr_to_name(dec))
    return out


def _end_lineno(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 0))


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
def index_python_file(
    *,
    repo_root: Path,
    local_path: Path,
    repo_rel_posix: str,
    emit_ast: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Index a Python file and produce:
      - module record (dict)
      - import edges (list of dicts with src_path/dst_module; dst_path may be filled downstream)
      - optional AST extras (when emit_ast=True): symbols/xrefs/calls/docstrings/symbol_metrics lists

    This function is **pure** and does not touch global state.
    """

    text = _read_text(local_path)
    try:
        tree = ast.parse(text, filename=repo_rel_posix)
    except SyntaxError as se:
        # Return a minimal record so callers can continue
        mod_rec = {
            "record_type": "module_index",
            "language": "python",
            "path": repo_rel_posix,
            "module": _mod_name_from_rel(repo_rel_posix),
            "error": {
                "kind": "SyntaxError",
                "message": str(se),
                "lineno": getattr(se, "lineno", None),
                "offset": getattr(se, "offset", None),
            },
        }
        return mod_rec, [], None

    module_name = _mod_name_from_rel(repo_rel_posix)
    classes: List[str] = []
    functions: List[str] = []
    imports: List[str] = []
    import_edges: List[Dict[str, Any]] = []

    # Optional AST payloads
    ast_symbols: List[Dict[str, Any]] = []
    ast_xrefs: List[Dict[str, Any]] = []
    ast_calls: List[Dict[str, Any]] = []
    ast_docs: List[Dict[str, Any]] = []
    ast_symmetrics: List[Dict[str, Any]] = []

    scope: List[_Scope] = [_Scope("module", module_name, 1, 0)]

    class Visitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            super().generic_visit(node)

        # --- modules / imports -------------------------------------------------
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                mod = alias.name
                asname = alias.asname
                imports.append(mod)
                import_edges.append(
                    {
                        "record_type": "edge.import",
                        "language": "python",
                        "src_path": repo_rel_posix,
                        "dst_module": mod,
                        "lineno": node.lineno,
                    }
                )
                if emit_ast:
                    ast_xrefs.append(
                        {
                            "record_type": "ast.xref",
                            "kind": "import",
                            "path": repo_rel_posix,
                            "module": module_name,
                            "name": mod,
                            "asname": asname,
                            "lineno": node.lineno,
                            "end_lineno": _end_lineno(node),
                        }
                    )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = node.module or ""
            for alias in node.names:
                name = alias.name
                asname = alias.asname
                full = f"{base}.{name}" if base else name
                imports.append(base or name)
                import_edges.append(
                    {
                        "record_type": "edge.import",
                        "language": "python",
                        "src_path": repo_rel_posix,
                        "dst_module": base or name,
                        "lineno": node.lineno,
                    }
                )
                if emit_ast:
                    ast_xrefs.append(
                        {
                            "record_type": "ast.xref",
                            "kind": "import_from",
                            "path": repo_rel_posix,
                            "module": module_name,
                            "from_module": base,
                            "name": name,
                            "asname": asname,
                            "level": getattr(node, "level", 0) or 0,
                            "lineno": node.lineno,
                            "end_lineno": _end_lineno(node),
                        }
                    )

        # --- classes -----------------------------------------------------------
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qual = _qualify(scope, node.name)
            classes.append(qual)
            if emit_ast:
                ast_symbols.append(
                    {
                        "record_type": "ast.symbol",
                        "kind": "class",
                        "path": repo_rel_posix,
                        "module": module_name,
                        "name": qual,
                        "bases": [_expr_to_name(b) for b in (node.bases or [])],
                        "decorators": _decorator_names(node),
                        "lineno": node.lineno,
                        "end_lineno": _end_lineno(node),
                    }
                )
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    ast_docs.append(
                        {
                            "record_type": "ast.docstring",
                            "path": repo_rel_posix,
                            "module": module_name,
                            "owner": qual,
                            "owner_kind": "class",
                            "doc": doc,
                            "lineno": node.lineno,
                            "end_lineno": _end_lineno(node),
                        }
                    )

            scope.append(_Scope("class", qual, node.lineno, node.col_offset))
            self.generic_visit(node)
            scope.pop()

        # --- functions (sync/async) -------------------------------------------
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_funclike(node, "function")

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_funclike(node, "async_function")

        def _visit_funclike(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef], kind: str) -> None:
            qual = _qualify(scope, node.name)
            functions.append(qual)

            if emit_ast:
                args = getattr(node, "args", None)
                arg_count = 0
                if args:
                    arg_count = len(getattr(args, "posonlyargs", []) or []) + len(getattr(args, "args", []) or [])
                    if getattr(args, "vararg", None):
                        arg_count += 1
                    arg_count += len(getattr(args, "kwonlyargs", []) or [])
                    if getattr(args, "kwarg", None):
                        arg_count += 1

                sym = {
                    "record_type": "ast.symbol",
                    "kind": "function",
                    "path": repo_rel_posix,
                    "module": module_name,
                    "name": qual,
                    "decorators": _decorator_names(node),
                    "lineno": node.lineno,
                    "end_lineno": _end_lineno(node),
                }
                ast_symbols.append(sym)
                ast_symmetrics.append(
                    {
                        "record_type": "ast.symbol_metrics",
                        "path": repo_rel_posix,
                        "module": module_name,
                        "name": qual,
                        "loc": max(0, _end_lineno(node) - node.lineno + 1),
                        "arg_count": arg_count,
                        "is_async": kind == "async_function",
                    }
                )
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    ast_docs.append(
                        {
                            "record_type": "ast.docstring",
                            "path": repo_rel_posix,
                            "module": module_name,
                            "owner": qual,
                            "owner_kind": "function",
                            "doc": doc,
                            "lineno": node.lineno,
                            "end_lineno": _end_lineno(node),
                        }
                    )

            scope.append(_Scope("function", qual, node.lineno, node.col_offset))
            self.generic_visit(node)
            scope.pop()

        # --- calls -------------------------------------------------------------
        def visit_Call(self, node: ast.Call) -> None:
            if emit_ast:
                caller = scope[-1].name if scope else module_name
                callee = _expr_to_name(node.func)
                ast_calls.append(
                    {
                        "record_type": "ast.call",
                        "path": repo_rel_posix,
                        "module": module_name,
                        "caller_name": caller,
                        "callee": callee,
                        "lineno": node.lineno,
                        "end_lineno": _end_lineno(node),
                    }
                )
            self.generic_visit(node)

    # module-level docstring
    mdoc = ast.get_docstring(tree, clean=False)
    if emit_ast and mdoc:
        ast_docs.append(
            {
                "record_type": "ast.docstring",
                "path": repo_rel_posix,
                "module": module_name,
                "owner": module_name,
                "owner_kind": "module",
                "doc": mdoc,
                "lineno": 1,
                "end_lineno": 1,
            }
        )

    Visitor().visit(tree)

    mod_rec: Dict[str, Any] = {
        "record_type": "module_index",
        "language": "python",
        "path": repo_rel_posix,
        "module": module_name,
        "classes": classes,
        "functions": functions,
        "imports": sorted(set(imports)),
    }

    extras: Optional[Dict[str, Any]] = None
    if emit_ast:
        extras = {
            "symbols": ast_symbols,
            "xrefs": ast_xrefs,
            "calls": ast_calls,
            "docstrings": ast_docs,
            "symbol_metrics": ast_symmetrics,
        }

    return mod_rec, import_edges, extras

