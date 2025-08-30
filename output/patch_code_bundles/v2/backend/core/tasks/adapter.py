# File: v2/backend/core/tasks/adapter.py
"""
Generic task adapters.

This module MUST stay domain-agnostic. Do not import from v2.backend.core.docstrings.*.
If a pipeline needs sanitization, call the Spine capability `sanitize.v1`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Generic orchestrator entrypoint (domain-agnostic)
from v2.backend.core.prompt_pipeline.executor.orchestrator import capability_run  # type: ignore


def _unwrap_meta(arts: Any) -> Dict[str, Any]:
    """Unwrap the first artifact/meta/result payload into a dict."""
    if not arts:
        return {}
    first = arts[0]
    meta = getattr(first, "meta", None) if not isinstance(first, dict) else first.get("meta")
    if isinstance(meta, dict):
        return meta.get("result") or meta
    if isinstance(first, dict):
        return first
    return {}


class GenericSanitizerAdapter:
    """
    Generic adapter that uses the Spine registry to sanitize items.

    It calls: capability "sanitize.v1" with payload {"items": [...]} and expects
    a result of the form {"items":[...]}.
    """

    def sanitize(self, items: List[Dict[str, Any]], *, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        ctx = {"phase": "TASKS.SANITIZE"}
        if context:
            ctx.update(context)
        arts = capability_run("sanitize.v1", {"items": items}, ctx)
        result = _unwrap_meta(arts)
        out = result.get("items")
        return out if isinstance(out, list) else items


# Back-compat alias some callers might reference
DocstringsAdapter = GenericSanitizerAdapter
