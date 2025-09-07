# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/__init__.py
from __future__ import annotations

"""
Spine Providers Package
-----------------------

Each module in this package exposes one or more provider functions with the
canonical signature:

    def provider(task: Task, context: dict) -> Artifact | list[Artifact] | Any

Providers are referenced from `backend/core/spine/capabilities.yml` using
the "module.path:function_name" target format, and are executed via the
Spine registry/loader.

No side effects or top-level registration occurs here; this package simply
namespaces provider modules.
"""

__all__: list[str] = []
