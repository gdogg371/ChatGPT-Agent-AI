from __future__ import annotations

"""
Task adapters registry.

This package contains:
- base.py:     TaskAdapter protocol defining the generic task interface.
- registry.py: select_task_adapter(ask_spec) → TaskAdapter
- docstrings/: DocstringsAdapter that retains docstring-specific logic
               (sanitize, verify, apply, parse) behind the generic interface.
"""

from .base import TaskAdapter  # re-export for convenience
from .registry import select_task_adapter  # re-export
