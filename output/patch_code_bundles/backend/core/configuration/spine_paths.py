# File: v2/backend/core/configuration/spine_paths.py
from __future__ import annotations

"""
Centralized paths and defaults for the Spine.

This module is *purely additive* and avoids touching your existing config.py.
Anything that needs the Spine paths should import from here:

    from v2.backend.core.configuration.spine_paths import (
        SPINE_DIR, SPINE_CAPS_PATH, SPINE_PIPELINES_ROOT, SPINE_PROFILE
    )
"""

import os
from pathlib import Path

# .../v2/backend/core/configuration/spine_paths.py
_THIS = Path(__file__).resolve()
CORE_DIR = _THIS.parent.parent            # .../v2/backend/core
SPINE_DIR = (CORE_DIR / "spine").resolve()

# Capability map and pipeline root
SPINE_CAPS_PATH = (SPINE_DIR / "capabilities.yml").resolve()
SPINE_PIPELINES_ROOT = (SPINE_DIR / "pipelines").resolve()

# Runtime profile (e.g., per-customer/preset). Override via env SPINE_PROFILE.
SPINE_PROFILE = os.getenv("SPINE_PROFILE", "default")
