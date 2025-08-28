# contracts.py
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List

from bundle_io import FileRec

__all__ = ["Contracts", "get_schemas", "build_run_manifest", "build_provenance"]


class Contracts:
    """
    Contracts and artifacts for run manifests and proposal schemas.
    Mirrors the original module's behavior and output shapes.
    """

    # ---------- schemas ----------

    @staticmethod
    def get_schemas() -> Dict[str, dict]:
        proposals = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Proposal",
            "type": "object",
            "required": ["item_id", "filepath", "lineno", "docstring"],
            "properties": {
                "item_id": {"type": "integer"},
                "filepath": {"type": "string"},
                "lineno": {"type": "integer"},
                "docstring": {"type": "string"},
            },
            "additionalProperties": True,
        }
        manifest = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Run Manifest",
            "type": "object",
            "required": ["created_at", "inputs_sha", "outputs_index"],
            "properties": {
                "created_at": {"type": "integer"},
                "inputs_sha": {"type": "string"},
                "outputs_index": {"type": "array"},
                "inputs_index": {"type": "array"},
                "codebase_root": {"type": "string"},
            },
            "additionalProperties": True,
        }
        return {"proposals": proposals, "manifest": manifest}

    # ---------- builders ----------

    @staticmethod
    def build_run_manifest(
        *, inputs: List[FileRec], outputs: Dict[str, Any], codebase_root: str = "v2/patches/output/patch_code_bundles/"
    ) -> Dict[str, Any]:
        """
        Create a stable run manifest snapshot.

        inputs:  list of FileRec (path, data, sha256)
        outputs: mapping of relative path -> JSON-serializable object
        """
        h = hashlib.sha256()

        # inputs_index (deterministic by path)
        inputs_index: List[Dict[str, Any]] = []
        for fr in sorted(inputs, key=lambda x: x.path):
            h.update(fr.path.encode("utf-8") + b"\0" + fr.sha256.encode("ascii") + b"\n")
            inputs_index.append({"path": fr.path, "sha256": fr.sha256, "size": len(fr.data)})

        # outputs_index (deterministic by rel path)
        outputs_index: List[Dict[str, str]] = []
        for rel, obj in sorted(outputs.items()):
            payload = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
            sha = hashlib.sha256(payload).hexdigest()
            outputs_index.append({"path": rel, "sha256": sha})

        return {
            "created_at": int(time.time()),
            "codebase_root": codebase_root,
            "inputs_sha": h.hexdigest(),
            "inputs_index": inputs_index,
            "outputs_index": outputs_index,
        }

    @staticmethod
    def build_provenance(manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Thin wrapper to expose artifacts from a manifest."""
        return {"artifacts": manifest.get("outputs_index", [])}


# ---------- legacy function shims (backwards-compatible API) ----------

def get_schemas() -> Dict[str, dict]:
    return Contracts.get_schemas()

def build_run_manifest(*, inputs: List[FileRec], outputs: Dict[str, Any], codebase_root: str = "v2/patches/output/patch_code_bundles/") -> Dict[str, Any]:
    return Contracts.build_run_manifest(inputs=inputs, outputs=outputs, codebase_root=codebase_root)

def build_provenance(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return Contracts.build_provenance(manifest)

