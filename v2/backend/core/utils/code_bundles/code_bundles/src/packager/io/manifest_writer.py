# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/io/manifest_writer.py
"""
BundleWriter: writes a JSONL bundle of artifact records and (optionally) a companion
SHA256SUMS file for those records. This module keeps **legacy** checksum writing to
avoid breaking callers, but you can disable legacy checksum emission by setting:

    PACKAGER_DISABLE_LEGACY_SUMS=1

When disabled, `write_sums(...)` becomes a no-op; the unified analysis/sidecar
emitter should be responsible for repo-level checksum files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Any, Mapping
import json
import os
import hashlib

# Preferred internal helpers (present in this codebase)
try:
    from ..core.paths import PathOps  # type: ignore
except Exception:
    class PathOps:  # minimal fallback
        @staticmethod
        def ensure_dir(p: Path) -> None:
            p.parent.mkdir(parents=True, exist_ok=True)

try:
    from ..core.integrity import Integrity  # type: ignore
except Exception:
    class Integrity:  # minimal fallback
        @staticmethod
        def sha256_bytes(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

# Optional legacy superbundle helpers (for shims below)
try:
    from ...superbundle_pack import SuperbundlePack  # type: ignore
except Exception:
    SuperbundlePack = None  # type: ignore


class BundleWriter:
    """
    Writes a JSONL bundle at `out_path`. Each record is serialized as UTF-8 JSON on its own line.

    Typical record shape (not enforced here):
        {
          "type": "file",
          "path": "design_manifest/part_0001.txt",
          "content_b64": "...",          # or other payload fields
          "sha256": "..."                 # optional
        }
    """

    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path

    # ---------- public API ----------

    def write(self, records: Iterable[Dict[str, Any]]) -> Tuple[int, int]:
        """
        Write the given records as JSONL to `self.out_path`.

        Returns:
            (num_records, num_bytes_written)
        """
        PathOps.ensure_dir(self.out_path)
        n = 0
        written = 0
        with self.out_path.open("wb") as f:
            for rec in records:
                line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
                b = line.encode("utf-8")
                f.write(b)
                written += len(b)
                n += 1
        return n, written

    def write_sums(self, out_sums: Path, files: List[Tuple[str, bytes]]) -> None:
        """
        Write a SHA256SUMS file for the provided in-memory files.

        Args:
            out_sums:     Path to write the checksum list.
            files:        List of (relative_name, raw_bytes) to hash.

        Lines are of the form:
            <sha256(hex)>␠␠<relative_name>

        Behavior:
            - When environment variable PACKAGER_DISABLE_LEGACY_SUMS == "1",
              this function is a NO-OP (use the unified emitter instead).
        """
        if os.getenv("PACKAGER_DISABLE_LEGACY_SUMS") == "1":
            return

        # Compute hashes deterministically
        lines = [f"{Integrity.sha256_bytes(data)}  {rel}" for (rel, data) in files]
        PathOps.ensure_dir(out_sums)
        out_sums.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------- internals ----------

    @staticmethod
    def _ensure_parent(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)


# ---------- legacy shims (backwards-compatible API) ----------

def write_artifacts_bundle(path: Path, artifacts: Mapping[str, Any]) -> None:
    """
    Legacy shim that proxies to SuperbundlePack to write a JSONL bundle.
    """
    if SuperbundlePack is None:
        raise RuntimeError("SuperbundlePack not available for legacy shim.")
    SuperbundlePack.write_artifacts_bundle(path, artifacts)


def write_sha256sums(path: Path, artifacts: Mapping[str, Any]) -> None:
    """
    Legacy shim that proxies to SuperbundlePack checksum writer.
    Honors PACKAGER_DISABLE_LEGACY_SUMS like BundleWriter.write_sums.
    """
    if SuperbundlePack is None:
        raise RuntimeError("SuperbundlePack not available for legacy shim.")
    if os.getenv("PACKAGER_DISABLE_LEGACY_SUMS") == "1":
        return
    SuperbundlePack.write_sha256sums(path, artifacts)


