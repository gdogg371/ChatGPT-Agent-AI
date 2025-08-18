from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

from v2.backend.core.docstrings.ast_utils import find_target_by_lineno, TargetInfo
from v2.backend.core.utils.io.file_ops import FileOps
from v2.backend.core.prompt_pipeline.executor.errors import IoError


@dataclass
class SourceRetriever:
    """
    Enrich rows from the introspection DB with precise AST targeting and prompt context.

    This class intentionally exposes a few helper methods (read_source, slice_context, etc.)
    so it remains “richer” and closer to a multi-function retriever without breaking your
    existing orchestrator which only requires enrich().
    """
    project_root: Path
    file_ops: FileOps

    # --------------------
    # Public API
    # --------------------
    def enrich(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expected row keys: id, filepath, lineno, description, name, unique_key_hash
        Returns an item with: id, relpath, path, signature, target_lineno, kind,
                              has_docstring, existing_docstring, context_code
        """
        relpath, abspath = self.resolve_path(row["filepath"])
        src = self.read_source(abspath)

        tinfo = self.reclassify_target(src, int(row.get("lineno", 1)), relpath)

        context_code = self.slice_context(src, anchor_lineno=tinfo.lineno, before=15, after=50)

        return {
            "id": str(row.get("id", row.get("unique_key_hash", ""))),
            "hash": row.get("unique_key_hash"),
            "relpath": relpath,
            "path": str(abspath),
            "description": (row.get("description") or "").strip(),
            "name": row.get("name"),
            "kind": tinfo.kind,
            "target_lineno": tinfo.lineno,
            "signature": tinfo.signature,
            "has_docstring": tinfo.has_docstring,
            "existing_docstring": tinfo.existing_docstring or "",
            "context_code": context_code,
        }

    # --------------------
    # Helpers (kept explicit so this file remains feature-rich)
    # --------------------
    def resolve_path(self, relpath: str) -> Tuple[str, Path]:
        rel = Path(relpath).as_posix()
        abs_path = (self.project_root / rel).resolve()
        if not abs_path.exists():
            raise IoError(f"File not found: {abs_path}")
        return rel, abs_path

    def read_source(self, abspath: Path) -> str:
        return self.file_ops.read_text(abspath)

    def reclassify_target(self, src: str, lineno: int, relpath: str) -> TargetInfo:
        """
        Do NOT trust DB symbol_type for placement. Re-derive from the AST using the lineno hint.
        """
        return find_target_by_lineno(src, lineno, relpath)

    def slice_context(self, src: str, anchor_lineno: int, before: int = 15, after: int = 50) -> str:
        """
        Extract a modest window of code around the anchor to help the LLM.
        """
        lines = src.splitlines()
        idx0 = max(anchor_lineno - 1, 0)
        start = max(idx0 - before, 0)
        end = min(idx0 + after, len(lines))
        return "\n".join(lines[start:end])

    # Optional future hooks: if you had prior utilities, plug them here
    def extract_signature_only(self, src: str, lineno: int) -> Optional[str]:
        """
        Example placeholder if you need a raw signature quickly (not used by orchestrator).
        """
        tinfo = find_target_by_lineno(src, lineno)
        return tinfo.signature if tinfo else None
