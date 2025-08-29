# File: v2/backend/core/prompt_pipeline/llm/response_parser.py
"""
Lenient LLM response parsing.

- Accepts raw model text and tries hard to extract a JSON object with an 'items' list.
- Does NOT assume domain fields (e.g., 'docstring'). The engine/adapters handle that.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple, Union

from .schema import known_schema_keys


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", flags=re.IGNORECASE | re.MULTILINE)


def _strip_fences(text: str) -> str:
    if not text:
        return text
    return _FENCE_RE.sub("", text).strip()


def _loads_maybe(text: str) -> Union[Dict[str, Any], List[Any], None]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _coerce_items(obj: Any) -> Dict[str, Any]:
    """
    Normalize various shapes into {"items": [ ... ]}.
    Accepted inputs:
      - {"items":[...]}
      - {"results":[...]}
      - [ ... ]  -> becomes {"items":[...]}
      - {"choices":[{"message":{"content":"{...json...}"}}], ...} (OpenAI-style) -> best-effort
    """
    if obj is None:
        return {"items": []}

    if isinstance(obj, list):
        return {"items": [r for r in obj if isinstance(r, dict)]}

    if isinstance(obj, dict):
        if isinstance(obj.get("items"), list):
            return {"items": [r for r in obj.get("items") if isinstance(r, dict)]}
        if isinstance(obj.get("results"), list):
            return {"items": [r for r in obj.get("results") if isinstance(r, dict)]}
        # Try to unwrap OpenAI-like envelope
        ch = obj.get("choices")
        if isinstance(ch, list) and ch:
            # Attempt to parse content if it's a JSON string
            content = None
            msg = ch[0].get("message") if isinstance(ch[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, str):
                inner = _loads_maybe(_strip_fences(content))
                if isinstance(inner, (list, dict)):
                    return _coerce_items(inner)

    return {"items": []}


def parse_json_response(raw_text: str, schema_key: str | None = None) -> Dict[str, Any]:
    """
    Parse raw model output into {"items":[ ...dict... ]}.

    - If schema_key is provided and recognized, we still only *normalize* to 'items'
      and leave field interpretation to adapters.
    - If no valid JSON is found, returns {"items": []}.
    """
    text = _strip_fences(raw_text or "")
    if not text:
        return {"items": []}

    # First attempt: direct JSON
    obj = _loads_maybe(text)
    if obj is not None:
        return _coerce_items(obj)

    # Heuristic: find the first JSON object/array in the text
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [p for p in (start_obj, start_arr) if p != -1]
    if not starts:
        return {"items": []}
    start = min(starts)
    # Scan to the end by trying increasingly long substrings (bounded)
    for end in range(len(text), start, -1):
        candidate = text[start:end].strip()
        obj = _loads_maybe(candidate)
        if obj is not None:
            return _coerce_items(obj)

    return {"items": []}
