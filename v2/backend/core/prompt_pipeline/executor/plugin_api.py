# File: v2/backend/core/prompt_pipeline/executor/plugin_api.py
"""
Plugin-facing dataclasses and types for the general-purpose prompt pipeline.

This module MUST remain domain-agnostic. Adapters can attach domain-specific
data inside the generic `meta` mapping.

Backward compatibility:
- `new_docstring` remains as an optional legacy field for older adapters.
  New code should use `result` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Item:
    id: str
    path: Optional[str] = None
    relpath: Optional[str] = None
    signature: Optional[str] = None
    lang: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    """
    Generic result from LLM/adapters.

    - `result` holds the primary textual output (domain-agnostic).
    - `meta` can hold any extra structured fields the adapter wants to pass.
    - `new_docstring` is retained for legacy consumers (deprecated).
    """
    id: str
    result: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    # Legacy/compat field (deprecated; do not rely on this in new code)
    new_docstring: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {"id": self.id, "result": self.result, "meta": dict(self.meta)}
        if self.new_docstring is not None:
            # keep legacy key for downstream code still expecting it
            out["new_docstring"] = self.new_docstring
        return out


@dataclass
class Batch:
    id: str
    messages: List[Dict[str, str]]
    ask_spec: Dict[str, Any] = field(default_factory=dict)
