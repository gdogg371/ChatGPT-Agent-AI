# v2/backend/core/utils/code_bundles/code_bundles/execute/repo.py

from __future__ import annotations
import fnmatch
import os
import sys
from pathlib import Path
from typing import List, Iterable

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

__all__ = ["_match_any", "_seg_excluded", "discover_repo_paths", "_is_managed_path"]


def _match_any(p: Path, patterns: Iterable[str]) -> bool:
    pp = str(p).replace("\\", "/")
    for pat in patterns or []:
        if fnmatch.fnmatch(pp, pat):
            return True
    return False


def _seg_excluded(p: Path, seg_excludes: Iterable[str]) -> bool:
    parts = [s.lower() for s in Path(p).parts]
    return any(seg.lower() in parts for seg in seg_excludes or [])


def discover_repo_paths(root: Path, include_globs: List[str], exclude_globs: List[str], segment_excludes: List[str]) -> List[Path]:
    root = Path(root).resolve()
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        if _seg_excluded(dp, segment_excludes):
            continue
        for fn in filenames:
            fp = (dp / fn).resolve()
            if _match_any(fp.relative_to(root), include_globs) and not _match_any(fp.relative_to(root), exclude_globs):
                out.append(fp)
    return out


def _is_managed_path(path: Path, managed_roots: List[Path]) -> bool:
    p = Path(path).resolve()
    for root in managed_roots or []:
        try:
            p.relative_to(Path(root).resolve())
            return True
        except Exception:
            pass
    return False
