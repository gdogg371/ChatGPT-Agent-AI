# File: v2/backend/core/prompt_pipeline/executor/__init__.py
"""
Executor package exports.

We re-export the orchestrator entry point and selected provider helpers so
callers can do:
    from v2.backend.core.prompt_pipeline.executor import capability_run, get_capability_map
without coupling to submodules.
"""

from __future__ import annotations

# Public orchestrator entry-point for capability execution
from .orchestrator import capability_run  # noqa: F401

# Provider-side utilities (alias map + local glue capabilities)
from .providers import get_capability_map, enrich_v1  # noqa: F401

__all__ = [
    "capability_run",
    "get_capability_map",
    "enrich_v1",
]

