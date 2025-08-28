# File: v2/backend/core/prompt_pipeline/executor/retriever.py
# Source baseline: https://raw.githubusercontent.com/gdogg371/ChatGPT-Agent-AI/refs/heads/main/output/patch_code_bundles/backend/core/prompt_pipeline/executor/retriever.py
# Changes:
# - Resolve DB 'filepath' relative to scan_root for non-absolute paths.
# - Add support for directory-basename excludes (e.g., "Archive", "output").
# - Keep all existing guardrails (project_root constraint, scan_root check, globs, existence).
# - Enrich each suspect with signature / has_docstring / existing_docstring / context_code.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from v2.backend.core.prompt_pipeline.executor.errors import IoError
from v2.backend.core.prompt_pipeline.executor.sources import analyze_symbol_context
from v2.backend.core.utils.io.file_ops import FileOps


def _is_within(root: Path, candidate: Path) -> bool:
    """Return True if 'candidate' is inside 'root' (or equal). Paths must be resolved."""
    try:
        candidate.relative_to(root)
        return True
    except Exception:
        return False


def _matches_any_glob(rel_posix: str, patterns: tuple[str, ...]) -> bool:
    """
    Return True if rel_posix matches any of the provided glob patterns.
    Matching is case-sensitive and uses fnmatch-style semantics.
    """
    from fnmatch import fnmatchcase

    for pat in patterns or ():
        if fnmatchcase(rel_posix, pat):
            return True
    return False


def _contains_excluded_dir(rel_posix: str, exclude_dirs: tuple[str, ...]) -> bool:
    """
    Case-insensitive directory-basename exclude. If any path segment matches one of the
    names in exclude_dirs, return True.
    """
    if not exclude_dirs:
        return False
    lowers = {d.lower() for d in exclude_dirs if d}
    for seg in rel_posix.split("/"):
        if seg and seg.lower() in lowers:
            return True
    return False


@dataclass(slots=True)
class SourceRetriever:
    """
    Enrich DB rows with filesystem-derived context, with guardrails:
    - Constrain all lookups to a configured scan_root subtree.
    - Respect exclude globs (e.g., '**/output/**', '.git/**', etc.) and directory basenames.
    - Produce normalized absolute path + repo-relative posix path.
    - Populate signature/has_docstring/existing_docstring/context_code for prompt building.

    Notes
    -----
    We deliberately do not try to "remap" old paths to new locations here. If a file
    has moved outside the scan_root, we skip and surface the skip reason.
    """
    project_root: Path
    file_ops: FileOps
    scan_root: Path | None = None
    exclude_globs: tuple[str, ...] = ()
    exclude_dirs: tuple[str, ...] = ()  # NEW: "Archive", "output", etc.

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve()
        if self.scan_root is None:
            self.scan_root = self.project_root
        else:
            self.scan_root = self.scan_root.resolve()

        # Normalize exclude tuples defensively
        if not isinstance(self.exclude_globs, tuple):
            self.exclude_globs = tuple(self.exclude_globs or ())
        if not isinstance(self.exclude_dirs, tuple):
            self.exclude_dirs = tuple(self.exclude_dirs or ())

    def _resolve_paths(self, raw_path: str) -> tuple[Path, str]:
        """
        Resolve the DB 'filepath' into:
          - abs_path: absolute path on disk
          - repo_rel_posix: path relative to project_root, posix-form

        Raises IoError with a precise reason if outside scan_root, excluded, or missing.
        """
        p = Path(raw_path)

        # Treat relative DB paths as relative to scan_root (not project_root),
        # because the docstring scanner wrote filepaths relative to the scan root.
        if p.is_absolute():
            abs_path = p.resolve()
        else:
            abs_path = (self.scan_root / p).resolve()  # type: ignore[operator]

        # Must still be anchored within the repo root
        try:
            repo_rel = abs_path.relative_to(self.project_root)
        except Exception:
            raise IoError(f"Path is outside project_root: {abs_path}")

        repo_rel_posix = repo_rel.as_posix()

        # Enforce scan root explicitly
        if not _is_within(self.scan_root, abs_path):  # type: ignore[arg-type]
            raise IoError(
                f"Path is outside scan_root: {abs_path} (scan_root={self.scan_root})"
            )

        # Enforce directory-basename excludes (case-insensitive)
        if self.exclude_dirs and _contains_excluded_dir(repo_rel_posix, self.exclude_dirs):
            raise IoError(
                f"Path excluded by directory list: {repo_rel_posix} "
                f"(dirs={self.exclude_dirs})"
            )

        # Enforce exclude globs (repo-relative, case-sensitive fnmatch semantics)
        if self.exclude_globs and _matches_any_glob(repo_rel_posix, self.exclude_globs):
            raise IoError(
                f"Path excluded by glob: {repo_rel_posix} "
                f"(patterns={self.exclude_globs})"
            )

        # Ensure file exists
        if not abs_path.exists() or not abs_path.is_file():
            raise IoError(f"File not found: {abs_path}")

        return abs_path, repo_rel_posix

    def _extract_context(
        self, abs_path: Path, lineno: int, name: str | None, symbol_type: str
    ) -> Dict:
        """
        Derive signature/docstring/context for a symbol at `lineno` in `abs_path`.
        Falls back to module-level docstring if symbol_name is missing.
        """
        try:
            ctx = analyze_symbol_context(
                file_path=abs_path,
                lineno=int(lineno or 0),
                symbol_name=name,
                symbol_type=symbol_type or "module",
            )
            # Ensure keys exist
            return {
                "signature": ctx.get("signature"),
                "has_docstring": bool(ctx.get("has_docstring", False)),
                "existing_docstring": ctx.get("existing_docstring") or "",
                "context_code": ctx.get("context_code") or "",
            }
        except Exception:
            # Non-fatal: return empty context; downstream will still pack prompts
            return {
                "signature": None,
                "has_docstring": False,
                "existing_docstring": "",
                "context_code": "",
            }

    def enrich(self, row: Dict) -> Dict:
        """
        Convert a DB row into a 'suspect' dict consumed downstream.

        Required DB fields:
          - 'id'        (int or str)
          - 'filepath'  (repo-relative or absolute)

        Optional DB fields are passed through when present:
          - 'lineno', 'symbol_type', 'name', 'unique_key_hash', 'description', etc.

        Raises IoError (caught by the engine) for any path-related issue.
        """
        if "filepath" not in row:
            raise IoError("DB row missing 'filepath'")

        abs_path, repo_rel_posix = self._resolve_paths(str(row["filepath"]))
        lineno = int(row.get("lineno") or 0)
        symbol_type = row.get("symbol_type") or "module"
        name = row.get("name")

        # Pull lightweight code context for better prompting
        ctx = self._extract_context(abs_path, lineno, name, symbol_type)

        suspect: Dict = {
            "id": str(row.get("unique_key_hash") or row.get("id")),
            "path": str(abs_path),
            "relpath": repo_rel_posix,
            "lineno": lineno,
            "symbol_type": symbol_type,
            # carry-throughs (optional)
            "name": name,
            "unique_key_hash": row.get("unique_key_hash"),
            "description": row.get("description"),
            # enriched context for prompt building
            "signature": ctx["signature"],
            "has_docstring": ctx["has_docstring"],
            "existing_docstring": ctx["existing_docstring"],
            "context_code": ctx["context_code"],
        }
        return suspect




