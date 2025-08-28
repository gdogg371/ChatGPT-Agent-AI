from __future__ import annotations

"""
Response-format helpers for LLM calls.

This module centralizes JSON Schema definitions and exposes a single helper
to resolve them by friendly name. The intent is to keep routing logic and
schema selection clean and testable.

Design goals
------------
- Provider-agnostic: we describe the *shape* we expect; the LLM client decides
  how to pass this to a given provider (e.g., OpenAI Responses API).
- Extensible: add new named formats without touching the orchestrator.
- Defensive: unknown names return None (caller decides fallback).
"""

from typing import Any, Dict, Optional


def _docstrings_v1_schema() -> Dict[str, Any]:
    """
    Schema for docstring updates.

    Expected model output:
    {
      "items": [
        {"id": "stable-id", "mode": "insert|replace|skip", "docstring": "..."},
        ...
      ]
    }

    Constraints:
      - 'items' is required.
      - Each item must include 'id' and 'mode'. 'docstring' required unless mode="skip".
      - No additional properties are allowed to reduce drift.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "docstrings_v1",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "mode"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1},
                                "mode": {
                                    "type": "string",
                                    "enum": ["insert", "replace", "skip"],
                                },
                                "docstring": {"type": "string"},
                                # Optional diagnostics channel if your parser wants it
                                "notes": {"type": "string"},
                            },
                            "allOf": [
                                {
                                    "if": {
                                        "properties": {"mode": {"const": "skip"}},
                                        "required": ["mode"],
                                    },
                                    "then": {
                                        "not": {"required": ["docstring"]}
                                    },
                                }
                            ],
                        },
                    }
                },
            },
            # Tighten model behavior in providers that honor these hints.
            "strict": True,
        },
    }


# Registry of named response formats.
# Add new entries as your pipeline grows (e.g., "type_hints.v1", "lint_fixes.v1").
_RESPONSE_FORMATS: Dict[str, Dict[str, Any]] = {
    "docstrings.v1": _docstrings_v1_schema(),
}


def get_response_format_by_name(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Resolve a friendly response-format name to a schema payload the LLM client can use.

    Parameters
    ----------
    name : Optional[str]
        Friendly handle like "docstrings.v1". If None or unknown, returns None.

    Returns
    -------
    Optional[Dict[str, Any]]
        A provider-agnostic response_format dict (e.g., for OpenAI Responses API),
        or None if no format should be enforced.
    """
    if not name:
        return None
    fmt = _RESPONSE_FORMATS.get(name)
    return fmt.copy() if isinstance(fmt, dict) else None


__all__ = ["get_response_format_by_name"]
