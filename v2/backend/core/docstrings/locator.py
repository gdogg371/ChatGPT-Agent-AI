from __future__ import annotations

import ast
import re
from typing import List, Optional, Tuple


_DOCSTRING_RX = re.compile(r'^[ \t]*[rRuUbB]*("""|\'\'\')')
_ENCODING_RX = re.compile(r'^[ \t]*#.*coding[:=][ \t]*([-\w.]+)')


def _split_lines_keepends(src: str) -> List[str]:
    return src.splitlines(keepends=True)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _existing_docstring_span_for_node(node: ast.AST) -> Optional[Tuple[int, int]]:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
        if isinstance(first.value.value, str):
            start = getattr(first, "lineno", None)
            end = getattr(first, "end_lineno", None)
            if isinstance(start, int) and isinstance(end, int):
                return (start - 1, end - 1)
    return None


def find_module_docstring_span(src_text: str) -> Optional[Tuple[int, int]]:
    """
    If the file has a true AST module docstring, return (start_idx, end_idx), 0-based inclusive.
    """
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    span = _existing_docstring_span_for_node(tree)  # type: ignore[arg-type]
    return span


def find_symbol_docstring_span(src_text: str, target_lineno: int) -> Optional[Tuple[int, int]]:
    """
    Locate the function/class whose header starts at target_lineno (1-based) and
    return its docstring span if present, else None.
    """
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None

    node: Optional[ast.AST] = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if getattr(n, "lineno", None) == int(target_lineno):
                node = n
                break
    if node is None:
        return None
    return _existing_docstring_span_for_node(node)


def find_orphan_module_string_span(src_text: str, search_limit: int = 50) -> Optional[Tuple[int, int]]:
    """
    Find a top-level triple-quoted string near the top of the file (after shebang/encoding/imports).
    Return (start_idx, end_idx) inclusive if found, else None.
    """
    lines = _split_lines_keepends(src_text)

    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and _ENCODING_RX.match(lines[i] or ""):
        i += 1

    # Scan a small window, ignoring blank/comment/import lines
    for j in range(i, min(len(lines), i + search_limit)):
        s = lines[j]
        if s.strip() == "" or s.lstrip().startswith("#"):
            continue
        if s.lstrip().startswith(("import ", "from ")):
            continue
        # Only consider truly top-level (no indent)
        if s[: len(s) - len(s.lstrip(" \t"))] != "":
            break
        if _DOCSTRING_RX.match(s or ""):
            q = '"""' if '"""' in s else "'''"
            if s.count(q) >= 2:
                return (j, j)
            for k in range(j + 1, len(lines)):
                if q in lines[k]:
                    return (j, k)
            return None
        # First non-import/comment/blank line isn't a triple-quote start — stop.
        break
    return None
