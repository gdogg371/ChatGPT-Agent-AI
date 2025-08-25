# File: v2/backend/core/docstrings/verify.py
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

# ---------------------------------------------------------------------------
# Docstring verifier
# - Structural sanity checks for LLM-produced docstrings
# - Conservative limits to prevent pathological outputs
# - Returns (all_ok, reports) where each report = {"id", "ok", "reason", "warnings"}
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_LINES = 60
DEFAULT_MAX_LINE_LEN = 200


_BANNED_TOKENS = (
    "```",                 # leftover code fences
    "<script",             # obvious HTML/script injection
    "</script",
    "as an ai language model",
    "i cannot", "i’m unable to", "i am unable to",
)


def _mk_report(rid: str, ok: bool, reason: str = "ok", warnings: List[str] | None = None) -> Dict[str, str]:
    rep: Dict[str, str] = {"id": rid, "ok": bool(ok), "reason": reason}
    if warnings:
        rep["warnings"] = "; ".join(warnings)
    return rep


def _basic_checks(rid: str, doc: str) -> Tuple[bool, str, List[str]]:
    """
    Return (ok, reason, warnings) for a single docstring.
    """
    if not rid:
        return False, "missing id", []
    if not isinstance(doc, str):
        return False, "docstring not a string", []
    if not doc.strip():
        return False, "empty docstring", []

    low = doc.lower()
    for tok in _BANNED_TOKENS:
        if tok in low:
            return False, f"banned token: {tok}", []

    warnings: List[str] = []
    # Soft guidance: line length checks etc.
    lines = doc.splitlines()
    for i, line in enumerate(lines, 1):
        if len(line) > DEFAULT_MAX_LINE_LEN:
            warnings.append(f"line {i} too long ({len(line)} chars)")

    return True, "ok", warnings


def _budget_checks(doc: str, max_chars: int, max_lines: int) -> Tuple[bool, str]:
    if len(doc) > max_chars:
        return False, f"too long ({len(doc)} chars > {max_chars})"
    line_count = doc.count("\n") + 1
    if line_count > max_lines:
        return False, f"too many lines ({line_count} > {max_lines})"
    return True, "ok"


def verify_rows(
    rows: Iterable[Dict],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> Tuple[bool, List[Dict]]:
    """
    Verify a sequence of rows in the shape:
        {"id": "<str>", "mode": "<create|rewrite>", "docstring": "<str>", ...}

    Returns:
        (all_ok, reports)
      where each report: {"id": str, "ok": bool, "reason": str, "warnings": "a; b; c"}
    """
    all_ok = True
    reports: List[Dict] = []

    for r in rows or []:
        rid = str(r.get("id", "")).strip()
        doc = r.get("docstring", "")

        ok, reason, warns = _basic_checks(rid, doc if isinstance(doc, str) else "")
        if ok:
            ok2, reason2 = _budget_checks(doc, max_chars=max_chars, max_lines=max_lines)
            if not ok2:
                ok, reason = False, reason2

        reports.append(_mk_report(rid or "?", ok, reason, warns))
        if not ok:
            all_ok = False

    # If there were no rows at all, that's a failure in this stage.
    if not reports:
        return False, [ _mk_report("?", False, "no rows to verify") ]

    return all_ok, reports


# -------------------------- Back-compat OO wrapper ----------------------------

class DocstringVerifier:
    """
    Backwards-compatible class used by existing imports.

    Usage:
        v = DocstringVerifier(max_chars=4000, max_lines=60)
        ok, reports = v.verify(rows)
    """
    def __init__(self, *, max_chars: int = DEFAULT_MAX_CHARS, max_lines: int = DEFAULT_MAX_LINES) -> None:
        self.max_chars = max_chars
        self.max_lines = max_lines

    def verify(self, rows: Iterable[Dict]) -> Tuple[bool, List[Dict]]:
        return verify_rows(rows, max_chars=self.max_chars, max_lines=self.max_lines)


__all__ = ["verify_rows", "DocstringVerifier"]


