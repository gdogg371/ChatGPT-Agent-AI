#v2/backend/core/spine/__init__.py
"""
Spine public API (YAML-driven; no env middleware loader).

Exports:
- Spine, build_spine, setup_registry, load_middlewares_from_config
- to_dict, Artifact, Task, Envelope, new_envelope

Note:
- Replaced deprecated `load_middlewares_from_env` with
  `load_middlewares_from_config` to align with centralized YAML config.
"""

from __future__ import annotations

from .bootstrap import (
    Spine,
    build_spine,
    setup_registry,
    load_middlewares_from_config,
)
from .contracts import (
    to_dict,
    Artifact,
    Task,
    Envelope,
    new_envelope,
)

__all__ = [
    "Spine",
    "build_spine",
    "setup_registry",
    "load_middlewares_from_config",
    "to_dict",
    "Artifact",
    "Task",
    "Envelope",
    "new_envelope",
]




