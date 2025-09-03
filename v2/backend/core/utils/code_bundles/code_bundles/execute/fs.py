# v2/backend/core/utils/code_bundles/code_bundles/execute/fs.py
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Tuple
import shutil

__all__ = ["_clear_dir_contents", "copy_snapshot"]


def _clear_dir_contents(path: Path) -> None:
    """
    Remove all files/dirs inside 'path' (keep the directory itself).
    Safe to call when the path doesn't exist. Idempotent.
    """
    path = Path(path)
    if not path.exists():
        return
    for child in path.iterdir():
        try:
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child, ignore_errors=True)
        except FileNotFoundError:
            # race-safe: if something else removed it, that's fine
            pass


def copy_snapshot(
    src_root: Path,
    dest_root: Path,
    discovered: Iterable[Tuple[Path, str]],
) -> int:
    """
    Copy the repository snapshot to dest_root using discovered pairs:
      (local_path, repo_relative_posix)

    Returns the number of files successfully copied.
    """
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    count = 0
    for local_path, rel_posix in discovered or []:
        lp = Path(local_path)
        # Only copy regular files; skip directories just in case
        if not lp.is_file():
            continue
        out_path = dest_root / rel_posix
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Binary-safe copy
        out_path.write_bytes(lp.read_bytes())
        count += 1
    return count
