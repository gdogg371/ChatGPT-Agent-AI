from __future__ import annotations

import ast
import io
import re
from dataclasses import dataclass
from typing import Optional, Tuple


_DOCSTRING_RX = re.compile(r'^[ \t]*[ruRUbB]*("""|\'\'\')')
_ENCODING_RX = re.compile(r'^[ \t]*#.*coding[:=][ \t]*([-\w.]+)')


def _split_lines(src: str) -> list[str]:
    # Keep original newlines so CRLF survives via FileOps (which mirrors endings)
    return src.splitlines(keepends=True)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _guess_body_indent(lines: list[str], def_lineno1: int, fallback: str) -> str:
    """
    Guess inner-body indentation for a function/class by scanning the next
    non-empty, non-comment line after the 'def/class' header.
    """
    for i in range(def_lineno1, len(lines)):
        s = lines[i]
        if s.strip() == "" or s.lstrip().startswith("#"):
            continue
        return _indent_of(s)
    # default to fallback + 4 spaces
    return fallback + (" " * 4)


def _render_docstring_block(content: str, indent: str) -> list[str]:
    """
    Render a PEP-257 style docstring:
      - summary on same line as opening quotes
      - blank line after summary (when there is more content)
      - no backslash after opening quotes
    The `content` is the model output WITHOUT triple quotes.
    """
    # Normalize content
    text = (content or "").strip("\r\n")
    # Ensure there is at least a one-line summary
    if not text:
        text = "Add a concise summary."

    # Split into lines; first line is the summary
    lines = text.splitlines()
    first = lines[0].strip()

    # Rejoin the rest; keep user’s blank lines as-is
    rest = lines[1:]

    out: list[str] = []
    if rest:
        # Multi-line: summary line after opening quotes, blank line, then body
        out.append(f'{indent}"""{first}\n')
        # If next non-empty line is not blank, insert a blank line to follow PEP 257
        if rest and rest[0].strip() != "":
            out.append(f"{indent}\n")
        for ln in rest:
            out.append(f"{indent}{ln.rstrip()}\n")
        out.append(f'{indent}"""\n')
    else:
        # One-liner: keep summary on same line; still keep closing on its own next line for consistency
        out.append(f'{indent}"""{first}\n')
        out.append(f'{indent}"""\n')

    return out


def _find_existing_docstring_span(node: ast.AST, lines: list[str]) -> Optional[Tuple[int, int]]:
    """
    If the node already has a docstring, return (start_idx, end_idx) line indices (0-based, inclusive).
    """
    # For modules/classes/functions, docstring is the first statement being a Constant(Str)
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    # Python 3.12: docstring is ast.Expr(value=ast.Constant(str))
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
        if isinstance(first.value.value, str):
            # Lines are 1-based in AST; convert to 0-based indexes
            start = getattr(first, "lineno", None)
            end = getattr(first, "end_lineno", None)
            if isinstance(start, int) and isinstance(end, int):
                return (start - 1, end - 1)
    return None


def _insert_module_docstring(src_lines: list[str], block_lines: list[str]) -> list[str]:
    """
    Insert module docstring at the top, after shebang and encoding comment.
    """
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
    """
    Insert `new_block` AFTER the given 0-based line index.
    """
    insert_at = min(max(lineno0 + 1, 0), len(src_lines))
    return src_lines[:insert_at] + new_block + src_lines[insert_at:]


def apply_docstring_update(
    original_src: str,
    target_lineno: int,
    new_docstring: str,
    *,
    relpath: Optional[str] = None,
) -> str:
    """
    Update or create a docstring at `target_lineno` (1-based). If no symbol is found
    at that line, treat it as a module docstring update.
    - Ensures no backslash after opening quotes.
    - Summary is on the same line as opening quotes; blank line follows when multi-line.
    - Preserves indentation (guessed from the body or uses 4 spaces).
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
        # Possibly a module docstring
        if int(target_lineno) == 1:
            # Replace existing module docstring if present, else insert
            span = _find_existing_docstring_span(tree, lines)
            block = _render_docstring_block(new_docstring, indent="")
            if span:
                return "".join(_replace_span(lines, span[0], span[1], block))
            else:
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

