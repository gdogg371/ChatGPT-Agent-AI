# v2/backend/core/spine/providers/packager_bundle_inject_prompt.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path
import json
import io
import os

from v2.backend.core.spine.contracts import Artifact, Task


def _result(meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri="spine://result/packager_bundle_inject_prompt.v1", sha256="", meta=meta)]


def _problem(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri="spine://problem/packager_bundle_inject_prompt.v1", sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": False, "details": details or {}}
    })]


def _ensure_parent(file: Path) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)


def _prepend_line(file: Path, line: str) -> None:
    """Prepend a single line to a UTF-8 text file (create if missing)."""
    _ensure_parent(file)
    if not file.exists():
        file.write_text(line, encoding="utf-8")
        return
    original = file.read_text(encoding="utf-8")
    file.write_text(line + original, encoding="utf-8")


def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Write assistant_handoff.v1.json and inject a {"record_type":"prompt",...} line
    at the head of design_manifest (monolith or its first part when chunked).

    Payload:
      - bundle: {
          "root": "<dir>",
          "assistant_handoff": "<path>/assistant_handoff.v1.json",
          "manifest": "<path>/design_manifest.jsonl",           # monolith (if present)
          "parts_index": "<path>/design_manifest_parts_index.json",
          "parts_dir": "<path>/design_manifest/",                # when chunked
          "is_chunked": bool,
          "split_bytes": int
        }
      - messages: {system, user}
      - ask_spec: {...}
      - prepared_batch: [...]
      - bundle_meta: {...}
    """
    p = dict(getattr(task, "payload", {}) or {})
    bundle = dict(p.get("bundle") or {})
    messages = dict(p.get("messages") or {})
    ask_spec = dict(p.get("ask_spec") or {})
    prepared_batch = list(p.get("prepared_batch") or [])
    bundle_meta = dict(p.get("bundle_meta") or {})

    root = Path(bundle.get("root") or "")
    if not root:
        return _problem("InvalidPayload", "bundle.root missing")

    # 1) Write assistant_handoff.v1.json
    handoff_path = Path(bundle.get("assistant_handoff") or (root / "assistant_handoff.v1.json"))
    _ensure_parent(handoff_path)
    handoff = {
        "messages": messages,
        "ask_spec": ask_spec,
        "batch": prepared_batch,
        "bundle_meta": bundle_meta,
    }
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle["assistant_handoff"] = str(handoff_path)

    # 2) Inject prompt line at head of design_manifest
    prompt_record = {
        "record_type": "prompt",
        "messages": messages,
        "ask_spec": ask_spec,
        "batch_size": len(prepared_batch),
    }
    line = json.dumps(prompt_record, ensure_ascii=False) + "\n"

    manifest_file = Path(bundle.get("manifest") or "")
    parts_dir = Path(bundle.get("parts_dir") or "")
    is_chunked = bool(bundle.get("is_chunked"))

    if is_chunked:
        # prepend into the first part (00.txt), create if missing
        # parts are typically named 00.txt, 01.txt, ...
        first = None
        if parts_dir.exists() and parts_dir.is_dir():
            # find the lowest-index .txt
            candidates = sorted([p for p in parts_dir.iterdir() if p.is_file() and p.suffix == ".txt"])
            first = candidates[0] if candidates else None
        if first is None:
            first = parts_dir / "00.txt"
        _prepend_line(first, line)
    else:
        if not manifest_file:
            # default to conventional path under root
            manifest_file = root / "design_manifest.jsonl"
        _prepend_line(manifest_file, line)
        bundle["manifest"] = str(manifest_file)

    return _result({"result": {"bundle": bundle}})
