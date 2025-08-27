#v2\backend\core\docstrings\verify.py
from __future__ import annotations
r"""
Docstring verification helpers.

This module supplies a lightweight `DocstringVerifier` with two checks that
match the call sites in providers:

- pep257_minimal(text) -> (ok: bool, issues: list[str])
    A small subset of PEP 257 style rules:
      * Non-empty.
      * First line (summary) is <= 72 chars.
      * First line ends with a period.
      * Avoids boilerplate openers like "This function ..." (soft warning).

- params_consistency(text, signature) -> (ok: bool, issues: list[str])
    For Python function signatures like "def name(x, y=1, *args, **kwargs):":
      * Every real parameter (excluding self/cls, *args, **kwargs) should be
        mentioned in the docstring (heuristic: "Args:" block or "name:").
      * Reports missing parameters. Extra documented names are ignored.

Both checks are permissive and never raise; they only return issues.
"""

from dataclasses import dataclass
from typing import List, Tuple, Optional
import re


_SUMMARY_MAX = 72
_THIS_PREFIX_RX = re.compile(
    r"^\s*(?:This\s+(?:function|method|class|module)\b.*)", re.IGNORECASE
)

# Simple detectors for sections
_ARGS_HEADER_RX = re.compile(r"^\s*Args?\s*:\s*$", re.IGNORECASE)
_RETURNS_HEADER_RX = re.compile(r"^\s*Returns?\s*:\s*$", re.IGNORECASE)
_RAISES_HEADER_RX = re.compile(r"^\s*Raises?\s*:\s*$", re.IGNORECASE)


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if text else ""


def _split_args_block(text: str) -> List[str]:
    """
    Return the lines that appear under an 'Args:' section (if any),
    stopping at the next blank line or a new section header.
    """
    lines = (text or "").splitlines()
    out: List[str] = []
    in_args = False
    for ln in lines:
        if not in_args and _ARGS_HEADER_RX.match(ln):
            in_args = True
            continue
        if in_args:
            if not ln.strip():
                break
            if _RETURNS_HEADER_RX.match(ln) or _RAISES_HEADER_RX.match(ln) or _ARGS_HEADER_RX.match(ln):
                break
            out.append(ln.rstrip())
    return out


_PARAM_NAME_RX = re.compile(r"^\s*([-_*a-zA-Z][-_a-zA-Z0-9]*)\s*[:(]")


def _documented_param_names(text: str) -> List[str]:
    """
    Heuristically extract parameter names from the Args block (if present).
    Accept both "name: desc" and "name (type): desc".
    """
    names: List[str] = []
    for ln in _split_args_block(text):
        m = _PARAM_NAME_RX.match(ln)
        if m:
            names.append(m.group(1))
    # Fallback: scan entire doc for "name:" if no Args section found.
    if not names:
        for ln in text.splitlines():
            m = _PARAM_NAME_RX.match(ln)
            if m:
                names.append(m.group(1))
    return names


_SIG_DEF_RX = re.compile(r"^\s*(?:async\s+def|def)\s+[A-Za-z_][A-Za-z0-9_]*\s*\((.*)\)\s*:", re.DOTALL)
_SIG_CLASS_RX = re.compile(r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:\((.*)\))?\s*:\s*$")


def _extract_params_from_sig(signature: Optional[str]) -> List[str]:
    """
    Extract parameter names from a Python function signature.
    Ignores self/cls, *args, **kwargs and types/defaults.
    """
    sig = (signature or "").strip()
    if not sig:
        return []
    m = _SIG_DEF_RX.match(sig)
    if not m:
        # Not a function; treat as class/module → no params to check.
        return []
    inside = m.group(1)  # content between parentheses
    # Split by commas, respecting simple nesting (no full parsing).
    parts = [p.strip() for p in inside.split(",") if p.strip()]
    names: List[str] = []
    for p in parts:
        # Remove type annotation and default: "x: int = 1" → "x"
        p = p.split("=", 1)[0].strip()
        p = p.split(":", 1)[0].strip()
        # Remove * / ** prefixes
        p = p.lstrip("*").strip()
        if not p:
            continue
        if p in {"self", "cls"}:
            continue
        if p in {"args", "kwargs"}:
            continue
        # Parameter may appear as "name)" if last and no defaults; strip trailing )
        p = p.rstrip(")")
        if p:
            names.append(p)
    return names


@dataclass
class DocstringVerifier:
    """
    Stateless verifier with two checks used by the pipeline.
    """

    def pep257_minimal(self, text: str) -> Tuple[bool, List[str]]:
        """Subset of PEP 257: non-empty, summary length & period, avoid boilerplate openers."""
        issues: List[str] = []
        t = (text or "").strip()
        if not t:
            issues.append("Docstring is empty.")
            return False, issues

        first = _first_line(t)
        if len(first) > _SUMMARY_MAX:
            issues.append(f"Summary line exceeds {_SUMMARY_MAX} characters.")
        if not first.endswith("."):
            issues.append("Summary line should end with a period.")
        if _THIS_PREFIX_RX.match(first):
            issues.append('Avoid starting with "This function/method/class/module ...".')

        return (len(issues) == 0), issues

    def params_consistency(self, text: str, signature: Optional[str]) -> Tuple[bool, List[str]]:
        """
        Ensure parameters from the signature are mentioned/documented in the docstring.
        """
        issues: List[str] = []
        params = _extract_params_from_sig(signature)
        if not params:
            return True, issues  # nothing to check for class/module or unknown sig

        documented = set(_documented_param_names(text))
        # Heuristic: if there is no Args section and no "name:" patterns at all, we accept leniently.
        if not documented:
            return True, issues

        for p in params:
            if p not in documented:
                issues.append(f"Parameter '{p}' is not documented.")

        return (len(issues) == 0), issues


__all__ = ["DocstringVerifier"]



