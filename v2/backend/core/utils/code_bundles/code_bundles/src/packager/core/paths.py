from __future__ import annotations
from pathlib import Path, PurePosixPath

class PathOps:
    """Path helpers for bundle emission. Static-only."""
    @staticmethod
    def to_posix_rel(path: Path, root: Path) -> str:
        return PurePosixPath(path.relative_to(root)).as_posix()

    @staticmethod
    def emitted_path(rel_posix: str, emitted_prefix: str) -> str:
        ep = (emitted_prefix or "").strip()
        if not ep.endswith("/"):
            ep += "/"
        return f"{ep}{rel_posix}".strip()

    @staticmethod
    def ensure_dir(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
