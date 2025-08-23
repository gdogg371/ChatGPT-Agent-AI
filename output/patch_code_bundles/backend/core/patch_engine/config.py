# File: backend/core/patch_engine/config.py
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _d(*parts: str | Path) -> Path:
    return Path(*map(str, parts)).resolve()


@dataclass
class SafetyLimits:
    max_patch_bytes: int = 2_000_000
    max_touched_files: int = 500
    allow_renames: bool = True


@dataclass
class PatchEngineConfig:
    """
    Minimal, file-only configuration for the patch engine.
    - 'mirror_current' points to the working mirror of the inscope code.
    - 'source_seed_dir' (optional) used to seed the mirror if it's empty.
    - test command lists may be empty (no tests).
    """

    # Mirror (current parallel codebase)
    mirror_current: Path

    # Optional: if the mirror doesn't exist or is empty, seed it from here once.
    source_seed_dir: Optional[Path] = None

    # Output roots
    output_root: Path = field(default_factory=lambda: _d("output"))
    patches_received: Path = field(default_factory=lambda: _d("output", "patches_received"))
    runs_root: Path = field(default_factory=lambda: _d("output", "runs"))
    mirrors_root: Path = field(default_factory=lambda: _d("output", "mirrors"))
    snapshots_root: Path = field(default_factory=lambda: _d("output", "mirrors", "snapshots"))
    archives_root: Path = field(default_factory=lambda: _d("output", "archives"))

    # Scope (root is implicit: the mirror)
    excludes: List[str] = field(
        default_factory=lambda: [
            "**/.git/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/output/**",
        ]
    )

    # Tests: each item is a shell command string (executed relative to workspace)
    initial_tests: List[str] = field(default_factory=list)
    extensive_tests: List[str] = field(default_factory=list)

    # Archives
    archive_enabled: bool = True
    keep_last_snapshots: int = 5

    # Safety
    safety: SafetyLimits = field(default_factory=SafetyLimits)

    # Promotion toggle (explicitly disabled by default)
    promotion_enabled: bool = False

    def ensure_dirs(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.patches_received.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.mirrors_root.mkdir(parents=True, exist_ok=True)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.archives_root.mkdir(parents=True, exist_ok=True)

