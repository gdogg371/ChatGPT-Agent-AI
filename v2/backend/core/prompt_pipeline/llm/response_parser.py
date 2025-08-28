#v2\backend\core\prompt_pipeline\llm\response_parser.py
r"""
Robust JSON response parsing for LLM outputs.

Goals
- Tolerate wrapping text (headers, code fences, trailing notes).
- Extract the first *balanced* JSON object and parse it strictly.
- If braces are unbalanced, attempt a minimal corrective balance (append '}')
  and parse; only then fail.

Exports
- parse_response(raw: str, expect_json: bool = True) -> dict | str
- parse_json_response(raw: str) -> dict
- parse_json(raw: str) -> dict
- extract_first_json_object(raw: str) -> str
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import json
import re


_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_code_fences(s: str) -> str:
    # Remove markdown code fences, if present
    return _CODE_FENCE_RE.sub("", s.strip())


def _find_first_brace_or_bracket(s: str) -> Tuple[int, str]:
    """Return (index, kind) where kind is '{' or '['; -1 if none."""
    i_obj = s.find("{")
    i_arr = s.find("[")
    if i_obj == -1 and i_arr == -1:
        return -1, ""
    if i_obj == -1:
        return i_arr, "["
    if i_arr == -1:
        return i_obj, "{"
    return (i_obj, "{") if i_obj < i_arr else (i_arr, "[")


def _extract_balanced_json(s: str, start: int, kind: str) -> Tuple[str, int]:
    """
    Scan from 'start' (points at '{' or '[') and return the substring of the
    first balanced JSON value, along with the index after the closing char.
    Handles nested pairs and strings with escapes.
    """
    open_ch = "{" if kind == "{" else "["
    close_ch = "}" if kind == "{" else "]"

    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(s):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return s[start : i + 1], i + 1
        i += 1
    raise ValueError("Unbalanced JSON braces in response")


def extract_first_json_object(raw: str) -> str:
    """
    Return the first balanced JSON object/array contained in 'raw'.
    Raises ValueError if no balanced value is found.
    """
    if not isinstance(raw, str):
        raise ValueError("Response is not a string")

    text = _strip_code_fences(raw)
    idx, kind = _find_first_brace_or_bracket(text)
    if idx == -1:
        raise ValueError("No JSON object found in response")
    obj, _ = _extract_balanced_json(text, idx, kind or "{")
    return obj


def _balance_minimally(s: str) -> str:
    """
    Minimal corrective strategy:
    - Trim to last '}' or ']' after the first opening.
    - If still unbalanced due to missing closers, append the exact number needed.
    """
    text = _strip_code_fences(s)
    start, kind = _find_first_brace_or_bracket(text)
    if start == -1:
        raise ValueError("No JSON object found in response")

    open_ch = "{" if kind == "{" else "["
    close_ch = "}" if kind == "{" else "]"

    # Trim trailing junk after the final closing brace/bracket, if present
    last_close = text.rfind(close_ch)
    if last_close != -1 and last_close >= start:
        candidate = text[start : last_close + 1]
    else:
        candidate = text[start:]

    # Count balance ignoring braces inside strings
    depth = 0
    in_str = False
    esc = False
    for ch in candidate:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1

    if depth > 0:
        candidate = candidate + (close_ch * depth)

    return candidate


def parse_json_response(raw: str) -> Dict[str, Any]:
    """
    Strict but resilient JSON parsing:
      1) Try direct json.loads on the stripped body (handles perfect JSON)
      2) Else, extract the first balanced { ... } or [ ... ] and json.loads that
      3) Else, try a minimal balancing fix and json.loads that
    """
    body = _strip_code_fences(raw)
    # 1) Direct parse
    try:
        return json.loads(body)
    except Exception:
        pass

    # 2) Balanced extract
    try:
        obj = extract_first_json_object(raw)
        return json.loads(obj)
    except Exception:
        pass

    # 3) Minimal balance + parse
    try:
        fixed = _balance_minimally(raw)
        return json.loads(fixed)
    except Exception as e:
        # Preserve legacy error message for upstream compatibility
        raise ValueError("Unbalanced JSON braces in response") from e


# Back-compat aliases
def parse_json(raw: str) -> Dict[str, Any]:
    return parse_json_response(raw)


def parse_response(raw: str, expect_json: bool = True):
    """
    Unified entrypoint:
      - If expect_json=True → return dict parsed from the first JSON value.
      - Else → return the raw string unchanged.
    """
    if not expect_json:
        return raw
    return parse_json_response(raw)


__all__ = [
    "parse_response",
    "parse_json_response",
    "parse_json",
    "extract_first_json_object",
]
