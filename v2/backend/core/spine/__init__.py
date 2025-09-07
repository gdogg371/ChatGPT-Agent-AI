# File: v2/backend/core/spine/__init__.py
"""
Spine package facade.

Re-exports:
- registry singleton and runners
- loader facade shim

This ensures both import styles work:
  from v2.backend.core.spine import registry      # singleton object
  from v2.backend.core.spine import run           # module-level runner
  from v2.backend.core.spine import run_capability
  from v2.backend.core.spine import capability_run
"""

from __future__ import annotations

# Registry exports
from .registry import REGISTRY as registry  # singleton object
from .registry import run as run            # module-level runner
from .registry import run_capability as run_capability  # legacy name

# Loader facade (ensures caps are loaded; alt entrypoint)
from .loader import capability_run as capability_run  # convenience facade

__all__ = [
    "registry",
    "run",
    "run_capability",
    "capability_run",
]
