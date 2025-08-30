# File: v2/backend/core/prompt_pipeline/executor/__init__.py
"""
Executor package exports.

We re-export the orchestrator entry point and selected provider helpers so
callers can do:

    from v2.backend.core.prompt_pipeline.executor import capability_run, enrich_v1, unpack_results_v1

without coupling to submodules.

Note:
- Do NOT import any legacy get_capability_map here; Spine owns capability resolution.
"""
from __future__ import annotations

# Public orchestrator entry-point for capability execution
from .orchestrator import capability_run  # noqa: F401

# Provider-side utilities (generic, domain-agnostic)
from .providers import enrich_v1, unpack_results_v1  # noqa: F401

__all__ = [
    "capability_run",
    "enrich_v1",
    "unpack_results_v1",
]

