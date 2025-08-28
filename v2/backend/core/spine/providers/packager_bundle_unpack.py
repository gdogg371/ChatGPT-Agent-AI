# SPDX-License-Identifier: MIT
# File: backend/core/spine/providers/packager_bundle_unpack.py
from __future__ import annotations

"""
Capability: packager.bundle.unpack.v1
-------------------------------------
Unpack a JSONL code bundle onto the filesystem.

Input bundle format (one JSON object per line):
{
  "path": "relative/posix/path.ext",
  "sha256": "<hex digest of raw file bytes>",
  "mode": "text" | "binary" | "...",   # optional/ignored; kept for compatibility
  "content_b64": "<base64-encoded raw file bytes>"
}

Payload
-------
- bundle_file:       str   (REQUIRED)  Path to .jsonl bundle
- out_root:          str   (REQUIRED)  Destination directory to write files into
- clean:             bool  (optional)  If True, remove out_root contents before writing (default False)
- verify_hashes:     bool  (optional)  Verify each record's sha256 before writing (default True)
- fail_on_mismatch:  bool  (optional)  If True, raise on any sha mismatch (default True)
- allow_outside_root:bool  (optional)  If True, allow records whose normalized paths would escape out_root (default False)
- create_manifest:   bool  (optional)  If True, write a summary manifest JSON in out_root (default True)

Return
------
{
  "bundle_file": "<abs>",
  "out_root": "<abs>",
  "files_written": <int>,
  "bytes_written": <int>,
  "hash_mismatches": <int>,
  "skipped_outside_root": <int>,
  "cleaned": true|false,
  "manifest": "<abs or ''>"
}
"""

import base64
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple


# ------------------------------ helpers ---------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_join(root: Path, rel_posix_path: str) -> Tuple[Path, bool]:
    """
    Join root with a user-provided relative POSIX path safely.
    Returns (joined_path, is_inside_root).
    """
    # Normalize separators and remove leading "./"
    norm = rel_posix_path.lstrip("./").replace("\\", "/")
    target = (root / Path(norm)).resolve()
    try:
        is_inside = str(target).startswith(str(root.resolve()) + os.sep) or target == root.resolve()
    except Exception:
        is_inside = False
    return target, is_inside


def _iter_jsonl(fp: Path) -> Iterator[Dict[str, Any]]:
    with fp.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at line {lineno}: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"Record at line {lineno} is not a JSON object")
            yield obj


# ------------------------------ provider --------------------------------------
def run_v1(task, context: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(getattr(task, "payload", {}) or {})

    bundle_file_raw = payload.get("bundle_file")
    out_root_raw = payload.get("out_root")
    if not bundle_file_raw or not out_root_raw:
        raise ValueError("payload must include 'bundle_file' and 'out_root'")

    bundle_file = Path(str(bundle_file_raw)).expanduser().resolve()
    out_root = Path(str(out_root_raw)).expanduser().resolve()

    if not bundle_file.is_file():
        raise FileNotFoundError(f"Bundle file not found: {bundle_file}")

    clean = bool(payload.get("clean", False))
    verify_hashes = bool(payload.get("verify_hashes", True))
    fail_on_mismatch = bool(payload.get("fail_on_mismatch", True))
    allow_outside_root = bool(payload.get("allow_outside_root", False))
    create_manifest = bool(payload.get("create_manifest", True))

    # Prepare destination
    out_root.mkdir(parents=True, exist_ok=True)
    if clean:
        # Remove contents but keep the root directory itself
        for child in out_root.iterdir():
            if child.is_file() or child.is_symlink():
                try:
                    child.unlink()
                except Exception:
                    pass
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    files_written = 0
    bytes_written = 0
    hash_mismatches = 0
    skipped_outside_root = 0

    for rec in _iter_jsonl(bundle_file):
        rel = rec.get("path")
        sha = rec.get("sha256")
        b64 = rec.get("content_b64")

        if not isinstance(rel, str) or not rel:
            raise ValueError("record missing 'path' (non-empty string required)")
        if not isinstance(b64, str) or not b64:
            raise ValueError(f"record for path {rel!r} missing 'content_b64'")
        if verify_hashes and (not isinstance(sha, str) or len(sha) != 64):
            raise ValueError(f"record for path {rel!r} has invalid 'sha256'")

        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise ValueError(f"record for path {rel!r} has invalid base64 content") from e

        if verify_hashes:
            actual = _sha256_bytes(raw)
            if actual != sha:
                hash_mismatches += 1
                if fail_on_mismatch:
                    raise ValueError(
                        f"sha256 mismatch for {rel!r}: expected {sha}, got {actual}"
                    )

        dest, inside = _safe_join(out_root, rel)
        if not inside and not allow_outside_root:
            skipped_outside_root += 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes exactly as present in the bundle
        with open(dest, "wb") as f:
            f.write(raw)
        files_written += 1
        bytes_written += len(raw)

    manifest_path = ""
    if create_manifest:
        manifest = {
            "bundle_file": str(bundle_file),
            "out_root": str(out_root),
            "files_written": files_written,
            "bytes_written": bytes_written,
            "hash_mismatches": hash_mismatches,
            "skipped_outside_root": skipped_outside_root,
            "cleaned": bool(clean),
        }
        manifest_fp = out_root / "_UNPACK_MANIFEST.json"
        manifest_fp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_path = str(manifest_fp.resolve())

    return {
        "bundle_file": str(bundle_file),
        "out_root": str(out_root),
        "files_written": files_written,
        "bytes_written": bytes_written,
        "hash_mismatches": hash_mismatches,
        "skipped_outside_root": skipped_outside_root,
        "cleaned": bool(clean),
        "manifest": manifest_path,
    }
