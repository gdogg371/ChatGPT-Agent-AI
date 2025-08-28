# File: patches/core/patch_ops.py
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from v2.backend.core.utils.io.file_ops import FileOps


@dataclass
class PatchOps:
    file_ops: FileOps

    def _patch_path(self, root: Path, base_name: str, suffix: Optional[str] = None) -> Path:
        """
        Build a deterministic patch path at the **run root** (no /patches subdir).
        If suffix is provided (e.g., '__'), it's appended before '.patch'.
        """
        root.mkdir(parents=True, exist_ok=True)
        name = f"{base_name}{suffix or ''}.patch"
        return root / name

    @staticmethod
    def _unified_diff(
        original_text: str,
        updated_text: str,
        relpath_label: str,
        context_lines: int = 3,
    ) -> str:
        """
        Create a unified diff with minimal, readable context.
        Always returns text with trailing newline.
        """
        orig_lines = original_text.splitlines(keepends=True)
        new_lines = updated_text.splitlines(keepends=True)

        # Header labels are informative, not file system paths
        fromfile = f"a/{relpath_label}"
        tofile = f"b/{relpath_label}"

        diff = difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile=fromfile,
            tofile=tofile,
            n=context_lines,
            lineterm="",  # avoid double newlines; we'll add at the end
        )
        text = "\n".join(diff)
        if not text.endswith("\n"):
            text += "\n"
        return text

    def write_patch(
        self,
        run_root: Path,
        base_name: str,
        original_src: str,
        updated_src: str,
        relpath_label: str,
        *,
        per_item_suffix: Optional[str] = None,
    ) -> Path:
        """
        Write a unified diff patch file.

        - base_name: sanitized file stem (e.g., 'backend__main.py')
        - per_item_suffix: like '__' for per-item artifacts

        Returns the patch path.
        """
        # If no change, do nothing
        if original_src == updated_src:
            # Build a stable, tiny hash to include in the filename to avoid misleading duplicates
            noop_hash = hashlib.sha1((relpath_label + original_src).encode("utf-8")).hexdigest()[:8]
            path = self._patch_path(run_root, base_name, per_item_suffix or f"__noop_{noop_hash}")
            self.file_ops.write_text(path, "# no changes\n")
            return path

        diff_text = self._unified_diff(original_src, updated_src, relpath_label)
        path = self._patch_path(run_root, base_name, per_item_suffix)
        self.file_ops.write_text(path, diff_text)
        return path

    def apply_to_sandbox(self, run_root: Path, relpath: str, updated_src: str) -> Path:
        """
        Writes the updated file into /sandbox_applied/ (under the same run root).
        """
        dst = run_root / "sandbox_applied" / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.file_ops.write_text(dst, updated_src)
        return dst
