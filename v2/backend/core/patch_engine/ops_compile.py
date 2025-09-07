from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple
import difflib

from .plan import PatchPlan, PatchOp, ReplaceRange, InsertAt, DeleteRange, AddFile, DeleteFile
from .textops import (
    read_text_preserve,
    split_lines_keepends,
    join_lines,
    detect_eol,
    coerce_eol,
    replace_span,
    insert_after_line,
    after_shebang_and_encoding,
    after_import_block,
)


def _apply_ops_to_file(
    relpath: str,
    original_text: str,
    ops: List[PatchOp],
) -> str:
    """
    Apply only the ops that target `relpath` to the given original_text.
    Returns the new text.
    """
    lines = split_lines_keepends(original_text)
    eol = detect_eol(lines)

    def _to_block(text: str) -> List[str]:
        # Accept arbitrary newline styles in new_text, normalize to '\n' then file EOL.
        block = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=False)
        # Re-append EOL for each logical line; ensure trailing newline at end of block
        out = [ln + "\n" for ln in block]
        if not out:
            return []
        # Coerce to file EOL
        return coerce_eol(out, eol)

    for op in ops:
        if isinstance(op, ReplaceRange) and op.relpath == relpath:
            start0 = max(op.start_line - 1, 0)
            end0 = max(op.end_line - 1, -1)
            block = _to_block(op.new_text)
            lines = replace_span(lines, start0, end0, block)
        elif isinstance(op, InsertAt) and op.relpath == relpath:
            anchor = op.anchor
            line = (op.line or 0)
            block = _to_block(op.new_text)
            if anchor == "file_start":
                lines = block + lines
            elif anchor == "file_end":
                # ensure file ends with newline
                if not lines or not lines[-1].endswith(("\r", "\n")):
                    lines.append("\n")
                lines = lines + block
            elif anchor == "after_shebang_and_encoding":
                idx = after_shebang_and_encoding(lines)
                # insert at index (before current idx elements)
                lines = lines[:idx] + block + lines[idx:]
            elif anchor == "after_import_block":
                idx = after_import_block(lines)
                lines = lines[:idx] + block + lines[idx:]
            elif anchor == "after_line":
                idx0 = max(line - 1, -1)
                lines = insert_after_line(lines, idx0, block)
            elif anchor == "before_line":
                # insert before the specified line
                idx0 = max(line - 1, 0)
                lines = lines[:idx0] + block + lines[idx0:]
            else:
                # unknown anchor -> no-op
                pass
        elif isinstance(op, DeleteRange) and op.relpath == relpath:
            start0 = max(op.start_line - 1, 0)
            end0 = max(op.end_line - 1, -1)
            lines = replace_span(lines, start0, end0, [])
        else:
            # AddFile / DeleteFile not handled here (done by caller who groups per-file)
            pass

    return join_lines(lines)


def _unified_diff(
    relpath: str,
    old_text: str,
    new_text: str,
    *,
    label_a: str = "a",
    label_b: str = "b",
) -> str:
    """
    Build a unified diff for one file, using the file's EOL style for both sides.
    """
    old_lines = split_lines_keepends(old_text)
    new_lines = split_lines_keepends(new_text)

    # difflib expects text lines that end with newline where appropriate
    # Our read/transform functions already preserve keepends, so OK.

    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"--- {label_a}/{relpath}",
            tofile=f"+++ {label_b}/{relpath}",
            lineterm="",  # lines already include EOLs
            n=3,
        )
    )


def ops_to_unified_diff(plan: PatchPlan, workspace: Path) -> str:
    """
    Compile a PatchPlan into a single unified diff string across all files.

    - Reads original files from `workspace`.
    - Applies per-file ops in-memory.
    - Emits diffs only for files whose content actually changes.
    - Handles AddFile/DeleteFile by diffing against empty content or producing empty new content.
    """
    # Group ops per relpath and track file-level add/delete
    by_file: Dict[str, List[PatchOp]] = {}
    add_files: Dict[str, AddFile] = {}
    delete_files: Dict[str, DeleteFile] = {}

    for op in plan.ops:
        if isinstance(op, AddFile):
            add_files[op.relpath] = op
        elif isinstance(op, DeleteFile):
            delete_files[op.relpath] = op
        else:
            by_file.setdefault(getattr(op, "relpath"), []).append(op)

    diffs: List[str] = []

    # Handle files with modifications / inserts / replaces
    for rel, ops in by_file.items():
        path = workspace / rel
        old_text = read_text_preserve(path) if path.exists() else ""
        new_text = _apply_ops_to_file(rel, old_text, ops)

        # If file is also scheduled for deletion, deletion wins (new_text -> "")
        if rel in delete_files:
            new_text = ""

        # If file is scheduled for add and previously empty/missing, seed with add content before ops
        if rel in add_files and old_text == "":
            # Treat add content as the baseline before ops
            base = add_files[rel].content.replace("\r\n", "\n").replace("\r", "\n")
            if not base.endswith("\n"):
                base += "\n"
            old_text = base

        if new_text != old_text:
            diffs.append(_unified_diff(rel, old_text, new_text))

    # Handle pure additions (no other ops)
    for rel, op in add_files.items():
        if rel in by_file:
            continue
        old_text = ""
        new_text = op.content.replace("\r\n", "\n").replace("\r", "\n")
        if not new_text.endswith("\n"):
            new_text += "\n"
        diffs.append(_unified_diff(rel, old_text, new_text))

    # Handle pure deletions (no other ops)
    for rel, op in delete_files.items():
        if rel in by_file:
            continue
        path = workspace / rel
        old_text = read_text_preserve(path) if path.exists() else ""
        new_text = ""
        if old_text != new_text:
            diffs.append(_unified_diff(rel, old_text, new_text))

    return "".join(diffs)
