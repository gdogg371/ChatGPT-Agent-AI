#v2\backend\core\docstrings\sanitize.py
from __future__ import annotations
r"""
Docstring sanitizer.

Normalizes model output into a consistent house style:
- Strips enclosing triple quotes if the model returned them.
- Summary on the first line, <= ~72 chars, ends with a period.
- Removes “This function/method/class/module …” boilerplate.
- For module docstrings: if AG-xx bullets are present, render under a
  “Capabilities:” section with clean dash bullets.
- Preserves typical sections (Args/Returns/Raises) when present.

Design:
- Pure functions, no I/O, no env vars.
- Be tolerant to upstream schema changes: `sanitize_docstring` accepts
  extra keyword args without failing (e.g., `signature`).
"""

import re
from typing import Optional

_THIS_PREFIX_RX = re.compile(
    r"^\s*(?:This\s+(?:function|method|class|module)\s+(?:does|is|handles|returns)\b[\s:,-]*)",
    re.IGNORECASE,
)
_TRIPLE_RX = re.compile(r'^\s*("{3}|\'{3})|("{3}|\'{3})\s*$', re.MULTILINE)


def _strip_enclosing_triple_quotes(s: str) -> str:
    """Remove enclosing triple quotes if the model wrapped the text."""
    return _TRIPLE_RX.sub("", s).strip()


def _infer_kind(signature: Optional[str]) -> str:
    """Best-effort symbol kind inference from a Python signature string."""
    sig = (signature or "").lstrip()
    if sig.startswith("def ") or sig.startswith("async def "):
        return "function"
    if sig.startswith("class "):
        return "class"
    return "module"


def _strip_ag_tags(lines: list[str]) -> list[str]:
    """
    Convert lines like `AG-40: Feature text` → `Feature text` and drop any
    `AG Coverage:` header lines.
    """
    out: list[str] = []
    for ln in lines:
        if re.match(r"^\s*AG\s*[-–—]?\s*Coverage\s*:\s*$", ln, re.IGNORECASE):
            # drop the header; we inject our own "Capabilities:" later
            continue
        m = re.match(r"^\s*-?\s*AG\s*[-–—]?\s*\d+\s*:\s*(.*)$", ln, re.IGNORECASE)
        if m:
            out.append(m.group(1).strip())
        else:
            # also handle bullets that already omit the AG tag
            m2 = re.match(r"^\s*-\s*(.*)$", ln)
            out.append((m2.group(1) if m2 else ln).rstrip())
    return [x for x in out if x.strip()]


def _split_summary_and_body(text: str) -> tuple[str, list[str]]:
    """Return (summary, body_lines) with a non-empty summary."""
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not lines:
        return "Add a concise summary.", []
    summary = lines[0].strip().rstrip(".")
    summary = _THIS_PREFIX_RX.sub("", summary).strip()
    # Keep simple imperative-ish summary; if empty, provide a default.
    if summary.lower().startswith(("start ", "initialize ", "initialise ")):
        pass  # acceptable opening verb
    elif not summary:
        summary = "Add a concise summary"
    body = lines[1:]
    return summary + ".", body


def _ensure_sections_for_module(summary: str, body_lines: list[str]) -> str:
    """
    For module docstrings:
    - Keep the summary line.
    - If body contains AG bullets (AG-xx: ...), render them under "Capabilities:".
    - Otherwise keep body as-is.
    """
    has_ag = any(re.search(r"\bAG\s*[-–—]?\s*\d+\s*:", ln, re.IGNORECASE) for ln in body_lines)
    normalized = _strip_ag_tags(body_lines) if has_ag else body_lines[:]

    bullets = [ln for ln in normalized if ln.strip() and not ln.strip().endswith(":")]
    is_bulleted = any(ln.strip().startswith("-") for ln in body_lines) or has_ag

    out: list[str] = [summary, ""]
    if is_bulleted and bullets:
        out.append("Capabilities:")
        for ln in bullets:
            # force dash bullets, strip any leading dashes
            ln = re.sub(r"^\s*-\s*", "", ln).strip()
            if ln:
                out.append(f"- {ln}")
    else:
        out.extend(normalized)

    return "\n".join(out).rstrip() + "\n"


def _clean_common_issues(text: str) -> str:
    """Remove boilerplate and collapse multiple blank lines."""
    lines = [_THIS_PREFIX_RX.sub("", ln).rstrip() for ln in text.splitlines()]
    cleaned: list[str] = []
    blank = False
    for ln in lines:
        if ln.strip() == "":
            if not blank:
                cleaned.append("")
            blank = True
        else:
            cleaned.append(ln)
            blank = False
    return "\n".join(cleaned).strip()


def sanitize_docstring(text: str, *, signature: Optional[str] = None, symbol_kind: Optional[str] = None) -> str:
    """
    Normalize a docstring produced by the LLM without inventing content.
    - Remove surrounding triple quotes if present.
    - Strip leading/trailing whitespace.
    - Normalize newlines to '\n'.
    - Ensure a trailing newline ONLY if content is non-empty.
    - DO NOT add any placeholder/default text if empty.
    """
    s = (text or "")
    # Strip code fences that models sometimes add
    if s.strip().startswith("```"):
        # remove outermost fenced block
        lines = s.strip().splitlines()
        if lines and lines[0].startswith("```"):
            lines = [ln for ln in lines[1:] if not ln.startswith("```")]
            s = "\n".join(lines)

    s = s.strip()

    # Remove triple quotes if the model returned a quoted docstring
    if (s.startswith('"""') and s.endswith('"""')) or (s.startswith("'''") and s.endswith("'''")):
        s = s[3:-3].strip()

    # Normalize newlines
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Return as-is; if empty, leave it empty (let verifier decide)
    if not s:
        return ""

    # Ensure trailing newline for consistency
    if not s.endswith("\n"):
        s += "\n"
    return s



