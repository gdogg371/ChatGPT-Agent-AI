# File: v2/backend/core/prompt_pipeline/llm/router.py
from __future__ import annotations

"""
LLM router (domain-agnostic).

- select_schema_key(...): choose a schema key from hints (adapter, ask_spec).
- parse_with_schema(raw_text, schema_key): parse raw text into {"items":[...]}
"""

from typing import Any, Dict, Optional

from .schema import known_schema_keys
from .response_parser import parse_json_response


def select_schema_key(
    *,
    adapter_hint: Optional[str] = None,
    ask_spec: Optional[Dict[str, Any]] = None,
    default: str = "generic.v1",
) -> str:
    """
    Decide which schema to use.

    Heuristics (non-binding; adapters can override upstream):
      1) If ask_spec["schema_key"] is a known key, use it.
      2) If adapter_hint suggests a known key (e.g., "docstrings.v1"), use it.
      3) Fallback to `default` (generic.v1).
    """
    keys = set(known_schema_keys())

    if isinstance(ask_spec, dict):
        k = ask_spec.get("schema_key")
        if isinstance(k, str) and k in keys:
            return k

    if isinstance(adapter_hint, str) and adapter_hint in keys:
        return adapter_hint

    return default


def parse_with_schema(raw_text: str, schema_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse raw model text into {"items":[...]} with a selected schema key.

    NOTE: The current parser is schema-tolerant; the schema key is accepted for
    future extensibility and tooling but does not change parsing behavior today.
    """
    return parse_json_response(raw_text or "", schema_key=schema_key)

