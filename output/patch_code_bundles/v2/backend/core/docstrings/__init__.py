from __future__ import annotations

"""
Docstring utilities package.

Keep this __init__ lightweight: avoid importing submodules so that
importing `v2.backend.core.docstrings.verify` doesn't get blocked by
unrelated import issues (e.g., sanitize not present yet).
"""

# Optional, lazy re-export. Never fail package import if sanitize is absent.
try:
    from .sanitize import sanitize_docstring  # noqa: F401
except Exception:  # pragma: no cover
    sanitize_docstring = None  # type: ignore

__all__ = ["sanitize_docstring"]



