from __future__ import annotations

"""
Docstring utilities package.

This module intentionally avoids importing any task adapters to prevent circular imports.
Adapters live under: v2.backend.core.tasks.docstrings
"""

from .sanitize import sanitize_docstring  # noqa: F401
from .verify import DocstringVerifier     # noqa: F401
from .ast_utils import *                  # noqa: F401,F403
from .promp_api import *                  # noqa: F401,F403
from .prompt_builder import *             # noqa: F401,F403

__all__ = [
    "sanitize_docstring",
    "DocstringVerifier",
    # wildcard exports from ast_utils, promp_api, prompt_builder are included implicitly
]


