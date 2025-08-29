from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class TargetInfo:
    kind: str                  # "module" | "class" | "function"
    lineno: int                # def/class lineno, or 1 for module
    has_docstring: bool
    existing_docstring: Optional[str]
    signature: str             # e.g., "def foo(a: int) -> str:", "class Bar(Baz):", "module"
    indent: str                # indentation to use for the docstring body (e.g., "    ")
    node: ast.AST              # target node (Module|ClassDef|FunctionDef)


def parse_source(src: str, filename: str = "<src>") -> ast.Module:
    return ast.parse(src, filename=filename)


def _get_indent_of_line(src_lines: list[str], idx0: int) -> str:
    if idx0 < 0 or idx0 >= len(src_lines):
        return ""
    m = re.match(r"[ \t]*", src_lines[idx0])
    return m.group(0) if m else ""


def _compute_body_indent_for_node(src_lines: list[str], node: ast.AST) -> str:
    # For function/class: indent one level deeper than header line.
    header_indent = _get_indent_of_line(src_lines, (node.lineno - 1))
    # Try to infer child block indent width from the next non-blank line
    for i in range(node.lineno, min(len(src_lines), node.lineno + 20)):
        line = src_lines[i]
        if not line.strip():
            continue
        child_indent = _get_indent_of_line(src_lines, i)
        if len(child_indent) > len(header_indent):
            return child_indent
        break
    # Fallback to 4 spaces deeper
    return header_indent + (" " * 4)


def _class_signature(node: ast.ClassDef) -> str:
    # Python 3.12: ast.unparse available
    bases = [ast.unparse(b) for b in node.bases] if getattr(ast, "unparse", None) else []
    base_part = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{base_part}:"


def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    if getattr(ast, "unparse", None):
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{node.name}({args[1:-1]}){returns}:"
    # Fallback (very rare for 3.12)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}(...):"


def _first_statement_positions(mod: ast.Module) -> Tuple[int, int] | None:
    """Return (lineno, end_lineno) of the first statement in the module, or None if empty."""
    if not mod.body:
        return None
    first = mod.body[0]
    if hasattr(first, "lineno") and hasattr(first, "end_lineno"):
        return (first.lineno, first.end_lineno)  # type: ignore
    return None


def _module_has_docstring(mod: ast.Module) -> Tuple[bool, Optional[str]]:
    doc = ast.get_docstring(mod)
    return (doc is not None, doc)


def find_target_by_lineno(src: str, lineno: int, relpath: str = "") -> TargetInfo:
    """
    Given a file's source and a lineno hint (DB), determine the real target node:
    - exact match to a FunctionDef/ClassDef.lineno → that node
    - lineno == 1 (or <= 1) → module
    - otherwise: best-effort nearest enclosing def/class; fallback to module
    """
    tree = parse_source(src, filename=relpath or "<src>")
    src_lines = src.splitlines()

    # Exact match for functions/classes first
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno == lineno:
                if isinstance(node, ast.ClassDef):
                    has_doc = ast.get_docstring(node) is not None
                    return TargetInfo(
                        kind="class",
                        lineno=node.lineno,
                        has_docstring=has_doc,
                        existing_docstring=ast.get_docstring(node),
                        signature=_class_signature(node),
                        indent=_compute_body_indent_for_node(src_lines, node),
                        node=node,
                    )
                else:
                    has_doc = ast.get_docstring(node) is not None
                    return TargetInfo(
                        kind="function",
                        lineno=node.lineno,
                        has_docstring=has_doc,
                        existing_docstring=ast.get_docstring(node),
                        signature=_func_signature(node),
                        indent=_compute_body_indent_for_node(src_lines, node),
                        node=node,
                    )

    # Module-level target?
    if lineno <= 1:
        has_doc, doc = _module_has_docstring(tree)
        return TargetInfo(
            kind="module",
            lineno=1,
            has_docstring=has_doc,
            existing_docstring=doc,
            signature="module",
            indent="",  # top-level has no indent
            node=tree,
        )

    # Best-effort: pick nearest def/class whose block spans the lineno
    best: Optional[ast.AST] = None
    best_kind = "module"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # span available in 3.8+: end_lineno
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if start and end and start <= lineno <= end:
                best = node
                best_kind = "class" if isinstance(node, ast.ClassDef) else "function"
                break

    if best is not None:
        if isinstance(best, ast.ClassDef):
            has_doc = ast.get_docstring(best) is not None
            return TargetInfo(
                kind="class",
                lineno=best.lineno,
                has_docstring=has_doc,
                existing_docstring=ast.get_docstring(best),
                signature=_class_signature(best),
                indent=_compute_body_indent_for_node(src_lines, best),
                node=best,
            )
        else:
            has_doc = ast.get_docstring(best) is not None
            return TargetInfo(
                kind="function",
                lineno=best.lineno,
                has_docstring=has_doc,
                existing_docstring=ast.get_docstring(best),
                signature=_func_signature(best),
                indent=_compute_body_indent_for_node(src_lines, best),
                node=best,
            )

    # Fallback to module
    has_doc, doc = _module_has_docstring(tree)
    return TargetInfo(
        kind="module",
        lineno=1,
        has_docstring=has_doc,
        existing_docstring=doc,
        signature="module",
        indent="",
        node=tree,
    )
