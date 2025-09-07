from __future__ import annotations

from pathlib import Path
from typing import List, Tuple
import re


_ENCODING_RX = re.compile(r'^[ \t]*#.*coding[:=][ \t]*([-\w.]+)')
_IMPORT_RX = re.compile(r'^[ \t]*(import\s+\S+|from\s+\S+\s+import\b)')


def read_text_preserve(path: Path) -> str:
    """
    Read text preserving original newlines (no implicit translation).
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def write_text_preserve(path: Path, content: str) -> None:
    """
    Write text avoiding platform newline translation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(content)


def split_lines_keepends(text: str) -> List[str]:
    return text.splitlines(keepends=True)


def join_lines(lines: List[str]) -> str:
    return "".join(lines)


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def detect_eol(lines: List[str]) -> str:
    for ln in lines:
        if ln.endswith("\r\n"):
            return "\r\n"
        if ln.endswith("\r"):
            return "\r"
        if ln.endswith("\n"):
            return "\n"
    return "\n"


def coerce_eol(lines: List[str], eol: str) -> List[str]:
    return [ln.replace("\r\n", "\n").replace("\r", "\n").replace("\n", eol) for ln in lines]


def replace_span(lines: List[str], start_idx: int, end_idx: int, new_block: List[str]) -> List[str]:
    """
    Replace an inclusive [start_idx, end_idx] with new_block.
    """
    return lines[:start_idx] + new_block + lines[end_idx + 1:]


def insert_after_line(lines: List[str], line_idx: int, new_block: List[str]) -> List[str]:
    """
    Insert new_block after the 0-based line index.
    """
    at = min(max(line_idx + 1, 0), len(lines))
    return lines[:at] + new_block + lines[at:]


def after_shebang_and_encoding(lines: List[str]) -> int:
    """
    Return a 0-based index where it's safe to insert a top-of-file block,
    after shebang and encoding comment (if present). This index represents
    the position *before* which lines are left intact. Use insert_after_line(idx-1)
    or splice at idx directly.
    """
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and _ENCODING_RX.match(lines[i] or ""):
        i += 1
    return i


def after_import_block(lines: List[str]) -> int:
    """
    Return the index *after* the initial import block (skipping blank/comment lines).
    If no import block is found near the top, fall back to after shebang+encoding.
    """
    i = after_shebang_and_encoding(lines)
    j = i
    # allow interspersed blank/comment lines in the first block
    while j < len(lines):
        s = lines[j]
        if s.strip() == "" or s.lstrip().startswith("#"):
            j += 1
            continue
        if _IMPORT_RX.match(s):
            j += 1
            # keep consuming contiguous import/comment/blank lines
            while j < len(lines):
                t = lines[j]
                if t.strip() == "" or t.lstrip().startswith("#") or _IMPORT_RX.match(t):
                    j += 1
                else:
                    break
            return j
        break
    return i
