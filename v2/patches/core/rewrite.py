from __future__ import annotations

import ast
import re
from typing import Optional, Tuple

# Detect triple-quoted blocks (top-level) and encoding comment
_DOCSTRING_RX = re.compile(r'^[ \t]*[rRuUbB]*("""|\'\'\')')
_ENCODING_RX = re.compile(r'^[ \t]*#.*coding[:=][ \t]*([-\w.]+)')


def _split_lines(src: str) -> list[str]:
    """
    Split while preserving existing newline characters (CRLF/LF/R).
    This helps us keep the original file EOLs stable when we splice.
    """
    return src.splitlines(keepends=True)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _guess_body_indent(lines: list[str], def_lineno1: int, fallback: str) -> str:
    """
    Guess inner-body indentation for a function/class by scanning the next
    non-empty, non-comment line after the header. Fallback to `fallback + 4`.
    """
    for i in range(def_lineno1, len(lines)):
        s = lines[i]
        if s.strip() == "" or s.lstrip().startswith("#"):
            continue
        return _indent_of(s)
    # default to fallback + 4 spaces
    return fallback + (" " * 4)


def _render_docstring_block(content: str, indent: str) -> list[str]:
    """Render a PEP 257–style docstring with opening/closing quotes on their own lines.

    Layout (indent shown as «indent»):
    «indent»
    «indent»Summary line.

    «indent»Body…
    «indent»

    `content` is the inner text (no triple quotes).
    """
    text = (content or "").strip("\r\n")
    if not text:
        text = "Add a concise summary."

    lines = text.splitlines()
    summary = lines[0].rstrip()
    body = lines[1:]

    out: list[str] = []
    # Opening quotes on their own line
    out.append(f'{indent}"""\n')
    out.append(f"{indent}{summary}\n")

    if body:
        # Ensure exactly one blank line between summary and body
        out.append(f"{indent}\n")
        for ln in body:
            out.append(f"{indent}{ln.rstrip()}\n")

    # Closing quotes on their own line
    out.append(f'{indent}"""\n')
    return out


def _find_existing_docstring_span(node: ast.AST, lines: list[str]) -> Optional[Tuple[int, int]]:
    """
    If the node already has a docstring, return (start_idx, end_idx) line indices
    (0-based, inclusive).
    """
    body = getattr(node, "body", None)
    if not body:
        return None

    first = body[0]
    # Python 3.12+: docstring is ast.Expr(value=ast.Constant(str))
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
        if isinstance(first.value.value, str):
            start = getattr(first, "lineno", None)
            end = getattr(first, "end_lineno", None)
            if isinstance(start, int) and isinstance(end, int):
                return (start - 1, end - 1)
    return None


def _insert_module_docstring(src_lines: list[str], block_lines: list[str]) -> list[str]:
    """Insert module docstring at the top, after shebang and encoding comment."""
    i = 0
    # Shebang
    if i < len(src_lines) and src_lines[i].startswith("#!"):
        i += 1
    # Encoding
    if i < len(src_lines) and _ENCODING_RX.match(src_lines[i] or ""):
        i += 1
    # Insert block at i
    return src_lines[:i] + block_lines + src_lines[i:]


def _replace_span(src_lines: list[str], start: int, end: int, new_block: list[str]) -> list[str]:
    return src_lines[:start] + new_block + src_lines[end + 1:]


def _insert_after_line(src_lines: list[str], lineno0: int, new_block: list[str]) -> list[str]:
    """Insert `new_block` AFTER the given 0-based line index."""
    insert_at = min(max(lineno0 + 1, 0), len(src_lines))
    return src_lines[:insert_at] + new_block + src_lines[insert_at:]


def _find_orphan_module_string_span(lines: list[str], search_limit: int = 50) -> Optional[Tuple[int, int]]:
    """
    Find a top-level triple-quoted string near the top of the file (after shebang/encoding/imports).
    Use this as a fallback “module docstring” when AST reports none.
    Returns (start_idx, end_idx) inclusive, or None.
    """
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
            # Closing on same line?
            if s.count(q) >= 2:
                return (j, j)
            # Otherwise scan forward to the closing
            for k in range(j + 1, len(lines)):
                if q in lines[k]:
                    return (j, k)
            return None
        # first non-import, non-comment, non-blank that isn't a triple-quoted start — stop looking
        break
    return None


def apply_docstring_update(
    original_src: str,
    target_lineno: int,
    new_docstring: str,
    *,
    relpath: Optional[str] = None,
) -> str:
    """
    Update or create a docstring at `target_lineno` (1-based).

    If no symbol is found at that line, treat it as a module docstring update:
      1) Replace an existing AST module docstring if present,
      2) else replace a near-top “orphan” triple-quoted block if present,
      3) else insert at canonical module top.

    The renderer places opening/closing quotes on their own lines,
    with exactly one blank line between summary and body.
    """
    try:
        tree = ast.parse(original_src)
    except SyntaxError:
        # If the file cannot be parsed, return original unchanged
        return original_src

    lines = _split_lines(original_src)

    # Try to find a matching def/class at that line
    target_node: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if getattr(node, "lineno", None) == int(target_lineno):
                target_node = node
                break

    if target_node is None:
        # Module-level update requested
        if int(target_lineno) == 1:
            block = _render_docstring_block(new_docstring, indent="")

            # Prefer a true module docstring if AST finds one
            span = _find_existing_docstring_span(tree, lines)
            if span:
                return "".join(_replace_span(lines, span[0], span[1], block))

            # Else replace a near-top orphan triple-quoted block if present
            orphan = _find_orphan_module_string_span(lines)
            if orphan:
                return "".join(_replace_span(lines, orphan[0], orphan[1], block))

            # Else insert at canonical module top
            return "".join(_insert_module_docstring(lines, block))

        # If we cannot match a node or module top, bail out safely
        return original_src

    # Compute indentation
    header_line0 = max(int(getattr(target_node, "lineno", 1)) - 1, 0)
    header_indent = _indent_of(lines[header_line0])
    body_indent = _guess_body_indent(lines, header_line0 + 1, header_indent)

    # New block rendered with body indentation (docstring sits at body level)
    block = _render_docstring_block(new_docstring, indent=body_indent)

    # Replace existing docstring or insert right after header
    span = _find_existing_docstring_span(target_node, lines)
    if span:
        new_lines = _replace_span(lines, span[0], span[1], block)
    else:
        new_lines = _insert_after_line(lines, header_line0, block)

    return "".join(new_lines)
