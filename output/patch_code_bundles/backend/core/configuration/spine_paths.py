# File: backend/core/configuration/spine_paths.py
from __future__ import annotations

"""
Centralized paths and defaults for the Spine.

This module is *purely additive* and avoids touching your existing config.
It resolves Spine resources in the following order:

1) Explicit env overrides:
   - SPINE_DIR
   - SPINE_CAPS_PATH
   - SPINE_PIPELINES_ROOT
   - SPINE_PROFILE (defaults to "default")

2) Bundle-local paths (relative to this file):
   - .../backend/core/spine/capabilities.yml
   - .../backend/core/spine/pipelines/

3) Repo-level fallback discovered by searching upwards for:
   - <ancestor>/backend/core/spine/

This makes the runtime resilient when YAMLs are not emitted into the
output bundle but do exist in the repo.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

# --- anchors ---------------------------------------------------------------

_THIS = Path(__file__).resolve()
CONFIG_DIR = _THIS.parent                  # .../backend/core/configuration
CORE_DIR = CONFIG_DIR.parent               # .../backend/core


# --- finders / helpers -----------------------------------------------------

def _find_repo_spine_upwards(start: Path) -> Optional[Path]:
    """
    Walk ancestors and return the first '<ancestor>/backend/core/spine' that exists.
    """
    for anc in start.parents:
        candidate = anc / "backend" / "core" / "spine"
        if candidate.exists():
            return candidate.resolve()
    return None


def determine_spine_dir() -> Path:
    """
    Decide where the Spine lives, honoring env overrides and sensible fallbacks.
    Order:
      (1) SPINE_DIR env
      (2) bundle-local  .../backend/core/spine
      (3) repo fallback discovered by upward search
      (4) bundle-local path even if missing (so callers consistently get a Path)
    """
    env_dir = os.getenv("SPINE_DIR")
    if env_dir:
        d = Path(env_dir).expanduser()
        if d.exists():
            return d.resolve()

    local = (CORE_DIR / "spine").resolve()
    if local.exists():
        return local

    repo = _find_repo_spine_upwards(_THIS)
    if repo:
        return repo

    return local  # final fallback (may not exist yet)


# --- resolved paths --------------------------------------------------------

SPINE_DIR: Path = determine_spine_dir()

SPINE_CAPS_PATH: Path = Path(
    os.getenv("SPINE_CAPS_PATH") or (SPINE_DIR / "capabilities.yml")
).resolve()

SPINE_PIPELINES_ROOT: Path = Path(
    os.getenv("SPINE_PIPELINES_ROOT") or (SPINE_DIR / "pipelines")
).resolve()

# Runtime profile (e.g., per-customer/preset). Override via env SPINE_PROFILE.
SPINE_PROFILE: str = os.getenv("SPINE_PROFILE", "default")


# --- diagnostics / validation ---------------------------------------------

def sanity_check() -> Tuple[bool, str]:
    """
    Returns (ok, message). If not ok, message lists the missing resources.
    """
    problems: list[str] = []

    if not SPINE_DIR.exists():
        problems.append(f"SPINE_DIR not found: {SPINE_DIR}")

    if not SPINE_CAPS_PATH.exists():
        problems.append(
            "Spine capabilities YAML not found: "
            f"{SPINE_CAPS_PATH}\n"
            "  • Set SPINE_CAPS_PATH, or place 'capabilities.yml' under SPINE_DIR."
        )

    if not SPINE_PIPELINES_ROOT.exists():
        problems.append(
            "Spine pipelines directory not found: "
            f"{SPINE_PIPELINES_ROOT}\n"
            "  • Set SPINE_PIPELINES_ROOT, or create '<SPINE_DIR>/pipelines/'."
        )

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


# --- static self-test ------------------------------------------------------

if __name__ == "__main__":
    print("[spine_paths] Resolved values:")
    print(f"  SPINE_DIR           = {SPINE_DIR}")
    print(f"  SPINE_CAPS_PATH     = {SPINE_CAPS_PATH}")
    print(f"  SPINE_PIPELINES_ROOT= {SPINE_PIPELINES_ROOT}")
    print(f"  SPINE_PROFILE       = {SPINE_PROFILE}")

    ok, msg = sanity_check()
    print(f"[spine_paths] Sanity check: {msg}")
    # Exit non-zero if required resources are missing to make CI noisy.
    raise SystemExit(0 if ok else 1)
