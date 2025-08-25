# File: v2/backend/core/docstrings/sanitize.py
from __future__ import annotations

import re
import textwrap
from typing import Dict, Iterable, List

# ---------------------------------------------------------------------------
# Docstring sanitization
# - Removes markdown fences, boilerplate disclaimers, and obvious junk
# - Dedents, normalizes whitespace, limits consecutive blank lines
# - Enforces conservative length limits without breaking mid-paragraph
# - Keeps the payload shape (id/mode/docstring/notes/extras)
# ---------------------------------------------------------------------------

# Markers that indicate low-value or hallucinated content to drop
_BANNED_SNIPPETS = (
    "as an ai language model",
    "cannot assist with this request",
    "i am unable to",
    "i’m unable to",
    "i cannot",
    "sorry, but i",
    "hallucination",
    "this is a placeholder",
    "lorem ipsum",
    "bad docstring",
    "duplicate docstring — skipped",
    "please see the attached",
)

_CODE_FENCE_RX = re.compile(r"^\s*```(?:\w+)?\s*$|^\s*```\s*$", re.IGNORECASE)
_TRAILING_WS_RX = re.compile(r"[ \t]+$", re.MULTILINE)


def _strip_markdown_fences(s: str) -> str:
    lines = s.splitlines()
    if not lines:
        return s
    # remove leading/trailing code fences only; keep interior backticks untouched
    if lines and _CODE_FENCE_RX.match(lines[0]):
        lines = lines[1:]
    if lines and _CODE_FENCE_RX.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def _normalize_whitespace(s: str) -> str:
    # normalize newlines, strip trailing spaces, dedent, collapse outer empties
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _TRAILING_WS_RX.sub("", s)
    s = textwrap.dedent(s)
    s = s.strip("\n")
    return s


def _limit_blank_runs(s: str, max_consecutive: int = 2) -> str:
    out: List[str] = []
    run = 0
    for line in s.splitlines():
        if line.strip() == "":
            run += 1
            if run > max_consecutive:
                continue
        else:
            run = 0
        out.append(line)
    return "\n".join(out)


def _is_banned(s: str) -> bool:
    low = s.lower()
    return any(snippet in low for snippet in _BANNED_SNIPPETS)


def _trim_to_budget(s: str, max_chars: int = 4000, max_lines: int = 60) -> str:
    # hard limits; prefer cutting at paragraph boundaries
    if len(s) <= max_chars and s.count("\n") + 1 <= max_lines:
        return s
    lines = s.splitlines()
    # first, cap lines
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    s = "\n".join(lines)
    # then cap characters, cutting at last full sentence/paragraph if possible
    if len(s) > max_chars:
        slice_ = s[:max_chars]
        # try to end at last period followed by space or newline
        m = re.search(r"[\.!?](?:\s|\n)+", slice_[::-1])
        if m:
            cut = len(slice_) - m.start()
            s = slice_[:cut].rstrip()
        else:
            s = slice_.rstrip()
    return s


def _sanitize_text(s: str) -> str:
    s = _strip_markdown_fences(s or "")
    s = _normalize_whitespace(s)
    if _is_banned(s) or not s.strip():
        return ""
    s = _limit_blank_runs(s, max_consecutive=2)
    s = _trim_to_budget(s, max_chars=4000, max_lines=60)
    return s.strip()


def sanitize_docstring(s: str) -> str:
    """
    Back-compat alias used by existing imports in v2.backend.core.docstrings.__init__.
    Cleans a single docstring and returns the sanitized text (or empty string if dropped).
    """
    return _sanitize_text(str(s or ""))


def sanitize_rows(rows: Iterable[Dict]) -> List[Dict]:
    """
    Sanitize a sequence of LLM-produced docstring rows.

    Input row schema (minimum):
        {"id": "<str>", "mode": "<create|rewrite>", "docstring": "<str>", ...}

    Returns only rows with a non-empty cleaned 'docstring'. Preserves unknown keys.
    """
    out: List[Dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id", "")).strip()
        if not rid:
            continue
        doc = r.get("docstring", "")
        if not isinstance(doc, str):
            continue
        clean = _sanitize_text(doc)
        if not clean:
            continue
        # preserve mode, notes, extras, etc.
        o = dict(r)
        o["docstring"] = clean
        out.append(o)
    return out


__all__ = ["sanitize_docstring", "sanitize_rows"]

