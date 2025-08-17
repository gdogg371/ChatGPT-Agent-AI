from __future__ import annotations

import re
from typing import Optional


_THIS_PREFIX_RX = re.compile(r"^\s*(?:This\s+(?:function|method|class|module)\s+(?:does|is|handles|returns)\b[\s:,-]*)", re.IGNORECASE)
_TRIPLE_RX = re.compile(r'^\s*("{3}|\'{3})|("{3}|\'{3})\s*$', re.MULTILINE)


def _strip_enclosing_triple_quotes(s: str) -> str:
    # model sometimes returns with triple quotes; remove any such wrappers
    return _TRIPLE_RX.sub("", s).strip()


def _infer_kind(signature: Optional[str]) -> str:
    sig = (signature or "").lstrip()
    if sig.startswith("def ") or sig.startswith("async def "):
        return "function"
    if sig.startswith("class "):
        return "class"
    return "module"


def _strip_ag_tags(lines: list[str]) -> list[str]:
    """Convert lines like 'AG-40: Feature text' → 'Feature text' and drop any leading 'AG Coverage:' header."""
    out: list[str] = []
    for ln in lines:
        if re.match(r"^\s*AG\s*[-–—]?\s*Coverage\s*:\s*$", ln, re.IGNORECASE):
            # drop the header; we'll inject our own "Capabilities:" later
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
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not lines:
        return "Add a concise summary.", []
    summary = lines[0].strip().rstrip(".")
    summary = _THIS_PREFIX_RX.sub("", summary).strip()
    # Make summary imperative-ish: start with a verb if model prefixed "Start the", "This function", etc.
    if summary.lower().startswith(("start ", "initialize ", "initialise ")):
        pass  # acceptable
    elif not summary:
        summary = "Add a concise summary"
    body = lines[1:]
    return summary + ".", body


def _ensure_sections_for_module(summary: str, body_lines: list[str]) -> str:
    """
    For module docstrings:
      - Keep summary line.
      - If body contains AG bullets (AG-xx: ...), convert to:
            Capabilities:
            - ...
            - ...
      - Otherwise keep body as-is.
    """
    # detect AG bullets and normalize
    has_ag = any(re.search(r"\bAG\s*[-–—]?\s*\d+\s*:", ln, re.IGNORECASE) for ln in body_lines)
    normalized = _strip_ag_tags(body_lines) if has_ag else body_lines[:]

    # If we now have a bullet-ish set, render under "Capabilities:"
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
    # Remove leading "This function/method..." in the body too
    lines = [ _THIS_PREFIX_RX.sub("", ln).rstrip() for ln in text.splitlines() ]
    # Remove redundant empty lines (max one blank between paragraphs)
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


def sanitize_docstring(
    raw: str,
    *,
    signature: Optional[str] = None,
    symbol_kind: Optional[str] = None,
) -> str:
    """
    Normalize model output to our house style:
      - NO enclosing triple quotes (we add them elsewhere).
      - Summary on the same line as opening quotes, <= ~72 chars, ends with period.
      - No 'This function/method/class/module...' phrasing.
      - For module docstrings: include a 'Capabilities:' section with dash bullets
        when input used AG-xx bullets; strip any AG labels/numbers.
      - Preserve Args/Returns/Raises sections for functions/classes if present.
    """
    text = _strip_enclosing_triple_quotes(raw)
    kind = (symbol_kind or _infer_kind(signature)).lower()

    # split into summary + body
    summary, body = _split_summary_and_body(text)

    if kind == "module":
        combined = _ensure_sections_for_module(summary, body)
    else:
        combined = summary + ("\n\n" + "\n".join(body) if body else "")

    # final grooming
    combined = _clean_common_issues(combined)

    # Guardrail: never return empty
    return (combined or "Add a concise summary.\n").rstrip() + "\n"
