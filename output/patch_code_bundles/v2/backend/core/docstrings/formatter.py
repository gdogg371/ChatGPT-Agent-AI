from __future__ import annotations

import re
import textwrap
from typing import List, Tuple


def _normalize_lines(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]
    out, blanks = [], 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks <= 2:
                out.append("")
        else:
            blanks = 0
            out.append(ln)
    return "\n".join(out).strip()


def _split_summary_body(s: str) -> Tuple[str, str]:
    s = s.strip()
    parts = re.split(r"\n\s*\n", s, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    m = re.search(r"([.!?])(\s+|$)", s)
    if m and m.end() < len(s):
        return s[:m.end()].strip(), s[m.end():].strip()
    return s, ""


def _wrap_paragraph(text: str, width: int) -> str:
    if not text.strip():
        return ""
    lines = text.split("\n")
    out: List[str] = []
    para: List[str] = []

    def flush():
        if not para:
            return
        block = " ".join(x.strip() for x in para).strip()
        out.append(textwrap.fill(block, width=width, break_long_words=False, break_on_hyphens=False))
        para.clear()

    for ln in lines:
        if ln.strip().startswith(("- ", "* ")):
            flush()
            bullet, rest = ln[:2], ln[2:].strip()
            wrapped = textwrap.fill(
                rest,
                width=max(4, width - 2),
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            parts = wrapped.split("\n")
            if parts:
                out.append(bullet + parts[0])
                out.extend("  " + p for p in parts[1:])
            else:
                out.append(bullet)
        elif ln.strip() == "":
            flush()
            out.append("")
        else:
            para.append(ln)
    flush()

    # collapse >2 blanks
    cleaned: List[str] = []
    blanks = 0
    for ln in out:
        if ln == "":
            blanks += 1
            if blanks <= 2:
                cleaned.append("")
        else:
            blanks = 0
            cleaned.append(ln.rstrip())
    return "\n".join(cleaned).strip()


_TRIPLE_QUOTE_RE = re.compile(r'^\s*(?P<q>("""|\'\'\'))(?P<body>.*?)(?P=q)\s*$', re.S)


def strip_triple_quotes(raw: str) -> str:
    m = _TRIPLE_QUOTE_RE.match(raw)
    return m.group("body") if m else raw


def format_inner_docstring(raw: str, width: int = 72) -> str:
    """
    Returns the *inner* content that goes between triple quotes,
    with exactly one blank line between summary and body (when body exists).
    Guarantees no trailing spaces; returns content with a leading and trailing '\n'
    so the renderer can put quotes on their own lines neatly.
    """
    raw = strip_triple_quotes(raw)
    norm = _normalize_lines(raw)
    summary, body = _split_summary_body(norm)
    summary_wrapped = _wrap_paragraph(summary, width)
    body_wrapped = _wrap_paragraph(body, width) if body else ""
    inner = summary_wrapped + (("\n\n" + body_wrapped) if body_wrapped else "")
    inner = inner.strip()
    return "\n" + inner + "\n"


def render_docstring_block(inner_content: str, indent: str = "") -> List[str]:
    """
    Produce a docstring block with opening/closing quotes on their own lines.
    Returns a list of lines (each ends with '\n'; caller can coerce to EOL).
    """
    text = (inner_content or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        text = "\nAdd a concise summary.\n"

    # Ensure leading/trailing single newline
    if not text.startswith("\n"):
        text = "\n" + text
    if not text.endswith("\n"):
        text = text + "\n"

    lines = text.splitlines(keepends=False)

    out: List[str] = []
    out.append(f'{indent}"""\n')
    # First line after opening quotes: summary
    out.append(f"{indent}{lines[0].rstrip()}\n")
    # Rest of body with at most one blank line between summary and body
    if len(lines) > 1:
        # if not already blank, add exactly one blank line
        if lines[1].strip() != "":
            out.append(f"{indent}\n")
        for ln in lines[1:]:
            out.append(f"{indent}{ln.rstrip()}\n")
    out.append(f'{indent}"""\n')
    return out
