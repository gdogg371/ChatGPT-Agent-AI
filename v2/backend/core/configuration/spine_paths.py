#v2/backend/core/configuration/spine_paths.py
"""
Spine path resolution (YAML-driven).

This module exposes the classic Spine variables consumed by the rest of the
codebase, but sources them **only** from the centralized loader:

- No environment variable reads.
- No hardcoded defaults.
- Fail-fast behavior lives in the loader; this module simply reflects values.

Exports (backward compatible):
- SPINE_DIR               : Directory that conceptually contains caps & pipelines.
- SPINE_CAPS_PATH         : Path to capabilities.yml.
- SPINE_PIPELINES_ROOT    : Directory containing pipeline profiles (each with patch_loop.yml).
- SPINE_PROFILE           : Active profile name (string).
- determine_spine_dir()   : Returns SPINE_DIR.
- sanity_check()          : Quick existence re-check (loader already validates).
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple, List

from v2.backend.core.configuration.loader import get_spine, ConfigError


def _compute_spine_dir(caps_path: Path, pipelines_root: Path) -> Path:
    """
    Compute a sensible SPINE_DIR. If caps and pipelines share a parent, use it.
    Otherwise prefer the parent of pipelines_root (this is where profiles live).
    """
    caps_parent = caps_path.parent
    pipes_parent = pipelines_root.parent
    return caps_parent if caps_parent == pipes_parent else pipes_parent


# Read once at import-time; loader performs strict validation.
_SPINE = get_spine()

SPINE_CAPS_PATH: Path = _SPINE.caps_path
SPINE_PIPELINES_ROOT: Path = _SPINE.pipelines_root
SPINE_PROFILE: str = _SPINE.profile
SPINE_DIR: Path = _compute_spine_dir(SPINE_CAPS_PATH, SPINE_PIPELINES_ROOT)


def determine_spine_dir() -> Path:
    """Compatibility wrapper; returns SPINE_DIR."""
    return SPINE_DIR


def sanity_check() -> Tuple[bool, str]:
    """
    Return (ok, message). Loader already validates existence; this is a light
    re-check that keeps the previous interface stable.
    """
    problems: List[str] = []
    if not SPINE_DIR.exists():
        problems.append(f"SPINE_DIR not found: {SPINE_DIR}")
    if not SPINE_CAPS_PATH.is_file():
        problems.append(f"Spine capabilities YAML not found: {SPINE_CAPS_PATH}")
    if not SPINE_PIPELINES_ROOT.is_dir():
        problems.append(f"Spine pipelines directory not found: {SPINE_PIPELINES_ROOT}")
    ok = not problems
    return ok, "OK" if ok else "\n".join(problems)


__all__ = [
    "SPINE_DIR",
    "SPINE_CAPS_PATH",
    "SPINE_PIPELINES_ROOT",
    "SPINE_PROFILE",
    "determine_spine_dir",
    "sanity_check",
]


if __name__ == "__main__":
    print("[spine_paths] Resolved values:")
    print(f" SPINE_DIR = {SPINE_DIR}")
    print(f" SPINE_CAPS_PATH = {SPINE_CAPS_PATH}")
    print(f" SPINE_PIPELINES_ROOT = {SPINE_PIPELINES_ROOT}")
    print(f" SPINE_PROFILE = {SPINE_PROFILE}")
    ok, msg = sanity_check()
    print(f"[spine_paths] Sanity check: {msg}")
    raise SystemExit(0 if ok else 1)
