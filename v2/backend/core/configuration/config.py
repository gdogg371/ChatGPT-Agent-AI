#v2/backend/core/configuration/config.py
"""
Compatibility shim for legacy constants.

Purpose:
- Preserve imports like `from v2.backend.core.configuration.config import SPINE_CAPS_PATH`
  while the codebase transitions to the centralized YAML loader.
- All values are sourced from the YAML-backed loader (no env vars, no hardcoded paths).

Notes:
- Prefer importing from `v2.backend.core.configuration.loader` in new/updated code.
- This module exposes a minimal, read-only surface to satisfy existing callers.
"""

from __future__ import annotations

from typing import Tuple

from v2.backend.core.configuration.loader import (
    get_spine,
    get_packager,
    get_db,
    get_llm,
)

# ----- Spine -----
_spine = get_spine()
SPINE_CAPS_PATH: str = str(_spine.caps_path)
SPINE_PIPELINES_ROOT: str = str(_spine.pipelines_root)
SPINE_PROFILE: str = _spine.profile

# ----- Packager -----
_pack = get_packager()
PACKAGER_EMITTED_PREFIX: str = _pack.emitted_prefix
PACKAGER_INCLUDE_GLOBS: Tuple[str, ...] = tuple(_pack.include_globs)
PACKAGER_EXCLUDE_GLOBS: Tuple[str, ...] = tuple(_pack.exclude_globs)
PACKAGER_SEGMENT_EXCLUDES: Tuple[str, ...] = tuple(_pack.segment_excludes)

# ----- Database -----
_db = get_db()
SQLALCHEMY_URL: str = _db.url

# ----- LLM -----
_llm = get_llm()
LLM_PROVIDER: str = _llm.provider
LLM_MODEL: str = _llm.model

__all__ = [
    # Spine
    "SPINE_CAPS_PATH",
    "SPINE_PIPELINES_ROOT",
    "SPINE_PROFILE",
    # Packager
    "PACKAGER_EMITTED_PREFIX",
    "PACKAGER_INCLUDE_GLOBS",
    "PACKAGER_EXCLUDE_GLOBS",
    "PACKAGER_SEGMENT_EXCLUDES",
    # DB
    "SQLALCHEMY_URL",
    # LLM
    "LLM_PROVIDER",
    "LLM_MODEL",
]
