# v2/backend/core/patch_engine/config.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import os


@dataclass
class SafetyLimits:
    """
    Back-compat shim + reasonable defaults for patch engine safety.

    Fields:
      - max_single_file_bytes: refuse to patch files larger than this.
      - max_total_apply_bytes: refuse if total patch payload exceeds this.
      - max_hunks_per_file: hard cap on number of hunks per file.
      - windows_path_max: soft limit to keep paths well under Win path limits.
      - safe_guard_symlinks: never follow/modify symlinks in target tree.
      - forbid_repo_recursion: prevent mirrors nesting inside source.
      - denylist_globs: absolute/relative patterns to reject outright.
    """
    max_single_file_bytes: int = 2_000_000
    max_total_apply_bytes: int = 10_000_000
    max_hunks_per_file: int = 200
    windows_path_max: int = 240
    safe_guard_symlinks: bool = True
    forbid_repo_recursion: bool = True
    denylist_globs: List[str] = field(default_factory=list)

    @classmethod
    def defaults(cls) -> "SafetyLimits":
        """Alias kept for older call sites."""
        return cls()

    @classmethod
    def from_env(cls) -> "SafetyLimits":
        """Allow lightweight overrides via env vars."""
        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "").strip() or default)
            except Exception:
                return default

        return cls(
            max_single_file_bytes=_int("PE_MAX_SINGLE_FILE_BYTES", cls.max_single_file_bytes),
            max_total_apply_bytes=_int("PE_MAX_TOTAL_APPLY_BYTES", cls.max_total_apply_bytes),
            max_hunks_per_file=_int("PE_MAX_HUNKS_PER_FILE", cls.max_hunks_per_file),
            windows_path_max=_int("PE_WINDOWS_PATH_MAX", cls.windows_path_max),
            safe_guard_symlinks=(os.environ.get("PE_SAFE_GUARD_SYMLINKS", "1").strip() not in {"0", "false", "off"}),
            forbid_repo_recursion=(os.environ.get("PE_FORBID_REPO_RECURSION", "1").strip() not in {"0", "false", "off"}),
            denylist_globs=[
                s.strip() for s in os.environ.get("PE_DENYLIST_GLOBS", "").split(",") if s.strip()
            ],
        )


@dataclass
class PatchEngineConfig:
    """
    Configuration for the patch application sandbox/target.

    Fields:
      - mirror_current: directory where the sandbox (or fixed target) root lives.
      - source_seed_dir: path to the source tree used to seed the target (may be empty for 'skip' strategy).
      - initial_tests / extensive_tests: optional test command lines to run.
      - archive_enabled: if True, keep archived copies inside the target before overwrites.
      - promotion_enabled: if True, allow promotion to 'live' (generally False in CI/engine).
      - ignore_globs: extra patterns to ignore when seeding (e.g., ["output", ".git", "__pycache__"]).
      - safety: SafetyLimits object; callers may pass custom limits or rely on defaults().
    """

    mirror_current: Path
    source_seed_dir: Path

    initial_tests: List[str] = field(default_factory=list)
    extensive_tests: List[str] = field(default_factory=list)

    archive_enabled: bool = False
    promotion_enabled: bool = False

    # Allow callers to pass patterns to skip when seeding the mirror/target
    ignore_globs: List[str] = field(default_factory=list)

    # Safety knobs (optional; defaulted if not provided)
    safety: SafetyLimits = field(default_factory=SafetyLimits.defaults)

    def ensure_dirs(self) -> None:
        """Create the required directory structure inside the target/mirror."""
        self.mirror_current.mkdir(parents=True, exist_ok=True)
        (self.mirror_current / "Archive").mkdir(parents=True, exist_ok=True)
        (self.mirror_current / "Runs").mkdir(parents=True, exist_ok=True)

    def default_ignores(self) -> List[str]:
        """
        Built-in ignore list plus user-provided extras. These are matched
        against relative paths from 'source_seed_dir'.
        """
        builtin = [
            "__pycache__",
            ".git",
            ".idea",
            ".vscode",
            "node_modules",
            "dist",
            "build",
            ".venv",
            "venv",
            "*.log",
            "*.tmp",
        ]
        # preserve order, remove duplicates
        seen = set()
        ordered: List[str] = []
        for pat in builtin + (self.ignore_globs or []):
            if pat not in seen:
                seen.add(pat)
                ordered.append(pat)
        return ordered


__all__ = ["PatchEngineConfig", "SafetyLimits"]

