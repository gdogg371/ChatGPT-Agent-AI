# File: v2/backend/core/docstrings/prompt_api.py
from __future__ import annotations

"""
Docstrings adapter API hooks referenced by the Spine capabilities.

These remain DOCSTRING-SPECIFIC but are isolated to this adapter.
They are tolerant of the generic item shapes introduced in the pipeline.
"""

from typing import Any, Dict, List, Optional


def context_build(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Optionally enrich items with adapter-specific context.
    Current implementation is conservative pass-through to avoid surprises.

    Payload:
      { "items": [ {...}, ... ], "options": { ... } }

    Returns:
      { "items": [ {...}, ... ] }    # MAY add/merge an inner 'context' mapping per item
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    items = list(payload.get("items") or [])
    # No-op enrichment (safe). Future: add static analysis to populate 'context' fields.
    return {"items": items}


def schema_select(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Return a preferred schema key for LLM outputs for this adapter.
    """
    return {"schema_key": "docstrings.v1"}


def results_unpack_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Optional hook to transform generic parsed items into adapter-specific structures.

    Current implementation: pass-through. The sanitize/verify steps already normalize
    into {id, docstring, new_docstring}, which patch application can consume.
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})
    items = list(payload.get("items") or [])
    return {"items": items}


