# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/packager_bundle_make.py
from __future__ import annotations

"""
Capability: packager.bundle.make.v1
-----------------------------------
Create a self-contained JSONL code bundle (plus SHA256SUMS) from a source tree.

Payload
-------
- root:           str   (REQUIRED)  Directory to scan
- out_file:       str   (REQUIRED)  Path to write the JSONL bundle (e.g., output/code_bundles/code_bundle.jsonl)
- sums_file:      str   (REQUIRED)  Path to write the SHA256SUMS file
- exclude_dirs:   list[str] (optional)  Directory names to skip (exact segment match)
- exclude_exts:   list[str] (optional)  File extensions to skip (e.g., [".pyc", ".log"])
- follow_symlinks: bool    (optional)  Follow symlinks (default False)

Return
------
{
  "root": "<abs>",
  "out_file": "<abs>",
  "sums_file": "<abs>",
  "files_written": <int>,
  "bytes_written_bundle": <int>,
  "bytes_written_sums": <int>,
  "excluded": {"dirs": [...], "exts": [...]}
}
"""

import base64
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _should_skip_dir(dir_path: Path, root: Path, excluded_names: set[str]) -> bool:
    """Skip if any segment from root→dir_path is in excluded_names."""
    # Compare by path segments to avoid accidental substring hits
    for part in dir_path.relative_to(root).parts:
        if part in excluded_names:
            return True
    return False


def _iter_files(root: Path, follow_symlinks: bool) -> Iterable[Path]:
    # rglob handles symlinks if follow_symlinks=True (Pathlib follows in .iterdir/.glob).
    # We explicitly gate following by checking is_symlink.
    for p in root.rglob("*"):
        try:
            if p.is_symlink() and not follow_symlinks:
                continue
            if p.is_file():
                yield p
        except OSError:
            # Broken symlink or permission issue: skip
            continue


def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    root_raw = payload.get("root")
    out_file_raw = payload.get("out_file")
    sums_file_raw = payload.get("sums_file")
    if not root_raw or not out_file_raw or not sums_file_raw:
        raise ValueError("payload must include 'root', 'out_file', and 'sums_file'")

    root = Path(str(root_raw)).expanduser().resolve()
    out_file = Path(str(out_file_raw)).expanduser().resolve()
    sums_file = Path(str(sums_file_raw)).expanduser().resolve()

    if not root.is_dir():
        raise FileNotFoundError(f"root directory not found: {root}")

    exclude_dirs = set(map(str, payload.get("exclude_dirs") or [])) or {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "output",
        "dist",
        "build",
        ".venv",
        "venv",
        ".idea",
        ".vscode",
        "software",
        "node_modules",
    }
    exclude_exts = set(map(str, payload.get("exclude_exts") or [])) or {
        ".pyc",
        ".pyo",
        ".pyd",
        ".log",
    }
    follow_symlinks = bool(payload.get("follow_symlinks", False))

    out_file.parent.mkdir(parents=True, exist_ok=True)
    sums_file.parent.mkdir(parents=True, exist_ok=True)

    files_written = 0
    # Write JSONL bundle
    with out_file.open("w", encoding="utf-8", newline="\n") as bundle_f, \
         sums_file.open("w", encoding="utf-8", newline="\n") as sums_f:

        for p in _iter_files(root, follow_symlinks=follow_symlinks):
            # Skip files under excluded dirs
            if _should_skip_dir(p.parent if p.is_file() else p, root, exclude_dirs):
                continue
            # Skip by extension
            if p.suffix.lower() in exclude_exts:
                continue

            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                # In case of symlink jumps outside root → skip
                continue

            try:
                blob = p.read_bytes()
            except Exception:
                # Unreadable file → skip
                continue

            record = {
                "path": rel,
                "sha256": _sha256_bytes(blob),
                "mode": "text",  # kept for compatibility with existing consumers
                "content_b64": base64.b64encode(blob).decode("ascii"),
            }

            # JSON per line (no indentation for compactness)
            import json  # local import to keep top clean
            bundle_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            sums_f.write(f"{record['sha256']}  {record['path']}\n")
            files_written += 1

    return {
        "root": str(root),
        "out_file": str(out_file),
        "sums_file": str(sums_file),
        "files_written": files_written,
        "bytes_written_bundle": out_file.stat().st_size if out_file.exists() else 0,
        "bytes_written_sums": sums_file.stat().st_size if sums_file.exists() else 0,
        "excluded": {"dirs": sorted(exclude_dirs), "exts": sorted(exclude_exts)},
    }
