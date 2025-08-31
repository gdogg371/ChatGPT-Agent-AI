# superbundle_pack.py
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class SuperbundlePack:
    """
    Utilities for emitting artifact bundles in JSONL form and companion SHA256SUMS.
    Output shapes are identical to the original module-level functions.
    """

    # ---------- public API ----------

    @staticmethod
    def write_artifacts_bundle(path: Path, artifacts: Mapping[str, Any]) -> None:
        """
        Write a JSONL bundle where each line is:
          {"type":"file","path":<rel>,"content_b64":<b64>,"sha256":<hex>}
        The payload is the UTF-8 JSON serialization of artifacts[rel]
        (ensure_ascii=False, sort_keys=True), exactly as before.

        Raises:
            ValueError if an artifact cannot be JSON-serialized.
            OSError/IOError for filesystem write errors.
        """
        SuperbundlePack._ensure_parent(path)
        with open(path, "w", encoding="utf-8") as fo:
            for rel, obj in sorted(artifacts.items()):
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
                fo.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def write_sha256sums(path: Path, artifacts: Mapping[str, Any]) -> None:
        """
        Write a SHA256SUMS file with lines of the form:
          <sha256(hex)>  <rel>
        where the hash is computed over the UTF-8 JSON serialization of
        artifacts[rel] (ensure_ascii=False, sort_keys=True).
        """
        SuperbundlePack._ensure_parent(path)
        with open(path, "w", encoding="utf-8") as fo:
            for rel, obj in sorted(artifacts.items()):
                try:
                    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
                except Exception as e:
                    raise ValueError(f"Failed to serialize artifact at path '{rel}': {e}") from e
                fo.write(f"{hashlib.sha256(payload).hexdigest()}  {rel}\n")

    # ---------- internals ----------

    @staticmethod
    def _ensure_parent(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)


# ---------- legacy shims (backwards-compatible API) ----------

def write_artifacts_bundle(path: Path, artifacts: Mapping[str, Any]) -> None:
    SuperbundlePack.write_artifacts_bundle(path, artifacts)

def write_sha256sums(path: Path, artifacts: Mapping[str, Any]) -> None:
    SuperbundlePack.write_sha256sums(path, artifacts)
