# bundle_io.py
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

__all__ = ["FileRec", "BundleIO", "read_folder"]


@dataclass(frozen=True)
class FileRec:
    """
    A single file record suitable for packing into a bundle.

    Attributes:
        path:   POSIX-style relative path used inside the bundle.
        data:   Raw file bytes.
        sha256: Hex digest of `data` (lowercase).
    """
    path: str
    data: bytes
    sha256: str


class BundleIO:
    """
    Minimal file ingestion helpers for bundle construction.

    Notes
    -----
    - Deterministic traversal: directory names and filenames are sorted.
    - Unreadable files are skipped (best-effort behavior matches original).
    - `prefix` is concatenated exactly like the original implementation.
    """

    @staticmethod
    def normalize_path(p: str) -> str:
        """
        Normalize a filesystem path to a POSIX-style, bundle-internal path.
        Mirrors original behavior: backslashes → '/', strip leading './'.
        """
        return p.replace("\\", "/").lstrip("./")

    @staticmethod
    def read_folder(
        root: Path,
        *,
        prefix: str = "",
        follow_symlinks: bool = False,
        include: Optional[Callable[[Path], bool]] = None,
        exclude: Optional[Callable[[Path], bool]] = None,
    ) -> List[FileRec]:
        """
        Read all files under `root` into FileRec entries.

        Args:
            root: Base directory to walk.
            prefix: String prepended to each relative path (unchanged from original).
            follow_symlinks: Whether to follow symlinks in traversal (default False).
            include: Optional predicate(path) -> bool; if provided, only files with
                     include(path) == True are kept.
            exclude: Optional predicate(path) -> bool; if provided, files for which
                     exclude(path) == True are skipped.

        Returns:
            List[FileRec] with deterministic ordering by relative path.
        """
        files: List[FileRec] = []
        root = root.resolve()

        # os.walk yields nothing if root doesn't exist -> consistent with original.
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            # Deterministic order
            dirnames.sort()
            filenames.sort()

            for fn in filenames:
                p = Path(dirpath) / fn
                if exclude and exclude(p):
                    continue
                if include and not include(p):
                    continue
                try:
                    data = p.read_bytes()
                except Exception:
                    # Best-effort: skip unreadable files
                    continue

                rel = f"{p.relative_to(root)}".replace("\\", "/")
                # Keep exact original prefix behavior (no slash fixing here)
                bundle_path = f"{prefix}{rel}"
                files.append(
                    FileRec(
                        path=BundleIO.normalize_path(bundle_path),
                        data=data,
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )

        # Deterministic output ordering by path (use same normalized path we emit)
        files.sort(key=lambda fr: fr.path)
        return files


# ------------- legacy shims (backwards compatible API) -------------

def _normalize_path(p: str) -> str:
    """Backward-compatible alias; prefer BundleIO.normalize_path()."""
    return BundleIO.normalize_path(p)

def read_folder(root: Path, prefix: str = "") -> List[FileRec]:
    """
    Backwards-compatible wrapper for the original free function.
    Uses default behavior (no symlinks, no include/exclude predicates).
    """
    return BundleIO.read_folder(root, prefix=prefix)

