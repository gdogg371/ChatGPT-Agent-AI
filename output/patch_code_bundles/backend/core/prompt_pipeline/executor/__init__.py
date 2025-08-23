# File: v2/backend/core/prompt_pipeline/executor/__init__.py
from __future__ import annotations

"""
Public exports for the executor package.

This package is now a *client* of the spine. It does not import other
feature domains directly. Backwards-compat shims are provided so older
imports continue to work while callers migrate to the spine.
"""

from .orchestrator import Orchestrator  # B/C shim: prefer calling Spine directly

__all__ = ["Orchestrator"]
