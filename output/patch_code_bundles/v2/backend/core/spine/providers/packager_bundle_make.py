# v2/backend/core/spine/providers/packager_bundle_make.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime
import json
import os

from v2.backend.core.spine.contracts import Artifact, Task

# Run dir + file ops
from v2.backend.core.utils.io.run_dir import RunDirs
from v2.backend.core.utils.io.file_ops import FileOps, FileOpsConfig

# Reuse your existing indexer provider so we don't duplicate logic
from v2.backend.core.spine.providers.utils_code_index import run_v1 as code_index_v1


# ---------- small utilities ----------
def _ok(meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri="spine://result/packager_bundle_make.v1", sha256="", meta=meta)]

def _ng(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri="spine://problem/packager_bundle_make.v1", sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": False, "details": details or {}}
    })]

def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool): return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"1","true","yes","on"}: return True
        if s in {"0","false","no","off"}: return False
    return default

def _first_artifact(res: Any) -> Tuple[Optional[Artifact], Dict[str, Any]]:
    if isinstance(res, list) and res:
        a = res[0]
        if isinstance(a, Artifact):
            return a, dict(a.meta or {})
        if isinstance(a, dict):
            return Artifact(kind="Result", uri="spine://shim", sha256="", meta=a), dict(a)
        return None, {}
    if isinstance(res, dict):
        return Artifact(kind="Result", uri="spine://shim", sha256="", meta=res), dict(res)
    return None, {}

def _call_provider(fn, payload: Dict[str, Any], context: Dict[str, Any]) -> Any:
    class _Shim:
        def __init__(self, payload: Dict[str, Any]): self.payload = payload; self.envelope = {}; self.payload_schema = {}
    t = _Shim(payload)
    try:
        return fn(t, context or {})
    except TypeError:
        return fn(t)


