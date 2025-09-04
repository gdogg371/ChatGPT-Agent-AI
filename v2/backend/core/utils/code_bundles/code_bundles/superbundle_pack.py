# File: v2/backend/core/utils/code_bundles/code_bundles/superbundle_pack.py
"""
SuperbundlePack
---------------
Helper to serialize a mapping of artifacts into a JSONL "superbundle" and to
write a companion SHA256SUMS file derived from those artifact payloads.

Legacy checksum emission can be disabled by setting:
    PACKAGER_DISABLE_LEGACY_SUMS=1
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Any, Tuple
import base64
import hashlib
import json
import os


class SuperbundlePack:
    @staticmethod
    def _ensure_parent(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def write_artifacts_bundle(path: Path, artifacts: Mapping[str, Any]) -> Tuple[int, int]:
        """
        Serialize `artifacts` (mapping of relative path -> any JSON-serializable object)
        into a JSONL bundle at `path`. Each line is a record with base64 content and sha256.

        Returns:
            (num_records, total_bytes_written)
        """
        SuperbundlePack._ensure_parent(path)
        n = 0
        written = 0
        with path.open("wb") as fo:
            for rel, obj in artifacts.items():
                try:
                    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
                except Exception as e:
                    raise ValueError(f"Failed to serialize artifact at path '{rel}': {e}") from e
                rec = {
                    "type": "file",
                    "path": rel,
                    "content_b64": base64.b64encode(payload).decode("ascii"),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
                b = line.encode("utf-8")
                fo.write(b)
                written += len(b)
                n += 1
        return n, written

    @staticmethod
    def write_sha256sums(path: Path, artifacts: Mapping[str, Any]) -> None:
        """
        Write a SHA256SUMS file with lines of the form:
          <sha256(hex)>␠␠<rel>

        The hash is computed over the UTF-8 JSON serialization of artifacts[rel]
        (ensure_ascii=False, sort_keys=True).

        Behavior:
            - When environment variable PACKAGER_DISABLE_LEGACY_SUMS == "1",
              this function is a NO-OP (use the unified emitter instead).
        """
        if os.getenv("PACKAGER_DISABLE_LEGACY_SUMS") == "1":
            return

        SuperbundlePack._ensure_parent(path)
        with path.open("w", encoding="utf-8") as fo:
            for rel, obj in sorted(artifacts.items()):
                try:
                    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
                except Exception as e:
                    raise ValueError(f"Failed to serialize artifact at path '{rel}': {e}") from e
                fo.write(f"{hashlib.sha256(payload).hexdigest()}  {rel}\n")
