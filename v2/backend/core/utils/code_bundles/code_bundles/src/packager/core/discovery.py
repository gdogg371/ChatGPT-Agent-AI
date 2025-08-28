# File: output/patch_code_bundles/backend/core/utils/code_bundles/code_bundles/src/packager/core/discovery.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, List
import os
import fnmatch

# Files we always ignore
_JUNK = {"Thumbs.db", ".DS_Store"}


def _cf(s: str, ci: bool) -> str:
    """Case-fold helper when case_insensitive is enabled."""
    return s.casefold() if ci else s


def _norm_glob(pattern: str) -> str:
    """Normalize glob pattern separators to POSIX for cross-platform matching."""
    return pattern.replace("\\", "/")


def _match_glob(rel_posix: str, pattern: str, ci: bool) -> bool:
    """
    Cross-platform glob match:
    - normalize pattern slashes
    - optionally case-fold for deterministic behavior across OSes
    """
    pat = _norm_glob(pattern)
    if ci:
        return fnmatch.fnmatch(rel_posix.casefold(), pat.casefold())
    return fnmatch.fnmatch(rel_posix, pat)


@dataclass(frozen=True)
class DiscoveryConfig:
    root: Path
    segment_excludes: Tuple[str, ...]
    include_globs: Tuple[str, ...]
    exclude_globs: Tuple[str, ...]
    case_insensitive: bool = False
    follow_symlinks: bool = False


class DiscoveryEngine:
    """Deterministic tree discovery with depth-aware segment excludes and globs."""

    def __init__(self, cfg: DiscoveryConfig) -> None:
        self.cfg = cfg

    def _seg_excluded(self, rel_parts: Tuple[str, ...]) -> bool:
        """
        Segment-based directory exclusion.

        NOTE: No allow-list exceptions. If a segment (like 'output') is present
        in `segment_excludes`, *any* path containing that segment is excluded.
        """
        excluded = set(_cf(x, self.cfg.case_insensitive) for x in self.cfg.segment_excludes)
        for seg in rel_parts[:-1]:  # exclude the filename itself
            if _cf(seg, self.cfg.case_insensitive) in excluded:
                return True
        return False

    def discover(self) -> List[Path]:
        root = self.cfg.root
        if not root.exists():
            raise FileNotFoundError(root)

        out: List[Path] = []

        for cur, dirs, files in os.walk(root, followlinks=self.cfg.follow_symlinks):
            # Deterministic order
            dirs.sort()
            files.sort()

            # Prune directories by segment excludes
            pruned: List[str] = []
            for d in dirs:
                rel_parts = (Path(cur) / d).relative_to(root).parts
                if self._seg_excluded(rel_parts):
                    continue
                pruned.append(d)
            dirs[:] = pruned

            # Files
            for fn in files:
                if fn in _JUNK:
                    continue
                p = Path(cur) / fn
                rel_posix = p.relative_to(root).as_posix()

                # include_globs: if set, require a match
                if self.cfg.include_globs and not any(
                    _match_glob(rel_posix, g, self.cfg.case_insensitive) for g in self.cfg.include_globs
                ):
                    continue

                # exclude_globs: skip if any match
                if self.cfg.exclude_globs and any(
                    _match_glob(rel_posix, g, self.cfg.case_insensitive) for g in self.cfg.exclude_globs
                ):
                    continue

                out.append(p)

        # Stable sort by repo-relative path
        out.sort(key=lambda x: x.relative_to(root).as_posix())
        return out


