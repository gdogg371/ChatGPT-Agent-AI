# File: v2/backend/core/spine/providers/packager_bundle_inject_prompt.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from v2.backend.core.spine.contracts import Artifact, Problem


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _try_invoke_code_bundles(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Best-effort invocation of the code-bundles integration. If the function
    is not present or raises, return None and let the caller fall back.
    """
    try:
        from v2.backend.core.utils.code_bundles.code_bundles import spine_targets as cb  # type: ignore
    except Exception:
        return None

    fn = getattr(cb, "inject_prompts_v1", None)
    if not callable(fn):
        return None

    # Try a couple of signatures to be resilient to minor drift.
    try:
        res = fn(payload)  # type: ignore
        if isinstance(res, dict):
            return res
    except TypeError:
        try:
            res = fn(task_like=payload)  # type: ignore
            if isinstance(res, dict):
                return res
        except Exception:
            return None
    except Exception:
        return None

    return None


def run_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Artifact:
    """
    Inject prompt/batch metadata into the active code bundle (if integration is present).
    Otherwise, write a JSON file to the run directory so artifacts are still discoverable.

    Payload:
      {
        "root": "<abs or project root>",
        "project_root": "<abs project root>",
        "run_dir": "<engine run dir>",
        "messages": [ {"role":"system","content":"..."}, ... ]   # optional
        "batches": [ {"id":"...","messages":[...]}, ... ]        # optional
        "provider": "openai" | "mock" | ...,
        "model": "gpt-4o-mini" | ...,
        "ask_spec": { ... }                                      # optional
      }

    Returns:
      Artifact(kind="Text", uri="file://.../bundle.prompts.json", meta={"ok": true, ...})
      or a Problem artifact if something unrecoverable happens.
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    # Normalize minimal fields
    root = payload.get("project_root") or payload.get("root") or "."
    run_dir = Path(payload.get("run_dir") or (Path("output/patches_received") / "run"))
    run_dir.mkdir(parents=True, exist_ok=True)

    out_path = Path(run_dir) / "bundle.prompts.json"
    record = {
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "ask_spec": payload.get("ask_spec") or {},
        "messages": payload.get("messages") or [],
        "batches": payload.get("batches") or [],
    }

    # 1) Try the code-bundles integration first.
    res = _try_invoke_code_bundles(
        {
            "root": root,
            "run_dir": str(run_dir),
            "record": record,
        }
    )
    if isinstance(res, dict) and res.get("ok"):
        # The integration wrote its own artifacts; echo a simple Artifact.
        return Artifact(
            kind="Text",
            uri=res.get("uri") or f"file://{out_path.as_posix()}",
            sha256=res.get("sha256") or "",
            meta={"ok": True, "integration": "code_bundles", **res},
        )

    # 2) Fallback: write a JSON snapshot into the run dir.
    try:
        _write_json(out_path, record)
        return Artifact(
            kind="Text",
            uri=f"file://{out_path.as_posix()}",
            sha256="",
            meta={"ok": True, "integration": "fallback", "path": str(out_path)},
        )
    except Exception as e:
        return Artifact(
            kind="Problem",
            uri="spine://problem/packager.bundle.inject_prompt.v1",
            sha256="",
            meta={
                "problem": {
                    "code": "WriteError",
                    "message": f"Failed to write bundle.prompts.json: {e}",
                    "retryable": False,
                    "details": {"path": str(out_path)},
                }
            },
        )
