# File: v2/backend/core/utils/code_bundles/spine_targets.py
from __future__ import annotations

"""
Spine capability implementations for code-bundle related operations.

Capability implemented
----------------------
- codebundle.inject_prompts.v1
    Input payload:
      {
        "prompts": {             # result from a prompt builder (messages + batch)
          "messages": {"system": str, "user": str},
          "batch": [ { "id": str, ... }, ... ]
        },
        "bundle_root": str,      # directory to write into
        "manifest_relpath": str  # e.g. "design_manifest/prompts.jsonl"
      }

    Behaviour:
      - Creates <bundle_root>/<manifest_relpath> (all parent dirs as needed).
      - Writes one JSON line per prompt item with shape:
          {"id": "...", "messages": {"system": "...", "user": "..."}, "item": {...}}
      - Returns Artifact(kind="Result", meta.result={"wrote": <int>, "path": <abs path>})
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from v2.backend.core.spine.contracts import Artifact


def _ok(result: Any) -> List[Artifact]:
    return [
        Artifact(
            kind="Result",
            uri="spine://codebundle/ok",
            sha256="",
            meta={"result": result},
        )
    ]


def _err(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri="spine://codebundle/error",
            sha256="",
            meta={
                "problem": {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": dict(details or {}),
                }
            },
        )
    ]


def inject_prompts_v1(payload: Dict[str, Any]) -> List[Artifact]:
    prompts = payload.get("prompts") or {}
    msgs = prompts.get("messages") or {}
    system = msgs.get("system") or ""
    user = msgs.get("user") or ""

    batch = list(prompts.get("batch") or [])
    bundle_root = Path(str(payload.get("bundle_root") or ".")).resolve()
    rel = str(payload.get("manifest_relpath") or "design_manifest/prompts.jsonl")
    out_fp = (bundle_root / rel).resolve()

    try:
        out_fp.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out_fp.open("w", encoding="utf-8") as f:
            for it in batch:
                line = {
                    "id": str(it.get("id") or count),
                    "messages": {"system": system, "user": user},
                    "item": it,
                }
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
                count += 1
    except Exception as e:
        return _err("WriteError", f"Failed to write manifest: {e}", {"path": str(out_fp)})

    return _ok({"wrote": count, "path": str(out_fp)})