# ---------- manifest writers ----------
def _write_manifest_monolith(file_ops: FileOps, manifest_path: Path, items: List[Dict[str, Any]]) -> int:
    """
    Write a JSONL manifest where each line is already a dict (e.g. from utils_code_index).
    Returns byte size of the written file.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for it in items:
            # normalize to a 'file' record if not specified
            rec = dict(it)
            if "record_type" not in rec:
                rec["record_type"] = "file"
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return manifest_path.stat().st_size


def _write_manifest_chunked(file_ops: FileOps, parts_dir: Path, parts_index: Path, items: List[Dict[str, Any]], split_bytes: int) -> Dict[str, Any]:
    """
    Split the manifest into parts/00.txt, 01.txt, ... while keeping each part <= split_bytes.
    Returns an index dict with metadata (also written to parts_index).
    """
    parts_dir.mkdir(parents=True, exist_ok=True)

    # Pre-serialize lines to count bytes precisely
    lines: List[str] = []
    for it in items:
        rec = dict(it)
        if "record_type" not in rec:
            rec["record_type"] = "file"
        lines.append(json.dumps(rec, ensure_ascii=False) + "\n")

    parts: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_bytes = 0
    part_idx = 0

    def flush_part():
        nonlocal part_idx, buf, buf_bytes
        if not buf:
            return
        name = f"{part_idx:02d}.txt"
        p = parts_dir / name
        with p.open("w", encoding="utf-8") as f:
            for s in buf:
                f.write(s)
        parts.append({
            "name": name,
            "size": p.stat().st_size,
            "lines": len(buf),
        })
        part_idx += 1
        buf = []
        buf_bytes = 0

    for s in lines:
        s_len = len(s.encode("utf-8"))
        if buf and (buf_bytes + s_len) > split_bytes:
            flush_part()
        buf.append(s)
        buf_bytes += s_len
    flush_part()

    index = {
        "record_type": "parts_index",
        "dir": parts_dir.name,
        "total_parts": len(parts),
        "split_bytes": split_bytes,
        "parts": parts,
    }
    parts_index.parent.mkdir(parents=True, exist_ok=True)
    parts_index.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


# ---------- main capability ----------
def run_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Build a local Code Bundle skeleton and return a bundle descriptor the LLM stage can use.

    Payload (all optional but recommended):
      - mode: "pipeline" | "interactive"               (default: pipeline)
      - out_base: str                                  (e.g., "output/patches_received")
      - project_root: str                              (defaults to cwd)
      - exclude_globs: list[str]
      - chunk_manifest: "auto" | "always" | "never"    (default: auto)
      - split_bytes: int                               (default: 300000)
      - group_dirs: bool                               (default: True)
      - publish_github: bool                           (ignored here; handled elsewhere)

    Output (meta.result.bundle):
      {
        "root": "<run_dir>/bundle",
        "assistant_handoff": "<run_dir>/bundle/assistant_handoff.v1.json",
        "manifest": "<run_dir>/bundle/design_manifest.jsonl",            # only if monolith
        "parts_index": "<run_dir>/bundle/design_manifest_parts_index.json",
        "parts_dir": "<run_dir>/bundle/design_manifest",
        "is_chunked": <bool>,
        "split_bytes": <int>,
        "run_dir": "<run_dir>"
      }
    """
    p = dict(getattr(task, "payload", {}) or {})
    mode = (p.get("mode") or "pipeline").strip().lower()
    out_base = str(p.get("out_base") or "").strip()
    project_root = Path(p.get("project_root") or os.getcwd())
    exclude_globs = list(p.get("exclude_globs") or [])
    chunk_manifest = (p.get("chunk_manifest") or "auto").strip().lower()  # auto|always|never
    split_bytes = int(p.get("split_bytes") or 300000)
    group_dirs = _as_bool(p.get("group_dirs"), True)
    # publish_github = _as_bool(p.get("publish_github"), False)  # not used here

    if not out_base:
        return _ng("InvalidPayload", "out_base is required")

    # 1) Make a timestamped run dir, and a 'bundle' subdir inside it
    rd = RunDirs(Path(out_base))
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run = rd.ensure(run_id)  # provision standard folders
    run_root = Path(run.root)
    bundle_root = run_root / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)

    file_ops = FileOps(FileOpsConfig(preserve_crlf=True))

    # 2) Build a code index using your existing provider
    idx_payload = {"project_root": str(project_root), "exclude_globs": exclude_globs}
    res = _call_provider(code_index_v1, idx_payload, context)
    art, meta = _first_artifact(res)
    if not art:
        return _ng("ProviderError", "utils_code_index.v1 returned nothing")
    if art.kind == "Problem":
        prob = (meta or {}).get("problem", {})
        return _ng(prob.get("code", "ProviderProblem"), prob.get("message", "code index failed"), details=prob)

    # Expect items (list of dicts describing files). We'll treat each dict as one JSONL line.
    items: List[Dict[str, Any]] = list((meta.get("result") or {}).get("items") or meta.get("items") or [])
    if not isinstance(items, list):
        items = []

    # 3) Write design manifest (monolith or chunked)
    manifest_path = bundle_root / "design_manifest.jsonl"
    parts_index_path = bundle_root / "design_manifest_parts_index.json"
    parts_dir = bundle_root / "design_manifest"

    is_chunked = False
    if chunk_manifest == "never":
        _write_manifest_monolith(file_ops, manifest_path, items)
    elif chunk_manifest == "always":
        _ = _write_manifest_chunked(file_ops, parts_dir, parts_index_path, items, split_bytes)
        is_chunked = True
    else:
        # auto: write monolith then flip to chunked if it exceeds split_bytes
        size = _write_manifest_monolith(file_ops, manifest_path, items)
        if size > max(1, split_bytes):
            # switch to parts
            try:
                manifest_path.unlink(missing_ok=True)  # py3.8+: ignore error if not exists
            except TypeError:
                # Python < 3.8
                if manifest_path.exists():
                    manifest_path.unlink()
            _ = _write_manifest_chunked(file_ops, parts_dir, parts_index_path, items, split_bytes)
            is_chunked = True

    # 4) Assemble bundle descriptor
    bundle: Dict[str, Any] = {
        "root": str(bundle_root),
        "assistant_handoff": str(bundle_root / "assistant_handoff.v1.json"),
        "manifest": str(manifest_path) if not is_chunked else "",
        "parts_index": str(parts_index_path) if is_chunked else "",
        "parts_dir": str(parts_dir) if is_chunked else "",
        "is_chunked": is_chunked,
        "split_bytes": split_bytes,
        "run_dir": str(run_root),
        "mode": mode,
        "group_dirs": group_dirs,
    }

    # Note: we do not publish to GitHub here. In 'interactive' mode, your existing
    # publish vehicle should be invoked by a separate provider that consumes this bundle.

    return _ok({"result": {"bundle": bundle}})

