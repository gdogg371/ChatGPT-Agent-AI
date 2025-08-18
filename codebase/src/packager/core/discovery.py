from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, List
import os, fnmatch

_JUNK = {"Thumbs.db", ".DS_Store"}

def _cf(s: str, ci: bool) -> str:
    return s.casefold() if ci else s

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
        excl = set(_cf(x, self.cfg.case_insensitive) for x in self.cfg.segment_excludes)
        for seg in rel_parts[:-1]:
            if _cf(seg, self.cfg.case_insensitive) in excl:
                return True
        return False

    def discover(self) -> List[Path]:
        root = self.cfg.root
        if not root.exists():
            raise FileNotFoundError(root)
        out: List[Path] = []
        for cur, dirs, files in os.walk(root, followlinks=self.cfg.follow_symlinks):
            dirs.sort(); files.sort()
            pruned = []
            for d in dirs:
                rel_parts = (Path(cur)/d).relative_to(root).parts
                if self._seg_excluded(rel_parts):
                    continue
                pruned.append(d)
            dirs[:] = pruned
            for fn in files:
                if fn in _JUNK:
                    continue
                p = Path(cur) / fn
                rel = p.relative_to(root).as_posix()
                if self.cfg.include_globs and not any(fnmatch.fnmatch(rel, g) for g in self.cfg.include_globs):
                    continue
                if self.cfg.exclude_globs and any(fnmatch.fnmatch(rel, g) for g in self.cfg.exclude_globs):
                    continue
                out.append(p)
        out.sort(key=lambda x: x.relative_to(root).as_posix())
        return out
