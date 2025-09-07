# File: v2/backend/core/patch_engine/applier.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict
import re

# ------------------------------------------------------------------------------
# Result DTO
# ------------------------------------------------------------------------------
@dataclass
class ApplyResult:
    dry_run_ok: bool
    applied: bool
    rejected_hunks: int
    stdout_path: Path
    rejects_manifest_path: Optional[Path]


# ------------------------------------------------------------------------------
# Unified diff data structures
# ------------------------------------------------------------------------------
@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str]  # raw hunk lines starting with ' ', '+', '-', or '\\'


@dataclass
class FilePatch:
    old_path: Optional[Path]  # None if /dev/null
    new_path: Optional[Path]  # None if /dev/null
    hunks: List[Hunk]


# ------------------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------------------
_HUNK_HDR_RE = re.compile(r"@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")


def _parse_header_path(line: str, prefix: str) -> Optional[Path]:
    """
    Parse a '--- path' or '+++ path' line.

    Strips optional 'a/' or 'b/' prefixes and discards timestamps (after a tab).
    Returns None if path is '/dev/null'.
    """
    assert line.startswith(prefix)
    body = line[len(prefix):].rstrip("\n")
    # Git may add a tab + timestamp after the path
    if "\t" in body:
        body = body.split("\t", 1)[0]
    body = body.strip()
    if body == "/dev/null":
        return None
    if body.startswith("a/") or body.startswith("b/"):
        body = body[2:]
    return Path(body)


def parse_unified_diff(text: str) -> List[FilePatch]:
    """
    Minimal unified diff parser supporting:
      - file headers: --- old, +++ new
      - hunks: @@ -l[,n] +l[,n] @@
      - hunk lines: ' ' (context), '-' (remove), '+' (add), '\' (no newline note)
    """
    lines = text.splitlines(keepends=True)
    i = 0
    files: List[FilePatch] = []

    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue

        old_path = _parse_header_path(lines[i], "--- ")
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("Malformed diff: expected '+++' after '---'")
        new_path = _parse_header_path(lines[i], "+++ ")
        i += 1

        hunks: List[Hunk] = []
        while i < len(lines) and lines[i].startswith("@@"):
            m = _HUNK_HDR_RE.match(lines[i].strip())
            if not m:
                raise ValueError(f"Malformed hunk header: {lines[i]!r}")
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "0")
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "0")
            i += 1

            h_lines: List[str] = []
            # collect hunk lines until next hunk or next file header
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                line = lines[i]
                if line and line[0] in (" ", "+", "-", "\\"):
                    h_lines.append(line)
                    i += 1
                else:
                    break
            hunks.append(Hunk(old_start, old_count, new_start, new_count, h_lines))

        files.append(FilePatch(old_path, new_path, hunks))

    return files


