# v2/backend/core/utils/code_bundles/code_bundles/execute/fs.py

from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import List

# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

__all__ = ["_clear_dir_contents", "copy_snapshot"]


def _clear_dir_contents(p: Path) -> None:
    p = Path(p)
    if not p.exists():
        return
    for child in p.iterdir():
        if child.is_dir():
            for root, dirs, files in os.walk(child, topdown=False):
                for fn in files:
                    Path(root, fn).unlink(missing_ok=True)
                for d in dirs:
                    Path(root, d).rmdir()
            child.rmdir()
        else:
            child.unlink(missing_ok=True)


def copy_snapshot(src: Path, dst: Path, rels: List[Path]) -> None:
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for rel in rels or []:
        sp = (src / rel).resolve()
        dp = (dst / rel).resolve()
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(sp.read_bytes())
