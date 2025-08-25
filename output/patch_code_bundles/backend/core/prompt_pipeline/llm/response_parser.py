# File: v2/backend/core/prompt_pipeline/llm/response_parser.py
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Set

from v2.backend.core.prompt_pipeline.executor.errors import ValidationError


_ARRAY_BLOCK_RX = re.compile(r"(\[\s*\{.*?\}\s*\])", flags=re.DOTALL)
_CODE_FENCE_RX = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)
_SMART_QUOTES = {
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "’": "'", "‘": "'", "‚": "'", "‛": "'",
}


def _strip_code_fences(s: str) -> str:
    return _CODE_FENCE_RX.sub("", s.strip())


def _normalize_quotes(s: str) -> str:
    for a, b in _SMART_QUOTES.items():
        s = s.replace(a, b)
    return s


def _extract_json_array_block(text: str) -> str | None:
    """
    Try to find a JSON array block like: [ { ... }, { ... } ] inside arbitrary text.
    Returns the first match or None.
    """
    m = _ARRAY_BLOCK_RX.search(text)
    return m.group(1) if m else None


class ResponseParser:
    """
    Strict-first parser for LLM responses with light salvage.

    Expected schema (preferred):
        {
          "items": [
            { "id": "<str>", "mode": "create|rewrite", "docstring": "<str>", ... },
            ...
          ]
        }

    Accepted alternative:
        [ { "id": "...", "docstring": "..." }, ... ]

    Validation:
      - Ensures each item is an object with a non-empty 'id' and 'docstring'
      - Deduplicates by 'id' (keeps first)
      - Verifies that all expected_ids are present
    """

    def __init__(self, expected_ids: Iterable[str]):
        self.expected: Set[str] = set(map(str, expected_ids or []))

    # ---- coercion -------------------------------------------------------------

    def _json_loads(self, raw: str) -> Any:
        return json.loads(raw)

    def _coerce(self, raw: str) -> List[Dict[str, Any]]:
        """
        Try strict JSON parse first. Accepts either an object with 'items' or
        a top-level array. If both fail, attempts minimal salvage by extracting
        a JSON array embedded in the text.
        """
        if not isinstance(raw, str):
            raise ValidationError("Raw response is not a string")

        text = _normalize_quotes(_strip_code_fences(raw))

        # 1) Strict parse
        try:
            obj = self._json_loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                return obj["items"]  # type: ignore[return-value]
            if isinstance(obj, list):
                return obj  # type: ignore[return-value]
        except json.JSONDecodeError:
            pass

        # 2) Minimal salvage: look for an embedded JSON array
        arr = _extract_json_array_block(text)
        if arr:
            try:
                obj2 = self._json_loads(arr)
                if isinstance(obj2, list):
                    return obj2  # type: ignore[return-value]
            except Exception:
                pass

        raise ValidationError("Response is not valid JSON (object with items[] or array)")

    # ---- parse & validate -----------------------------------------------------

    def parse(self, raw: str) -> List[Dict[str, Any]]:
        """
        Returns a list of dicts with at least: {"id": str, "docstring": str}
        Optionally passes through: "mode", "notes", and any other fields.
        """
        data = self._coerce(raw)
        if not isinstance(data, list):
            raise ValidationError("Top-level JSON must be an array or object with items[]")

        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValidationError(f"Item {i} is not an object")

            _id = str(item.get("id", "")).strip()
            _doc = item.get("docstring")

            if not _id:
                raise ValidationError(f"Item {i} missing id")
            if _id in seen:
                # drop dup, keep first occurrence
                continue
            if not isinstance(_doc, str) or not _doc.strip():
                raise ValidationError(f"Item {_id} has empty docstring")

            # pass through mode/notes/extras without being prescriptive
            out.append({
                "id": _id,
                "mode": str(item.get("mode", "rewrite")).lower() or "rewrite",
                "docstring": _doc,
                "notes": item.get("notes"),
                # keep all other keys for downstream if needed
                **{k: v for k, v in item.items() if k not in {"id", "mode", "docstring", "notes"}},
            })
            seen.add(_id)

        # Ensure we got all items we asked for
        missing = self.expected.difference({o["id"] for o in out})
        if missing:
            raise ValidationError(f"Missing ids in response: {sorted(missing)[:5]}")

        return out


# ---------------------------- Legacy shim --------------------------------------

def unpack_raw_results(raw: str, expected_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """
    Backwards-compat function used by older call-sites.

    Example:
        results = unpack_raw_results(raw_text_from_llm, expected_ids=["A","B","C"])
    """
    return ResponseParser(expected_ids).parse(raw)


__all__ = ["ResponseParser", "unpack_raw_results"]
