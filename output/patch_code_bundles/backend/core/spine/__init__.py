# File: v2/backend/core/spine/__init__.py
from __future__ import annotations

"""
Spine public API.

This package exposes a small, stable surface for callers:
- Spine:        class-based entrypoint (dispatch, pipelines)
- Registry:     provider registry (used internally by Spine)
- CapabilitiesLoader: YAML → capability map loader
- Middlewares:  GuardMiddleware, TimingMiddleware, RetriesMiddleware
- Contracts:    Envelope, Task, Artifact, Problem, helpers (new_envelope, to_dict)
- Validation:   VALIDATORS registry and helper combinators

All cross-domain work should go through Spine capabilities.
"""

# Core bus
from .bootstrap import Spine
from .registry import Registry

# Loader
from .loader import CapabilitiesLoader

# Middleware (class-based, callables via __call__)
from .middleware import GuardMiddleware, TimingMiddleware, RetriesMiddleware

# Contracts & helpers
from .contracts import (
    Envelope,
    Task,
    Artifact,
    Problem,
    new_envelope,
    to_dict,
)

# Validation registry & helpers (optional use by providers/callers)
from .validation import (
    VALIDATORS,
    ValidationRegistry,
    Validator,
    register_input_validator,
    register_output_validator,
    get_input_validator,
    get_output_validator,
    require_fields,
    forbid_empty,
    all_of,
    any_of,
)

__all__ = [
    # Core
    "Spine",
    "Registry",
    "CapabilitiesLoader",
    # Middleware
    "GuardMiddleware",
    "TimingMiddleware",
    "RetriesMiddleware",
    # Contracts
    "Envelope",
    "Task",
    "Artifact",
    "Problem",
    "new_envelope",
    "to_dict",
    # Validation
    "VALIDATORS",
    "ValidationRegistry",
    "Validator",
    "register_input_validator",
    "register_output_validator",
    "get_input_validator",
    "get_output_validator",
    "require_fields",
    "forbid_empty",
    "all_of",
    "any_of",
]


