# File: backend/core/patch_engine/scope.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Set
import fnmatch


@dataclass
class Scope:
    """
    Simple path scope with exclude globs.
    Root is the mirror root (must be an absolute Path).
    """

    root: Path
    excludes: List[str]

    def is_within_scope(self, p: Path) -> bool:
        try:
            p_abs = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
            root = self.root.resolve()
            p_abs.relative_to(root)
        except Exception:
            return False
        # Exclude globs (evaluate on POSIX relpath)
        rel = p_abs.relative_to(self.root).as_posix()
        for pat in self.excludes:
            if fnmatch.fnmatch(rel, pat):
                return False
        return True

    def validate_touched_files(self, touched: Iterable[Path]) -> tuple[bool, List[str]]:
        errors: List[str] = []
        for fp in touched:
            parts = list(fp.parts)
            if any(part == ".." for part in parts):
                errors.append(f"Illegal path traversal in: {fp}")
                continue
            if not self.is_within_scope(fp):
                errors.append(f"Out-of-scope path: {fp}")
        return (len(errors) == 0, errors)

    @staticmethod
    def parse_patch_paths(patch_text: str) -> Set[Path]:
        """
        Extract file paths from unified diff headers.
        Considers lines '+++ ' and '--- ' and strips 'a/' or 'b/' prefixes.
        Ignores /dev/null.
        Returns relative Paths (no root prefixed).
        """
        paths: Set[Path] = set()
        for line in patch_text.splitlines():
            line = line.rstrip("\n")
            if not (line.startswith("+++ ") or line.startswith("--- ")):
                continue
            # up to first tab (git can include a timestamp after a tab)
            head = line.split("\t", 1)[0]
            _, path_str = head.split(" ", 1)
            path_str = path_str.strip()
            if path_str == "/dev/null":
                continue
            if path_str.startswith("a/") or path_str.startswith("b/"):
                path_str = path_str[2:]
            # Normalize Windows/Posix separators
            paths.add(Path(path_str))
        return paths
