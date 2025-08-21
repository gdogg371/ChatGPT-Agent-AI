# SPDX-License-Identifier: MIT
# Package: v2.backend.core.spine
"""
Minimal “spine” package to provide a thin, stable translation layer:
- Canonical contracts (Envelope, Task, Artifact, Problem)
- A tiny capability registry (capability -> provider)
- Adapter base protocol for edge shims

This package is intentionally small and dependency-light.
"""

from .contracts import Envelope, Task, Artifact, Problem, new_envelope
from .registry import Registry, CapabilityDescriptor, Provider