# ------------------------------------------------------------------------------
# Applier (pure Python, no git)
# ------------------------------------------------------------------------------
class PatchApplier:
    """
    Apply a unified diff to the given workspace directory (in-place).

    This implementation performs a full dry-run validation and only writes files
    if **all** hunks across **all** files validate cleanly.

    Limitations:
      - No fuzzy matching; hunks must apply exactly as specified.
      - Rename detection is not implemented (treats as remove+add).
      - '\\ No newline at end of file' markers are ignored (content applied as-is).
    """

    def __init__(self, workspace: Path, apply_dir: Path):
        self.workspace = workspace
        self.apply_dir = apply_dir
        self.apply_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Public API ----------

    def apply_unified_diff(self, patch_file: Path) -> ApplyResult:
        # Preserve patch-file line endings while reading (avoid implicit translation)
        with patch_file.open("r", encoding="utf-8", errors="replace", newline="") as f:
            patch_text = f.read()

        files = parse_unified_diff(patch_text)

        stdout_fp = self.apply_dir / "apply_stdout.txt"
        rejects_manifest = self.apply_dir / "rejected_hunks_manifest.txt"

        # Dry run
        dry_ok, rejects = self._dry_run(files)

        with open(stdout_fp, "w", encoding="utf-8", newline="") as out:
            out.write(f"Applying patch: {patch_file.name}\n")
            out.write(f"Workspace: {self.workspace}\n\n")
            out.write("[dry-run] OK: all hunks validate\n" if dry_ok else "[dry-run] FAILED: some hunks did not match\n")

        if not dry_ok:
            # Write rejects manifest and individual .rej files
            self._write_rejects(rejects_manifest, rejects)
            return ApplyResult(
                dry_run_ok=False,
                applied=False,
                rejected_hunks=sum(len(v) for v in rejects.values()),
                stdout_path=stdout_fp,
                rejects_manifest_path=rejects_manifest,
            )

        # Apply for real (all or nothing)
        self._apply(files)
        return ApplyResult(
            dry_run_ok=True,
            applied=True,
            rejected_hunks=0,
            stdout_path=stdout_fp,
            rejects_manifest_path=None,
        )

    # ---------- Internal helpers ----------

    def _read_lines(self, path: Path) -> List[str]:
        # Keep original EOLs so we can preserve them on write
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            return f.read().splitlines(keepends=True)

    def _detect_eol(self, lines: List[str]) -> str:
        for ln in lines:
            if ln.endswith("\r\n"):
                return "\r\n"
            if ln.endswith("\r"):
                return "\r"
            if ln.endswith("\n"):
                return "\n"
        return "\n"

    def _coerce_eol(self, lines: List[str], eol: str) -> List[str]:
        # Normalize to '\n' then convert to target EOL
        return [ln.replace("\r\n", "\n").replace("\r", "\n").replace("\n", eol) for ln in lines]

    def _write_lines(self, path: Path, lines: List[str], *, prefer_eol: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(lines if prefer_eol is None else self._coerce_eol(lines, prefer_eol))
        # Avoid platform newline translation on write
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(content)

    def _dry_run(self, files: List[FilePatch]) -> Tuple[bool, Dict[Path, List[str]]]:
        all_ok = True
        rejects: Dict[Path, List[str]] = {}

        for fp in files:
            rel_old = fp.old_path
            rel_new = fp.new_path

            # Determine target file (where we match against)
            if rel_old is None and rel_new is not None:
                # new file creation: base is empty
                base_lines: List[str] = []
                base_path = self.workspace / rel_new
            elif rel_old is not None and rel_new is None:
                # file deletion
                base_path = self.workspace / rel_old
                if not base_path.exists():
                    all_ok = False
                    rejects.setdefault(base_path, []).append("Target for deletion does not exist.")
                    continue
                base_lines = self._read_lines(base_path)
            else:
                # modification in place (or rename treated as modify of new path)
                # Prefer reading existing content from rel_old if present; otherwise try rel_new.
                read_path = self.workspace / (rel_old or rel_new)
                if not read_path.exists():
                    read_path = self.workspace / (rel_new or rel_old)
                base_lines = self._read_lines(read_path) if read_path.exists() else []
                base_path = self.workspace / (rel_new or rel_old)

            ok, _ = self._try_apply_to_lines(base_lines, fp.hunks)
            if not ok:
                all_ok = False
                rejects.setdefault(base_path, []).append("One or more hunks failed to match.")

        return all_ok, rejects

    def _apply(self, files: List[FilePatch]) -> None:
        for fp in files:
            # Determine base and target
            if fp.old_path is None and fp.new_path is not None:
                base_lines: List[str] = []
                target_path = self.workspace / fp.new_path
            elif fp.old_path is not None and fp.new_path is None:
                # deletion
                target_path = self.workspace / fp.old_path
                if target_path.exists():
                    base_lines = self._read_lines(target_path)
                    ok, new_lines = self._try_apply_to_lines(base_lines, fp.hunks)
                    if not ok:
                        raise RuntimeError(f"Hunk validation unexpectedly failed during apply: {target_path}")
                    # For deletion, the resulting content should be empty; remove the file.
                    if new_lines:
                        # If hunks would leave content, overwrite with result
                        eol = self._detect_eol(base_lines)
                        self._write_lines(target_path, new_lines, prefer_eol=eol)
                    else:
                        target_path.unlink()
                continue
            else:
                # modification
                prefer = fp.old_path or fp.new_path
                source_path = self.workspace / prefer
                if not source_path.exists():
                    alt = self.workspace / (fp.new_path or fp.old_path)
                    source_path = alt
                base_lines = self._read_lines(source_path) if source_path.exists() else []
                target_path = self.workspace / (fp.new_path or fp.old_path)

            ok, new_lines = self._try_apply_to_lines(base_lines, fp.hunks)
            if not ok:
                raise RuntimeError(f"Hunk validation unexpectedly failed during apply: {target_path}")

            eol = self._detect_eol(base_lines)
            self._write_lines(target_path, new_lines, prefer_eol=eol)

    def _try_apply_to_lines(self, old_lines: List[str], hunks: List[Hunk]) -> Tuple[bool, List[str]]:
        """
        Attempt to apply hunks to old_lines.

        Returns (ok, new_lines_if_ok). Exact matching only (no fuzz).
        """
        # Work on a copy
        new_lines: List[str] = []
        old_idx = 0  # 0-based index into old_lines

        for h in hunks:
            # Convert 1-based old_start to 0-based index; old_start may be 0 for new files
            target_old_idx = max(0, h.old_start - 1)

            # Append unchanged region before hunk
            if target_old_idx < old_idx:
                return False, []
            new_lines.extend(old_lines[old_idx:target_old_idx])
            old_idx = target_old_idx

            # Apply hunk lines
            for raw in h.lines:
                if not raw:
                    return False, []
                tag = raw[0]
                content = raw[1:]  # includes original newline if present

                if tag == " ":
                    # context line must match exactly
                    if old_idx >= len(old_lines) or old_lines[old_idx] != content:
                        return False, []
                    new_lines.append(content)
                    old_idx += 1
                elif tag == "-":
                    # deletion: must match old
                    if old_idx >= len(old_lines) or old_lines[old_idx] != content:
                        return False, []
                    old_idx += 1  # skip (delete) in new output
                elif tag == "+":
                    # addition
                    new_lines.append(content)
                elif tag == "\\":
                    # "\ No newline at end of file" note — ignore for application
                    continue
                else:
                    # unknown tag
                    return False, []

        # After last hunk, append remaining old lines
        new_lines.extend(old_lines[old_idx:])
        return True, new_lines

    def _write_rejects(self, manifest_path: Path, rejects: Dict[Path, List[str]]) -> None:
        if not rejects:
            return
        with open(manifest_path, "w", encoding="utf-8", newline="") as mf:
            for path, items in rejects.items():
                mf.write(f"{path}\n")
                for msg in items:
                    mf.write(f" - {msg}\n")
