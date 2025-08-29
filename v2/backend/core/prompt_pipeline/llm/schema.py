# File: v2/backend/core/prompt_pipeline/llm/schema.py
"""
Schema registry for LLM result shapes.

The engine is domain-agnostic; adapters select a schema key (e.g., via Spine
capability `schema.select`). Parsers/providers should avoid hard-coding any
domain fields and instead accept whichever schema the adapter chooses.

Provided:
- known_schema_keys() -> List[str]
- get_schema(key: str) -> Dict[str, Any]
"""

from __future__ import annotations

from typing import Any, Dict, List


# Minimal, declarative descriptions (human-readable; not strict validators)
_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # Generic, domain-neutral: items carry a primary 'result' and optional 'meta'
    "generic.v1": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "result": {"type": ["string", "null"]},
                        "meta": {"type": "object"},
                    },
                    "required": ["id"],
                },
            }
        },
    },

    # Docstrings-oriented: kept for adapter use only; NOT referenced by core logic.
    "docstrings.v1": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "docstring": {"type": ["string", "null"]},
                        "mode": {"type": ["string", "null"]},
                    },
                    "required": ["id"],
                },
            }
        },
    },
}


def known_schema_keys() -> List[str]:
    """Return all known schema keys."""
    return list(_SCHEMAS.keys())


def get_schema(key: str) -> Dict[str, Any]:
    """Return a schema descriptor for a given key, or an empty dict if unknown."""
    return dict(_SCHEMAS.get(key, {}))
